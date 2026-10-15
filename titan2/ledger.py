"""The trade ledger, the daily report and the pub/ exports (spec §4.6-4.7, §5.3, §8.1, §12; DETAIL §2, §12.5).

- build_trade_row() builds the one immutable `trades` row of a closed structure from its orders' effective fills,
  through charges.net_pnl (the only P&L function). It is pure. positions.close() and close_external() call it and
  insert the row (store.insert_trade) inside the transition's own transaction, so P&L is booked once (I10).
  No LTP enters any money figure: ltp_entry / ltp_exit are logged for information only.
- Valuation never guesses in the bot's favour, and it never blocks a close (the position is already flat):
  an order whose effective fills do not explain its filled_qty is valued at its fill VWAP (ESTIMATED); with no
  fills at all a bot order is valued at its own LIMIT, which an IOC LIMIT can only have improved on (UNRESOLVED);
  an unpriced SELL the bot did not place is valued at 0 (UNRESOLVED). Bought and sold quantities that differ also
  make the row UNRESOLVED. G1 refuses every UNRESOLVED or ESTIMATED trade.
- daily_report() is the 15:38 report per book and strategy (reports/<date>.md plus a Telegram summary). Since
  2026-10-14 night its phone text also gives the day's refusals and voids by reason, the saved what-if totals, the
  runtime's notes (the laptop's condor), the bar check, the alerts delayed or not delivered and, per G1 line, whether
  today counts for G1; its latency table has the quote calls' and broker reads' rows (the engine's LATENCY events).
- pub/ files are written atomically (temp file, fsync, os.replace, mode 0640): the dashboard reads nothing else.
  The engine publishes after its transaction commits, so pub/ never shows a row that was rolled back.
- export_session() writes exports/{events,trades}/<date>.jsonl.gz from the DB, which stays authoritative.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from titan2.charges import CHARGE_FIELDS, OrderFills, OrderPart, RateSet, net_pnl, rates_for, round_money
from titan2.clock import iso, parse_wall
from titan2.model import (
    Account, BrokerTrade, Direction, ExitProfile, Fill, Leg, Order, OrderState, PnlStatus, Role, Side, Structure,
    StructureType,
)
from titan2.settings import G1_LOOKS, HEARTBEAT_EVERY_S
from titan2.store import db_json

if TYPE_CHECKING:
    from titan2.store import Store

__all__ = [
    "CarriedTruth", "DailyReport", "ExecStats", "OrderValue", "TruthInputs", "build_trade_row", "daily_report",
    "execution_stats", "export_session", "external_root", "order_parts", "order_values_by_structure", "publish_acks",
    "publish_events_tail", "publish_g1", "publish_trade_detail", "publish_trades_90d", "publish_trades_today",
    "query_in", "quote_path_1m", "trade_detail", "truth_attribution", "truth_inputs", "value_order", "worse_status",
    "write_daily_report", "write_pub_json",
]

ENTRY_ROLES = frozenset({Role.ENTRY, Role.SELFTEST})     # the opening BUYs; a filled self-test is sold like an entry
COST_ROLES = frozenset({Role.ENTRY, Role.SELFTEST, Role.EXIT})   # orders priced from a decision quote
SENT_STATES = frozenset({OrderState.COMPLETE, OrderState.CANCELLED, OrderState.REJECTED})   # reached the broker
PUB_FILE_MODE = 0o640
_REPLACE_TRIES = 5 if os.name == "nt" else 1           # see _replace()
_REPLACE_BACKOFF_S = 0.01
TRADES_90D_DAYS = 90
EVENTS_TAIL_N = 500
ACKS_N = 50
# The daily report's measurement rows (2026-10-14 night). A quote call this long after the previous one counts as
#   cold: broker.lanes.KEEP_WARM_IDLE_S, the keep-warm call's own threshold, so a cold call is one it did not prevent
QUOTE_COLD_S = 60.0
WHATIF_PREFIX = "whatif-"              # titan2.whatif.REPORT_PREFIX: reports/whatif-<date>.json, one per replayed day
REASONS_SHOWN = 4                      # the phone report's refusal lines name this many reasons, then "+k more"
_IN_CHUNK = 500
_STATUS_RANK = {PnlStatus.FINAL: 0, PnlStatus.ESTIMATED: 1, PnlStatus.UNRESOLVED: 2}


def worse_status(a: PnlStatus | str, b: PnlStatus | str) -> PnlStatus:
    a, b = PnlStatus(a), PnlStatus(b)
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


def _is_exit(role: Role, side: Side) -> bool:
    """Exit side of a phase-1 LONG leg: bot EXIT orders plus closes made outside the bot (model.leg_fill_totals)."""
    return role is Role.EXIT or (role is Role.EXTERNAL and side is Side.SELL)


def _mean(xs: Sequence[float]) -> float | None:
    return math.fsum(xs) / len(xs) if xs else None


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd) + 0.0


def _positive(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


# ---------------------------------------------------------------------------------------------------------------
# Valuing one order from its effective fills
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OrderValue:
    """One order's executed quantity and value, as the ledger books it."""

    order_pk: str
    role: Role
    side: Side
    filled_qty: int
    value: float                      # Σ px·qty; 0.0 when the quantity has no price at all (UNRESOLVED)
    status: PnlStatus                 # FINAL: the effective fills explain filled_qty exactly
    external_kind: str | None
    first_wall: datetime | None       # earliest and latest fill time
    last_wall: datetime | None
    part: OrderPart | None = None     # this row is one leg's share of a broker order shared by several legs

    @property
    def vwap(self) -> float | None:
        return self.value / self.filled_qty if self.filled_qty > 0 and self.value > 0 else None

    def order_fills(self) -> OrderFills | OrderPart | None:
        """The charges input; None when nothing is priced (an unpriced quantity books no value and no charges). A
        part of a shared broker order books its share of that order's charges (charges.OrderPart)."""
        if self.filled_qty <= 0 or self.value <= 0:
            return None
        if self.part is not None:
            return self.part
        return OrderFills(self.side, self.filled_qty, self.value, self.external_kind)


def value_order(order_pk: str, role: Role | str, side: Side | str, filled_qty: int, limit_px: float | None,
                external_kind: str | None, fills: Iterable[Fill], part: OrderPart | None = None) -> OrderValue:
    """Value one order. Effective fills are the truth; the fallbacks are conservative bounds (module docstring).
    part: the row is one leg's share of a broker order shared by several legs (order_parts()); when the fills
    explain it, its value and charges are that share of the whole order's."""
    role, side = Role(role), Side(side)
    fs = list(fills)
    q = sum(f.qty for f in fs)
    v = math.fsum(f.px * f.qty for f in fs)
    walls = [f.wall for f in fs]
    first, last = (min(walls), max(walls)) if walls else (None, None)
    if filled_qty <= 0:
        return OrderValue(order_pk, role, side, 0, 0.0, PnlStatus.FINAL, external_kind, first, last)
    if q == filled_qty:
        if part is not None and part.filled_qty == filled_qty and part.side is side:
            return OrderValue(order_pk, role, side, filled_qty, part.value, PnlStatus.FINAL, external_kind, first,
                              last, part)
        return OrderValue(order_pk, role, side, filled_qty, v, PnlStatus.FINAL, external_kind, first, last)
    if q > 0:                                     # fills explain part of it: their VWAP prices the rest
        return OrderValue(order_pk, role, side, filled_qty, v / q * filled_qty, PnlStatus.ESTIMATED, external_kind,
                          first, last)
    if role is not Role.EXTERNAL and _positive(limit_px):
        return OrderValue(order_pk, role, side, filled_qty, limit_px * filled_qty, PnlStatus.UNRESOLVED,
                          external_kind, first, last)
    if side is Side.SELL:                         # proceeds unknown: book none
        return OrderValue(order_pk, role, side, filled_qty, 0.0, PnlStatus.UNRESOLVED, external_kind, first, last)
    raise ValueError(f"order {order_pk}: {filled_qty} bought with no fill and no limit price; cannot be valued")


def _group_fills(fills: Mapping[str, Iterable[Fill]] | Iterable[Fill]) -> dict[str, list[Fill]]:
    if isinstance(fills, Mapping):
        return {pk: list(fs) for pk, fs in fills.items()}
    out: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        out[f.order_pk].append(f)
    return out


def query_in(store: Store, template: str, ids: Sequence[Any], params: Sequence[Any] = ()) -> list:
    """store.query over `... IN ({})` in chunks (SQLite's oldest host-parameter limit is 999)."""
    rows: list = []
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = list(ids[i:i + _IN_CHUNK])
        rows += store.query(template.format(",".join("?" * len(chunk))), [*params, *chunk])
    return rows


def order_values_by_structure(store: Store, structure_ids: Iterable[int]) -> dict[int, list[OrderValue]]:
    """structure_id -> the OrderValue of every order with fills (bot and EXTERNAL), in creation order."""
    ids = sorted({int(i) for i in structure_ids})
    rows = sorted(query_in(store, "SELECT rowid AS _rowid, order_pk, structure_id, role, side, filled_qty, limit_px, "
                                  "external_kind FROM orders WHERE filled_qty > 0 AND structure_id IN ({})", ids),
                  key=lambda r: r["_rowid"])
    fills = _group_fills(store.effective_fills([r["order_pk"] for r in rows]))
    parts = order_parts(store, [r["order_pk"] for r in rows if r["role"] == Role.EXTERNAL.value])
    out: dict[int, list[OrderValue]] = {i: [] for i in ids}
    for r in rows:
        out[r["structure_id"]].append(value_order(r["order_pk"], r["role"], r["side"], r["filled_qty"],
                                                  r["limit_px"], r["external_kind"], fills.get(r["order_pk"], ()),
                                                  parts.get(r["order_pk"])))
    return out


# ---------------------------------------------------------------------------------------------------------------
# One broker order shared by several legs (one square-off or manual close of a symbol two books hold)
# ---------------------------------------------------------------------------------------------------------------
def external_root(order_pk: str) -> str | None:
    """The broker order id behind an EXTERNAL row: 'EX<id>' and 'EX<id>:<leg_id>' (positions.close_external)
    give <id>; a row with no broker order ('EX:<leg_id>:<n>') gives None."""
    if not (isinstance(order_pk, str) and order_pk.startswith("EX")) or order_pk[2:3] in ("", ":"):
        return None
    return order_pk[2:].split(":", 1)[0]


def _get(row: Any, key: str) -> Any:
    return row[key] if isinstance(row, Mapping) or not hasattr(row, key) else getattr(row, key)


def _shared(rows: Iterable[Any]) -> dict[str, list[Any]]:
    """broker order id -> its EXTERNAL rows with fills, in leg_id order, for ids that more than one leg shares."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        root = external_root(_get(r, "order_pk"))
        if root is not None and str(_get(r, "role")) == Role.EXTERNAL.value and int(_get(r, "filled_qty") or 0) > 0:
            groups[root].append(r)
    return {root: sorted(rs, key=lambda r: (int(_get(r, "leg_id")), _get(r, "order_pk")))
            for root, rs in groups.items() if len({int(_get(r, "leg_id")) for r in rs}) > 1}


def order_parts(store: Store, order_pks: Iterable[str]) -> dict[str, OrderPart]:
    """order_pk -> its charges.OrderPart, for EXTERNAL rows that are one leg's share of a broker order several legs
    share. Read back from titan2.db the way close_external booked them: the rows of one broker order in leg_id
    order cover [0, a), [a, b), ... of the qty they hold together, whose value is the sum of theirs. A share that
    is not fully priced is left as a plain order (its trade is ESTIMATED or UNRESOLVED anyway)."""
    roots = sorted({root for pk in order_pks if (root := external_root(pk)) is not None})
    out: dict[str, OrderPart] = {}
    for root in roots:
        rows = store.query("SELECT order_pk, leg_id, role, side, filled_qty, external_kind FROM orders WHERE "
                           "role = 'EXTERNAL' AND (order_pk = ? OR substr(order_pk, 1, ?) = ?)",
                           ("EX" + root, len(root) + 3, f"EX{root}:"))
        shared = _shared(rows).get(root)
        if not shared:
            continue
        fills = _group_fills(store.effective_fills([r["order_pk"] for r in shared]))
        priced = []
        for r in shared:
            fs = fills.get(r["order_pk"], [])
            if sum(f.qty for f in fs) != r["filled_qty"] or not all(f.px > 0 for f in fs):
                priced = []
                break
            priced.append((r, math.fsum(f.px * f.qty for f in fs)))
        if not priced or len({r["side"] for r, _ in priced}) != 1:
            continue
        total = sum(r["filled_qty"] for r, _ in priced)
        whole = OrderFills(Side(priced[0][0]["side"]), total, round_money(math.fsum(v for _, v in priced)),
                           priced[0][0]["external_kind"])
        start = 0
        for r, _ in priced:
            out[r["order_pk"]] = OrderPart(whole, start, start + r["filled_qty"])
            start += r["filled_qty"]
    return out


def truth_attribution(rows: Iterable[Any]) -> tuple[dict[str, tuple[str, str | None]],
                                                    dict[str, list[tuple[str, int]]]]:
    """The §9.8 truth check's attribution from titan2.db order rows (Order objects or row mappings with order_pk,
    leg_id, book, role, broker_order_id, filled_qty, external_kind): (order_books, parts) for
    recon.eod_truth_check. order_books: broker_order_id -> (book, external_kind) for every executed order with a
    broker id. parts: broker_order_id -> [(book, qty)] in leg_id order, for a broker order several legs share (its
    later rows 'EX<id>:<leg_id>' carry no broker id: orders.broker_order_id is unique)."""
    rows = [r for r in rows if int(_get(r, "filled_qty") or 0) > 0]
    order_books = {_get(r, "broker_order_id"): (_get(r, "book"), _get(r, "external_kind"))
                   for r in rows if _get(r, "broker_order_id")}
    parts: dict[str, list[tuple[str, int]]] = {}
    for root, shared in _shared(rows).items():
        parts[root] = [(_get(r, "book"), int(_get(r, "filled_qty"))) for r in shared]
        if root not in order_books:
            order_books[root] = (_get(shared[0], "book"), _get(shared[0], "external_kind"))
    return order_books, parts


@dataclass(frozen=True, slots=True)
class CarriedTruth:
    """The structures of one earlier session that have a broker order executed today: a leftover the paper exchange
    settled at today's start (the trade is booked to the structure's session, the settlement is today's). recon's
    truth check leaves their broker orders out of today's check and checks them against their own trades: today's
    tradebook rows for `today_ids`, the earlier days' fills from titan2.db (`earlier`; the broker no longer serves
    them), with that session's charges."""

    session: date
    ledger_by_book: Mapping[str, float]               # Σ trades.net of these structures per book (booked ones only)
    order_books: Mapping[str, tuple[str, str | None]]
    parts: Mapping[str, list[tuple[str, int]]]
    today_ids: frozenset[str]                         # their broker orders executed today
    earlier: tuple[BrokerTrade, ...]                  # their other fills, as the broker reported them on their day
    structures: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TruthInputs:
    """recon.eod_truth_check's inputs for one account and session, from titan2.db (§9.8)."""

    ledger_by_book: dict[str, float]
    order_books: dict[str, tuple[str, str | None]]
    parts: dict[str, list[tuple[str, int]]]
    carried: tuple[CarriedTruth, ...] = ()


_TRUTH_COLS = ("o.order_pk, o.leg_id, o.structure_id, o.book, o.role, o.side, o.symbol, o.broker_order_id, "
               "o.filled_qty, o.external_kind, o.created_ts, s.session")


def truth_inputs(store: Store, account: Account | str, session: date) -> TruthInputs:
    """The EOD truth check's view of titan2.db, both sides by structure session (review round 2): today's ledger is
    Σ trades.net of today's structures, and today's attribution comes from those structures' orders. An earlier
    session's structure with a broker order executed today (a leftover settled at today's start) is not today's: it
    comes back as a CarriedTruth per session, so its settlement in today's tradebook is checked against its own
    trade instead of failing today's check. A close with no broker order (EX:<leg_id>:<n>: CLOSE_UNRESOLVED, the
    no-record backstop) makes no structure carried: no tradebook can show it, so its value could only fail the check
    (review round 2 follow-up); it is already reported as unresolved (CLOSED_EXTERNAL, MISMATCH)."""
    acct, day = Account(account).value, session.isoformat()
    ledger_by_book = {r["book"]: float(r["n"]) for r in store.query(
        "SELECT book, SUM(net) AS n FROM trades WHERE account = ? AND session = ? GROUP BY book", (acct, day))}
    rows = store.query(f"SELECT {_TRUTH_COLS} FROM orders o JOIN structures s ON s.structure_id = o.structure_id "
                       "WHERE o.account = ? AND o.filled_qty > 0 AND s.session = ?", (acct, day))
    order_books, parts = truth_attribution(rows)
    sids = sorted({int(r["structure_id"]) for r in store.query(
        "SELECT o.structure_id, o.order_pk, o.broker_order_id FROM orders o JOIN structures s ON "
        "s.structure_id = o.structure_id WHERE o.account = ? AND o.filled_qty > 0 AND substr(o.created_ts, 1, 10) = ? "
        "AND s.session < ?", (acct, day, day)) if r["broker_order_id"] or external_root(r["order_pk"])})
    if not sids:
        return TruthInputs(ledger_by_book, order_books, parts)
    crows = query_in(store, f"SELECT {_TRUTH_COLS} FROM orders o JOIN structures s ON "
                            "s.structure_id = o.structure_id WHERE o.filled_qty > 0 AND o.structure_id IN ({})", sids)
    nets = {int(r["structure_id"]): (r["book"], float(r["net"])) for r in query_in(
        store, "SELECT structure_id, book, net FROM trades WHERE structure_id IN ({})", sids)}
    fills = _group_fills(store.effective_fills([r["order_pk"] for r in crows]))
    carried = []
    for s_day in sorted({r["session"] for r in crows}):
        group = [r for r in crows if r["session"] == s_day]
        ob, pt = truth_attribution(group)
        today_ids, earlier = set(), []
        for r in group:
            bid = r["broker_order_id"] or external_root(r["order_pk"])
            if bid is None:
                continue                              # a close with no broker order: nothing the broker can show
            if str(r["created_ts"])[:10] == day:
                today_ids.add(bid)
                continue
            earlier += [BrokerTrade(Account(acct), f"db:{f.order_pk}:{f.seq_key}", bid, r["symbol"], Side(r["side"]),
                                    f.qty, f.px, f.wall, None) for f in fills.get(r["order_pk"], ())]
        ids = tuple(sorted({int(r["structure_id"]) for r in group}))
        books: dict[str, float] = {}
        for sid in ids:
            if sid in nets:
                book, net = nets[sid]
                books[book] = books.get(book, 0.0) + net
        carried.append(CarriedTruth(date.fromisoformat(str(s_day)), books, ob, pt, frozenset(today_ids),
                                    tuple(earlier), ids))
    return TruthInputs(ledger_by_book, order_books, parts, tuple(carried))


@dataclass(frozen=True, slots=True)
class ExecStats:
    """Execution quality of one structure (§6.7 X2): slippage against the decision walk and the IOC fill rate."""

    entry_value: float          # premium paid: Σ entry fill value
    slip_rs: float              # Σ_entry (fill − walk)·q + Σ_exit (walk − fill)·q; positive = against us
    slip_qty: int               # quantity that had a decision walk
    ioc_qty: int                # Σ qty of bot ENTRY/EXIT orders that reached the broker
    ioc_filled: int             # Σ filled_qty of those orders
    entry_orders: int           # ENTRY orders that reached the broker

    @property
    def slip_pct(self) -> float | None:
        """slip_vs_walk, entry plus exit, as % of premium."""
        return self.slip_rs / self.entry_value * 100.0 if self.entry_value > 0 and self.slip_qty > 0 else None

    @property
    def fill_rate_pct(self) -> float | None:
        return self.ioc_filled / self.ioc_qty * 100.0 if self.ioc_qty > 0 else None


def execution_stats(store: Store, structure_ids: Iterable[int]) -> dict[int, ExecStats]:
    ids = sorted({int(i) for i in structure_ids})
    rows = sorted(query_in(store, "SELECT rowid AS _rowid, order_pk, structure_id, role, side, qty, filled_qty, "
                                  "state, limit_px, external_kind, decision_walk_px FROM orders "
                                  "WHERE structure_id IN ({})", ids), key=lambda r: r["_rowid"])
    fills = _group_fills(store.effective_fills([r["order_pk"] for r in rows if r["filled_qty"] > 0]))
    acc = {i: [0.0, 0.0, 0, 0, 0, 0] for i in ids}
    for r in rows:
        role, side, a = Role(r["role"]), Side(r["side"]), acc[r["structure_id"]]
        if role in (Role.ENTRY, Role.EXIT) and OrderState(r["state"]) in SENT_STATES:
            a[3] += r["qty"]
            a[4] += r["filled_qty"]
            a[5] += role is Role.ENTRY
        if r["filled_qty"] <= 0 or role not in COST_ROLES:
            continue
        v = value_order(r["order_pk"], role, side, r["filled_qty"], r["limit_px"], r["external_kind"],
                        fills.get(r["order_pk"], ()))
        if role in ENTRY_ROLES:
            a[0] += v.value
        walk, vw = r["decision_walk_px"], v.vwap
        if _positive(walk) and vw is not None:
            a[1] += (vw - walk) * v.filled_qty if side is Side.BUY else (walk - vw) * v.filled_qty
            a[2] += v.filled_qty
    return {i: ExecStats(round(a[0], 4), round(a[1], 4), a[2], a[3], a[4], a[5]) for i, a in acc.items()}


# ---------------------------------------------------------------------------------------------------------------
# The trades row
# ---------------------------------------------------------------------------------------------------------------
def _dec_mid(d: Any) -> float | None:
    if d is None:
        return None
    if _positive(d.mid):
        return float(d.mid)
    if _positive(d.bid) and _positive(d.ask) and d.ask >= d.bid:
        return (d.bid + d.ask) / 2.0
    return None


def _dec_spread(d: Any) -> float | None:
    if d is None or not (_positive(d.bid) and _positive(d.ask)) or d.ask < d.bid:
        return None
    return d.ask - d.bid


def build_trade_row(st: Structure, legs: Sequence[Leg], orders: Sequence[Order],
                    fills: Mapping[str, Iterable[Fill]] | Iterable[Fill], *, exit_wall: datetime,
                    pnl_status: PnlStatus | str = PnlStatus.FINAL, exit_reason: str | None = None,
                    mode: str | None = None, rates: RateSet | None = None,
                    thin_book: bool | None = None, parts: Mapping[str, OrderPart] | None = None) -> dict[str, Any]:
    """The `trades` row for st, ready for store.insert_trade().

    orders: every order of st's legs, bot and EXTERNAL, in creation order. fills: their effective fills (a flat
    list as store.effective_fills() returns it, or order_pk -> fills). exit_wall: the close time, used as exit_ts
    when no exit fill carries a time. pnl_status: the caller's view (close(): FINAL; close_external(): FINAL,
    ESTIMATED or UNRESOLVED), only ever made worse here. mode: the strategy mode of the trade (default: the
    account). rates: default rates_for(session). thin_book: default True when a bot EXIT order was under-filled.
    parts: order_pk -> charges.OrderPart for EXTERNAL rows that are this leg's share of a broker order shared with
    other legs (close_external): the trade books that share of the order's value and charges.
    """
    legs = sorted(legs, key=lambda lg: lg.leg_no)
    if not legs:
        raise ValueError(f"structure {st.structure_id}: no legs to book")
    leg_ids = {lg.leg_id for lg in legs}
    for lg in legs:
        if lg.structure_id != st.structure_id:
            raise ValueError(f"leg {lg.leg_id} belongs to structure {lg.structure_id}, not {st.structure_id}")
    for o in orders:
        if o.structure_id != st.structure_id or o.leg_id not in leg_ids:
            raise ValueError(f"order {o.order_pk} is not an order of structure {st.structure_id}")
    given = {o.order_pk for o in orders}
    for lg in legs:
        missing = [pk for pk in lg.orders if pk not in given]
        if missing:                    # a partial order list would book a wrong, immutable P&L
            raise ValueError(f"leg {lg.leg_id}: orders {missing} were not passed to build_trade_row")
    by_pk = _group_fills(fills)
    parts = parts or {}
    values = [value_order(o.order_pk, o.role, o.side, o.filled_qty, o.limit_px, o.external_kind,
                          by_pk.get(o.order_pk, ()), parts.get(o.order_pk)) for o in orders]
    r = rates if rates is not None else rates_for(st.session)

    entry = [v for v in values if v.role in ENTRY_ROLES and v.filled_qty > 0]
    qty = sum(v.filled_qty for v in entry)
    if qty <= 0:
        raise ValueError(f"structure {st.structure_id}: no entry fills; a structure without fills is VOID, "
                         "never booked")
    entry_value = math.fsum(v.value for v in entry)
    exits = [v for v in values if _is_exit(v.role, v.side) and v.filled_qty > 0]
    exit_qty = sum(v.filled_qty for v in exits)
    pnl = net_pnl([of for v in values if (of := v.order_fills()) is not None], r)

    status = PnlStatus(pnl_status)
    for v in values:
        status = worse_status(status, v.status)
    bought = sum(v.filled_qty for v in values if v.side is Side.BUY)
    sold = sum(v.filled_qty for v in values if v.side is Side.SELL)
    if bought != sold:                 # the structure's own fills do not show it flat
        status = PnlStatus.UNRESOLVED

    leg = legs[0]
    entry_walls = [v.first_wall for v in entry if v.first_wall is not None]
    entry_ts = min(entry_walls) if entry_walls else (leg.entry_fill_wall or st.opened_wall or st.reserved_wall)
    exit_walls = [v.last_wall for v in exits if v.last_wall is not None]
    exit_ts = max(exit_walls) if exit_walls else exit_wall
    risk = round_money(st.risk_basis_rs) if _positive(st.risk_basis_rs) else 0.0
    if risk <= 0:                      # P must be > 0 (DDL CHECK): fall back to the premium actually paid
        risk = round_money(entry_value)
    entry_avg = entry_value / qty

    # Decision-quote costs (G1's C, the daily report's slippage). EXTERNAL orders have no decision quote.
    hs = cross = 0.0
    entry_dec = None
    exit_parts: list[tuple[float, float | None, float, int]] = []    # (mid, spread %, vwap, qty)
    ltp_exit = None
    for o, v in zip(orders, values):
        d = o.decision
        if v.role in ENTRY_ROLES and d is not None and entry_dec is None:
            entry_dec = d
        if v.role is Role.EXIT and d is not None and d.ltp is not None:
            ltp_exit = d.ltp
        if d is None or v.filled_qty <= 0 or v.role not in COST_ROLES:
            continue
        spread, mid, vw = _dec_spread(d), _dec_mid(d), v.vwap
        if spread is not None:
            hs += v.filled_qty * spread / 2.0
        if mid is not None and vw is not None:
            cross += (vw - mid) * v.filled_qty if v.side is Side.BUY else (mid - vw) * v.filled_qty
            if v.role is Role.EXIT:
                exit_parts.append((mid, None if spread is None else spread / mid * 100.0, vw, v.filled_qty))
    e_mid, e_spread = _dec_mid(entry_dec), _dec_spread(entry_dec)
    xq = sum(p[3] for p in exit_parts)
    spread_parts = [(p[1], p[3]) for p in exit_parts if p[1] is not None]
    exit_mid_w = math.fsum(p[0] * p[3] for p in exit_parts) / xq if xq else None
    exit_spread_w = (math.fsum(s * q for s, q in spread_parts) / sum(q for _, q in spread_parts)
                     if spread_parts else None)
    exit_slip = (math.fsum((p[0] - p[2]) * p[3] for p in exit_parts) / math.fsum(p[0] * p[3] for p in exit_parts)
                 * 100.0 if xq else None)

    if exit_reason is None:
        exit_reason = st.exit_reason or next((lg.exit_reason for lg in legs if lg.exit_reason), None)
    if exit_reason is None:
        kinds = {v.external_kind for v in values if v.role is Role.EXTERNAL}
        exit_reason = ("BROKER_SQUAREOFF" if "AUTO_SQUAREOFF" in kinds else "CLOSED_EXTERNAL" if kinds
                       else "UNKNOWN")
    sent = [(v, OrderState(o.state)) for o, v in zip(orders, values)]
    if thin_book is None:
        thin_book = any(v.role is Role.EXIT and s in (OrderState.COMPLETE, OrderState.CANCELLED)
                        and o.filled_qty < o.qty for o, (v, s) in zip(orders, sent))
    profile = st.exit_params.profile if st.exit_params is not None else ExitProfile()
    mfe = leg.peak_rb / entry_avg * 100.0 - 100.0 if _positive(leg.peak_rb) else None
    mae = leg.trough_rb / entry_avg * 100.0 - 100.0 if _positive(leg.trough_rb) else None
    inst = leg.inst
    return {
        "structure_id": st.structure_id, "session": st.session, "book": st.book, "account": Account(st.account),
        "mode": mode or Account(st.account).value, "strategy": st.strategy, "strategy_version": st.strategy_version,
        "intent_id": st.intent.intent_id, "is_placebo": bool(st.is_placebo),
        "parent_structure_id": st.parent_structure_id, "blocked_by": st.blocked_by, "underlying": st.underlying,
        "symbol": inst.symbol, "structure_type": StructureType(st.type), "direction": Direction(st.intent.direction),
        "opt_type": inst.opt_type, "strike": inst.strike, "expiry": inst.expiry, "lot_size": inst.lot_size,
        "qty": qty, "entry_ts": entry_ts, "exit_ts": exit_ts,
        "hold_s": round(max(0.0, (exit_ts - entry_ts).total_seconds()), 3),
        "entry_avg": round(entry_avg, 4),
        "exit_avg": round(math.fsum(v.value for v in exits) / exit_qty, 4) if exit_qty else None,
        "risk_basis_rs": risk, "gross": pnl.gross,
        **{name: getattr(pnl.charges, name) for name in CHARGE_FIELDS},
        "charges": pnl.charges.total, "net": pnl.net, "net_pct": round(pnl.net / risk * 100.0, 4) + 0.0,
        "rates_version": pnl.rates_version,
        "n_entry_orders": sum(1 for v, s in sent if v.role in ENTRY_ROLES and s is not OrderState.NOT_PLACED),
        "n_exit_orders": sum(1 for v, s in sent if (v.role is Role.EXIT and s is not OrderState.NOT_PLACED)
                             or (v.role is Role.EXTERNAL and v.side is Side.SELL)),
        "n_executed_orders": pnl.n_executed_orders,
        "entry_bid": None if entry_dec is None else entry_dec.bid,
        "entry_ask": None if entry_dec is None else entry_dec.ask,
        "entry_mid": _r(e_mid),
        "entry_spread_pct": _r(e_spread / e_mid * 100.0) if e_spread is not None and e_mid else None,
        "entry_slip_vs_mid_pct": _r((entry_avg / e_mid - 1.0) * 100.0) if e_mid else None,
        "entry_walk_px": None if entry_dec is None else entry_dec.walk_px,
        "exit_mid_w": _r(exit_mid_w), "exit_spread_pct_w": _r(exit_spread_w), "exit_slip_vs_mid_pct": _r(exit_slip),
        "hs_cost_rs": round_money(hs), "crossing_cost_rs": round_money(cross),
        "mfe_pct": _r(mfe), "mfe_ts": leg.peak_wall if mfe is not None else None,
        "mae_pct": _r(mae), "mae_ts": leg.trough_wall if mae is not None else None,
        "lock_armed": any(lg.lock_armed for lg in legs), "exit_reason": exit_reason, "pnl_status": status,
        "ltp_entry": None if entry_dec is None else entry_dec.ltp, "ltp_exit": ltp_exit,
        "thin_book": bool(thin_book), "exit_profile": asdict(profile),
    }


# ---------------------------------------------------------------------------------------------------------------
# pub/ (the dashboard's only input) and the EOD exports
# ---------------------------------------------------------------------------------------------------------------
def _fsync_dir(d: Path) -> None:
    if os.name == "posix":               # Windows cannot open a directory to fsync it
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace(tmp: str, path: Path) -> None:
    """os.replace; on Windows a reader holding the target (the dashboard, an indexer, antivirus) makes it fail with
    PermissionError for a moment, so it is retried a few times (at most ~0.15 s). POSIX renames never need it."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 >= _REPLACE_TRIES:
                raise
            time.sleep(_REPLACE_BACKOFF_S * 2 ** attempt)


def _atomic_write(path: Path, data: bytes, mode: int = PUB_FILE_MODE) -> Path:
    """Write data to path: temp file in the same directory, fsync, chmod, os.replace, fsync the directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        _replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    return path


def write_pub_json(pub_dir: str | os.PathLike, rel: str, obj: Any) -> Path:
    """Atomically publish obj as pub/<rel> (JSON: sorted keys, NaN refused)."""
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or ".." in parts or "\\" in rel or ":" in rel:
        raise ValueError(f"pub path must be relative and stay inside pub/: {rel!r}")
    return _atomic_write(Path(pub_dir).joinpath(*parts), (db_json(obj) + "\n").encode("utf-8"))


def publish_trades_today(store: Store, pub_dir: str | os.PathLike, session: date, now_wall: datetime) -> Path:
    """pub/trades_today.json, on each close: today's ledger rows."""
    return write_pub_json(pub_dir, "trades_today.json", {"ts": iso(now_wall), "session": session.isoformat(),
                                                          "trades": store.trades(session=session)})


def publish_trades_90d(store: Store, pub_dir: str | os.PathLike, session: date, now_wall: datetime,
                       days: int = TRADES_90D_DAYS) -> Path:
    """pub/trades_90d.json, at EOD: ledger rows of the last `days` calendar days up to session."""
    since = session - timedelta(days=days - 1)
    return write_pub_json(pub_dir, "trades_90d.json", {
        "ts": iso(now_wall), "since": since.isoformat(), "until": session.isoformat(),
        "trades": store.trades(since=since, until=session)})


def quote_path_1m(store: Store, leg_ids: Iterable[int]) -> list[dict[str, Any]]:
    """The 1-min premium path from quote_checks (HOLD while held, POST in the post-exit tail), per leg and phase."""
    rows = query_in(store, "SELECT * FROM quote_checks WHERE leg_id IN ({}) ORDER BY ts, id", sorted(set(leg_ids)))
    out: dict[tuple[int, str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["leg_id"], r["ts"][:16], r["phase"])
        b = out.get(key)
        if b is None:
            b = out[key] = {"leg_id": r["leg_id"], "minute": r["ts"][:16], "phase": r["phase"], "n": 0,
                            "bid_open": None, "bid_high": None, "bid_low": None, "bid_close": None}
        b["n"] += 1
        bid = r["bid"]
        if bid is not None:
            b["bid_open"] = bid if b["bid_open"] is None else b["bid_open"]
            b["bid_high"] = bid if b["bid_high"] is None else max(b["bid_high"], bid)
            b["bid_low"] = bid if b["bid_low"] is None else min(b["bid_low"], bid)
            b["bid_close"] = bid
        b.update(ask_close=r["ask"], rb_close=r["rb"], stop_px=r["stop_px"], lock_armed=r["lock_armed"],
                 decision=r["decision"], ltp_info=r["ltp"])
    return list(out.values())


def trade_detail(store: Store, structure_id: int) -> dict[str, Any]:
    """Orders, fills (all of them, each marked effective or not) and the 1-min quote path of one structure."""
    srow = store.query_one("SELECT * FROM structures WHERE structure_id=?", (structure_id,))
    if srow is None:
        raise KeyError(f"no structure {structure_id}")
    structure = dict(srow)
    for col in ("decision_json", "exit_params"):
        if structure.get(col) is not None:
            structure[col] = json.loads(structure[col])
    legs = [dict(r) for r in store.query("SELECT * FROM legs WHERE structure_id=? ORDER BY leg_no", (structure_id,))]
    orders = [dict(r) for r in store.query("SELECT * FROM orders WHERE structure_id=? ORDER BY rowid",
                                           (structure_id,))]
    pks = [o["order_pk"] for o in orders]
    effective = {r["fill_id"] for r in query_in(store, "SELECT fill_id FROM effective_fills WHERE order_pk IN ({})",
                                                 pks)}
    fills = [dict(r) | {"effective": r["fill_id"] in effective}
             for r in query_in(store, "SELECT * FROM fills WHERE order_pk IN ({}) ORDER BY fill_id", pks)]
    fills.sort(key=lambda f: f["fill_id"])
    return {"structure": structure, "trade": store.trade(structure_id), "legs": legs, "orders": orders,
            "fills": fills, "path_1m": quote_path_1m(store, [lg["leg_id"] for lg in legs])}


def publish_trade_detail(store: Store, pub_dir: str | os.PathLike, structure_id: int, now_wall: datetime) -> Path:
    """pub/trade/<structure_id>.json, on close."""
    return write_pub_json(pub_dir, f"trade/{int(structure_id)}.json",
                          {"ts": iso(now_wall), **trade_detail(store, structure_id)})


def publish_events_tail(store: Store, pub_dir: str | os.PathLike, now_wall: datetime, *, last_seq: int | None = None,
                        n: int = EVENTS_TAIL_N, debug: bool = False) -> int:
    """pub/events_tail.json (the last n events), written only when a newer event exists than last_seq. DEBUG rows
    (the minute's HEARTBEAT and LATENCY) are left out unless debug: at the 10-14 close they were 227 of the last
    500, and the morning's refusals had already dropped out of the dashboard (2026-10-14 night).

    Returns the newest seq published, which the caller passes back as last_seq next time."""
    events = store.events_tail(n, debug=debug)
    top = events[-1]["seq"] if events else 0
    if last_seq is not None and top == last_seq:
        return last_seq
    write_pub_json(pub_dir, "events_tail.json", {"ts": iso(now_wall), "events": events})
    return top


def publish_acks(store: Store, pub_dir: str | os.PathLike, now_wall: datetime, n: int = ACKS_N) -> Path:
    """pub/acks.json, on change: the last n command acks."""
    return write_pub_json(pub_dir, "acks.json", {"ts": iso(now_wall), "acks": store.command_acks(n)})


def publish_g1(pub_dir: str | os.PathLike, scorecards: Iterable[Mapping[str, Any]], now_wall: datetime) -> Path:
    """pub/g1.json, on every G1 report: the per-strategy scorecards that g1.scorecards() builds."""
    return write_pub_json(pub_dir, "g1.json", {"ts": iso(now_wall), "strategies": [dict(c) for c in scorecards]})


def _jsonl_gz(lines: Iterable[Any]) -> bytes:
    text = "".join(db_json(x) + "\n" for x in lines)
    return gzip.compress(text.encode("utf-8"), mtime=0)


# Ported from agentic_trader/trade_ledger.py:457-481 @b4694f3 — changes: one gzip JSONL file per session and kind,
#   written from the DB at EOD (temp file, fsync, rename) instead of an append per event; errors raise instead of
#   being printed; the DB, not the file, is the record
def export_session(store: Store, exports_dir: str | os.PathLike, session: date) -> dict[str, Path]:
    """exports/events/<date>.jsonl.gz and exports/trades/<date>.jsonl.gz for one session (§4.6). A re-run
    replaces the files with the same content."""
    base = Path(exports_dir)
    day = session.isoformat()
    events = [{k: v for k, v in e.items() if v is not None and k != "mono"} for e in store.events(session=session)]
    return {
        "events": _atomic_write(base / "events" / f"{day}.jsonl.gz", _jsonl_gz(events)),
        "trades": _atomic_write(base / "trades" / f"{day}.jsonl.gz", _jsonl_gz(store.trades(session=session))),
    }


# ---------------------------------------------------------------------------------------------------------------
# The daily report (§12.5, 15:38)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DailyReport:
    session: date
    data: dict[str, Any]
    markdown: str
    telegram: str


def _pctl(xs: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not xs:
        return None
    s = sorted(xs)
    return s[max(0, math.ceil(p / 100.0 * len(s)) - 1)]


def _ms_between(a: str | None, b: str | None) -> float | None:
    if a is None or b is None:
        return None
    return (parse_wall(b) - parse_wall(a)).total_seconds() * 1000.0


def _book_stats(store: Store, session: date) -> list[dict[str, Any]]:
    rows = store.trades(session=session)
    by_book: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        by_book[t["book"]].append(t)
    ex = execution_stats(store, [t["structure_id"] for t in rows])
    out = []
    for book in sorted(by_book):
        ts = by_book[book]
        nets = [t["net"] for t in ts]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        worst = min(ts, key=lambda t: (t["net"], t["structure_id"]))
        st = [ex[t["structure_id"]] for t in ts]
        slips = [s.slip_pct for s in st if s.slip_pct is not None]
        ioc_qty = sum(s.ioc_qty for s in st)
        spreads = [t["entry_spread_pct"] for t in ts if t["entry_spread_pct"] is not None]
        entry_slips = [t["entry_slip_vs_mid_pct"] for t in ts if t["entry_slip_vs_mid_pct"] is not None]
        exit_slips = [t["exit_slip_vs_mid_pct"] for t in ts if t["exit_slip_vs_mid_pct"] is not None]
        out.append({
            "book": book, "strategy": ts[0]["strategy"], "account": ts[0]["account"], "n": len(ts),
            "wins": len(wins), "wr_net_pct": round(len(wins) / len(ts) * 100.0, 1),
            "exp_net_rs": round_money(_mean(nets)), "exp_net_pct": _r(_mean([t["net_pct"] for t in ts]), 3),
            "net_rs": round_money(math.fsum(nets)), "charges_rs": round_money(math.fsum(t["charges"] for t in ts)),
            "avg_win_rs": None if not wins else round_money(_mean(wins)),
            "avg_loss_rs": None if not losses else round_money(_mean(losses)),
            "worst": {"structure_id": worst["structure_id"], "net": worst["net"], "symbol": worst["symbol"],
                      "exit_reason": worst["exit_reason"]},
            "exit_mix": dict(sorted(Counter(t["exit_reason"] for t in ts).items())),
            "pnl_status": dict(sorted(Counter(t["pnl_status"] for t in ts).items())),
            "entry_slip_vs_mid_pct": _r(_mean(entry_slips), 3), "exit_slip_vs_mid_pct": _r(_mean(exit_slips), 3),
            "slip_vs_walk_pct": _r(_mean(slips), 3),
            "ioc_fill_rate_pct": _r(sum(s.ioc_filled for s in st) / ioc_qty * 100.0, 1) if ioc_qty else None,
            "entry_spread_pct": {"p50": _r(_pctl(spreads, 50), 3), "p95": _r(_pctl(spreads, 95), 3),
                                 "max": _r(max(spreads), 3) if spreads else None},
            "thin_book": sum(1 for t in ts if t["thin_book"]),
        })
    return out


def _refusals(store: Store, session: date) -> dict[str, Any]:
    routes = store.query(
        "SELECT r.book, r.outcome, COALESCE(r.gate, '') AS gate, COUNT(*) AS n FROM intent_routes r "
        "JOIN intents i ON i.intent_id = r.intent_id WHERE i.session = ? AND r.outcome <> 'RESERVED' "
        "GROUP BY r.book, r.outcome, r.gate ORDER BY r.book, n DESC, gate", (session,))
    voids: dict[tuple[str, str], int] = Counter()
    for r in store.query("SELECT book, void_reason FROM structures WHERE session = ? AND state = 'VOID'", (session,)):
        voids[(r["book"], (r["void_reason"] or "").split(":", 1)[0] or "UNKNOWN")] += 1
    # Why paper orders did not (fully) fill, in the paper exchange's words: no fresh depth, a crossed or locked
    #   book or one older than the decision (paper model 2), IOC unfilled, a band or funds reject ...
    sims: dict[tuple[str, str], int] = Counter()
    for r in store.query("SELECT o.book, o.status_msg FROM orders o JOIN structures s ON s.structure_id = "
                         "o.structure_id WHERE s.session = ? AND o.role <> 'EXTERNAL' AND o.state IN "
                         "('CANCELLED', 'REJECTED') AND substr(o.status_msg, 1, 4) = 'SIM:'", (session,)):
        sims[(r["book"], _sim_reason(r["status_msg"]))] += 1
    return {"routes": [dict(r) for r in routes],
            "void": [{"book": b, "reason": g, "n": n} for (b, g), n in sorted(voids.items())],
            "sim": [{"book": b, "reason": g, "n": n} for (b, g), n in sorted(sims.items())]}


_SIM_NUMBER = re.compile(r"\s*\d[\d.,/]*")


def _sim_reason(msg: str) -> str:
    """A paper order's status text without its own details: 'SIM: IOC partial fill 100/200' -> 'SIM: IOC partial
    fill', 'SIM: insufficient funds: need Rs ...' -> 'SIM: insufficient funds'."""
    return _SIM_NUMBER.sub("", ": ".join(msg.split(": ", 2)[:2])).strip()


def _halts(store: Store, session: date) -> list[dict[str, Any]]:
    return [dict(r) for r in store.query("SELECT halt_id, scope, kind, reason, clears, created_ts, cleared_ts, "
                                         "cleared_by FROM halts WHERE session = ? OR cleared_ts IS NULL "
                                         "ORDER BY halt_id", (session,))]


def _event_counts(store: Store, session: date) -> list[dict[str, Any]]:
    return [dict(r) for r in store.query(
        "SELECT kind, severity, COUNT(*) AS n FROM events WHERE session = ? AND severity IN ('WARN','CRITICAL') "
        "GROUP BY kind, severity ORDER BY severity, n DESC, kind", (session,))]


def _twins(store: Store, session: date) -> list[dict[str, Any]]:
    """Paper twins of intents routed to LIVE:<s> today: live coverage and the live-minus-paper slip gap."""
    rows = store.query(
        "SELECT t.strategy, t.structure_id AS paper_id, (SELECT lt.structure_id FROM trades lt "
        "WHERE lt.intent_id = t.intent_id AND lt.book = 'LIVE:' || t.strategy) AS live_id FROM trades t "
        "WHERE t.session = ? AND t.book = 'PAPER:' || t.strategy AND EXISTS (SELECT 1 FROM intent_routes r "
        "WHERE r.intent_id = t.intent_id AND r.book = 'LIVE:' || t.strategy) ORDER BY t.structure_id", (session,))
    ex = execution_stats(store, [r[k] for r in rows for k in ("paper_id", "live_id") if r[k] is not None])
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        o = out.setdefault(r["strategy"], {"strategy": r["strategy"], "twins": 0, "live": 0, "gaps": []})
        o["twins"] += 1
        if r["live_id"] is not None:
            o["live"] += 1
            lp, pp = ex[r["live_id"]].slip_pct, ex[r["paper_id"]].slip_pct
            if lp is not None and pp is not None:
                o["gaps"].append(lp - pp)
    res = []
    for o in out.values():
        gaps = sorted(o.pop("gaps"))
        res.append({**o, "live_coverage_pct": round(o["live"] / o["twins"] * 100.0, 1),
                    "slip_gap_pp_median": _r(_pctl(gaps, 50), 3), "pairs_with_slip": len(gaps)})
    return res


def _g1_progress(store: Store) -> list[dict[str, Any]]:
    out = []
    for m in store.strategy_modes():
        trial = store.trial(m["strategy"], m["version"])
        rep = store.query_one("SELECT * FROM g1_reports WHERE strategy = ? AND version = ? AND kind = 'PROGRESS' "
                              "ORDER BY report_id DESC LIMIT 1", (m["strategy"], m["version"]))
        detail = json.loads(rep["detail"]) if rep is not None else {}
        done = set(trial["looks_done"]) if trial is not None else set()
        out.append({
            "strategy": m["strategy"], "version": m["version"], "mode": m["mode"],
            "trial": None if trial is None else trial["state"],
            "n": None if rep is None else rep["n"], "sessions": None if rep is None else rep["sessions"],
            "exp_pct": None if rep is None else rep["exp_pct"], "lb_pct": None if rep is None else rep["lb_pct"],
            "next_look": (next((x for x in G1_LOOKS if x not in done), None)
                          if trial is not None and trial["state"] == "COLLECTING" else None),
            "futility_ub_pct": detail.get("futility_ub_pct"),
        })
    return out


def _checks(store: Store, session: date) -> dict[str, Any]:
    vcn = Counter(r["kind"] for r in store.query("SELECT kind FROM events WHERE session = ? AND kind IN "
                                                 "('CHARGES_CHECK','CHARGES_DRIFT')", (session,)))
    bars = store.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(ok = 0), 0) AS bad FROM bar_checks "
                           "WHERE session = ?", (session,))
    why: Counter = Counter()                       # the mismatches' reasons (VOLUME, OPEN, LIVE_MISSING, ...)
    for r in store.query("SELECT detail FROM bar_checks WHERE session = ? AND ok = 0", (session,)):
        try:
            why.update(json.loads(r["detail"]).get("reasons") or ["?"])
        except (TypeError, ValueError, AttributeError):
            why.update(["?"])
    truth = []
    for r in store.query("SELECT account, payload FROM events WHERE session = ? AND kind = 'EOD_TRUTH_CHECK' "
                         "ORDER BY seq", (session,)):
        p = json.loads(r["payload"])
        truth.append({"account": r["account"] or p.get("account"), "ok": p.get("ok"),
                      "ledger_net": p.get("ledger_net"), "broker_net": p.get("broker_net")})
    return {"vcn": dict(vcn), "bar_checks": {"n": bars["n"], "mismatched": bars["bad"],
                                             "reasons": dict(sorted(why.items(), key=lambda kv: (-kv[1], kv[0])))},
            "eod_truth": truth}


def _latencies(store: Store, session: date) -> dict[str, Any]:
    rows = store.query(
        "SELECT o.role, o.created_ts, o.submitted_ts, o.acked_ts, o.terminal_ts, o.decision_quote_age_ms, "
        "i.created_ts AS intent_ts FROM orders o JOIN structures s ON s.structure_id = o.structure_id "
        "JOIN intents i ON i.intent_id = s.intent_id WHERE substr(o.created_ts, 1, 10) = ? AND o.role <> 'EXTERNAL'",
        (session,))
    lat: dict[str, list[float]] = {"intent_to_submit_ms": [], "submit_to_ack_ms": [], "ack_to_terminal_ms": []}
    ages = []
    for r in rows:
        sub = r["submitted_ts"] or r["created_ts"]
        if r["role"] == "ENTRY" and (x := _ms_between(r["intent_ts"], sub)) is not None:
            lat["intent_to_submit_ms"].append(x)
        if (x := _ms_between(r["submitted_ts"], r["acked_ts"])) is not None:
            lat["submit_to_ack_ms"].append(x)
        if (x := _ms_between(r["acked_ts"], r["terminal_ts"])) is not None:
            lat["ack_to_terminal_ms"].append(x)
        if r["decision_quote_age_ms"] is not None:
            ages.append(float(r["decision_quote_age_ms"]))
    # exit decision -> its first EXIT order sent (the Stage 2 exit criterion, p95 < 1.5 s; decision B1 makes it wait
    #   for a positions read requested after the decision). Wall times, so a restart in between cannot hide the gap.
    lat["exit_decision_to_submit_ms"] = []
    for r in store.query(
            "SELECT l.exiting_since_ts AS decided_ts, (SELECT o.submitted_ts FROM orders o WHERE o.leg_id = l.leg_id "
            "AND o.role = 'EXIT' AND o.submitted_ts IS NOT NULL ORDER BY o.attempt LIMIT 1) AS first_exit_ts "
            "FROM legs l WHERE l.session = ? AND l.exiting_since_ts IS NOT NULL", (session,)):
        if (x := _ms_between(r["decided_ts"], r["first_exit_ts"])) is not None:
            lat["exit_decision_to_submit_ms"].append(x)
    out: dict[str, Any] = {k: {"n": len(v), "p50": _r(_pctl(v, 50), 1), "p95": _r(_pctl(v, 95), 1)}
                           for k, v in lat.items()}
    out["decision_quote_age_ms_p95"] = _r(_pctl(ages, 95), 1)
    beats = [parse_wall(r["ts"]) for r in store.query("SELECT ts FROM events WHERE session = ? AND kind = "
                                                      "'HEARTBEAT' ORDER BY seq", (session,))]
    gaps = [(b - a).total_seconds() for a, b in zip(beats, beats[1:])]
    stalls = [g for g in gaps if g > 2 * HEARTBEAT_EVERY_S]
    out["loop_stalls"] = {"n": len(stalls), "max_gap_s": _r(max(gaps), 1) if gaps else None}
    measured = _measured_latencies(store, session)
    rows = measured.pop("rows")
    out.update({k: {"n": len(v), "p50": _r(_pctl(v, 50), 1), "p95": _r(_pctl(v, 95), 1)} for k, v in rows.items()})
    out["max_ms"] = {k: _r(max(v), 1) for k, v in {**lat, **rows}.items() if v}
    out.update(measured)
    return out


def _measured_latencies(store: Store, session: date) -> dict[str, Any]:
    """The LATENCY events' measurements (2026-10-14 night; the engine writes one a minute): the quote lane's calls
    and every broker read's round trip. rows: the latency table's new rows, quote_call_ms (the whole call, the wait
    for the 1-a-second token included: what a decision waited), quote_kite_ms (Kite's own answer time) and
    read_ms:<ACCOUNT>:<KIND> (the engine's send to its answer); quote_calls: the counts the latency section's line
    gives; read_failures: failed reads per account and kind."""
    calls: list[dict[str, Any]] = []
    reads: dict[str, list[float]] = defaultdict(list)
    failed: Counter = Counter()
    for r in store.query("SELECT payload FROM events WHERE session = ? AND kind = 'LATENCY' ORDER BY seq",
                         (session,)):
        try:
            p = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict):
            continue
        qc, rd, rf = p.get("quote_calls"), p.get("reads"), p.get("read_failures")
        calls += [c for c in qc if isinstance(c, dict)] if isinstance(qc, list) else []
        for key, ms in (rd.items() if isinstance(rd, dict) else ()):
            if isinstance(ms, list):
                reads[str(key)] += [float(x) for x in ms if isinstance(x, (int, float)) and not isinstance(x, bool)
                                    and math.isfinite(x) and x >= 0]
        failed.update({str(k): v for k, v in (rf.items() if isinstance(rf, dict) else ())
                       if isinstance(v, int) and not isinstance(v, bool)})
    quotes = [c for c in calls if c.get("kind", "QUOTE") == "QUOTE"]
    warm = [c for c in calls if c.get("kind") == "WARM"]

    def num(c: Mapping[str, Any], key: str) -> float | None:
        x = c.get(key)
        return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None

    def bad(c: Mapping[str, Any]) -> bool:
        return not str(c.get("outcome", "")).startswith(("OK", "MISSING"))

    cold = [c for c in quotes if num(c, "idle_s") is None or num(c, "idle_s") >= QUOTE_COLD_S]
    rows: dict[str, list[float]] = {
        "quote_call_ms": [x for c in quotes if (x := num(c, "ms")) is not None],
        "quote_kite_ms": [x for c in quotes if (x := num(c, "kite_ms")) is not None]}
    rows.update({f"read_ms:{key}": v for key, v in sorted(reads.items())})
    cold_ms = [x for c in cold if (x := num(c, "ms")) is not None]
    summary = {"n": len(quotes), "cold": len(cold),
               "cold_pct": _r(len(cold) / len(quotes) * 100.0, 1) if quotes else None,
               "cold_p50_ms": _r(_pctl(cold_ms, 50), 1), "failed": sum(1 for c in quotes if bad(c)),
               "failures": dict(sorted(Counter(str(c.get("outcome")) for c in quotes if bad(c)).items())),
               "missing": sum(1 for c in quotes if str(c.get("outcome", "")).startswith("MISSING")),
               "retried": sum(1 for c in quotes if c.get("retried") is True),
               "keep_warm": len(warm), "keep_warm_failed": sum(1 for c in warm if bad(c))}
    return {"rows": rows, "quote_calls": summary, "read_failures": dict(sorted(failed.items()))}


def _g1_today(store: Store, session: date, rows: Sequence[dict[str, Any]]) -> None:
    """Each G1 row's "today": whether today counts for G1 as a clean day, in plain words (2026-10-14 night; display
    only): yes; or no, with why: a mode G1 does not test, no running trial, a PAPER mismatch today (G1 fails every
    later look of this version) or an IMPAIRED mark (its trades still count; the day uses the allowance)."""
    from titan2 import g1 as g1_mod                    # g1 imports this module: imported at call time

    day = session.isoformat()
    flags = {r["strategy"]: r["detail"] for r in store.query(
        "SELECT strategy, detail FROM session_flags WHERE session = ? AND flag = 'IMPAIRED'", (day,))}
    for g in rows:
        s = g["strategy"]
        try:
            if g["mode"] not in ("PAPER", "LIVE"):
                g["today"] = f"no (mode {g['mode']}: G1 tests PAPER and LIVE strategies only)"
            elif g["trial"] not in ("COLLECTING", "CONFIRMING"):
                g["today"] = f"no (no running G1 trial: {g['trial'] or 'none registered'})"
            elif (n := g1_mod.count_mismatches(store, g1_mod.paper_book(s), Account.PAPER.value, day, day)):
                g["today"] = (f"no ({n} PAPER mismatch{'es' if n > 1 else ''} today: G1 fails every later look of "
                              "this version)")
            elif s in flags:
                g["today"] = f"no (marked IMPAIRED: {flags[s]}; its trades still count, the day uses the allowance)"
            else:
                g["today"] = "yes"
        except Exception as exc:                   # display only: never a reason to lose the report
            g["today"] = f"unknown ({type(exc).__name__}; titan2 g1 {s} shows the trial)"


def _whatif_totals(reports_dir: str | os.PathLike | None, session: date) -> dict[str, Any] | None:
    """The saved what-if days up to `session` (reports/whatif-<date>.json, which `titan2 whatif` writes after each
    session): days, refused entries replayed, winners and their net. At 15:38 today's has not run yet (today: False).
    None without a reports folder."""
    if reports_dir is None:
        return None
    days, n, won, net, today = 0, 0, 0, 0.0, False
    for path in sorted(Path(reports_dir).glob(f"{WHATIF_PREFIX}*.json")):
        day = path.stem[len(WHATIF_PREFIX):]
        if day > session.isoformat():
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
        except (OSError, ValueError, AttributeError):
            continue
        days += 1
        today |= day == session.isoformat()
        for r in rows if isinstance(rows, list) else ():
            o = r.get("outcome") if isinstance(r, dict) else None
            x = o.get("net") if isinstance(o, dict) else None
            if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x):
                n, won, net = n + 1, won + (x > 0), net + float(x)
    return {"days": days, "n": n, "won": won, "net": round_money(net), "today": today}


def daily_report(store: Store, session: date, now_wall: datetime, *, reports_dir: str | os.PathLike | None = None,
                 alerts: Mapping[str, int] | None = None, notes: Mapping[str, str] | None = None) -> DailyReport:
    """The 15:38 report: per book and strategy (n, WR_net, Exp_net, worst trade, charges, exit mix, slippage),
    refusals by gate (and paper orders not filled, by SIM reason), halts, WARN/CRITICAL events, paper-live twin
    gaps, G1 progress (and whether today counts for G1), checks and latencies (the quote calls and broker read
    round trips included). Given by the engine, all display only: reports_dir (the saved what-if totals), alerts (the
    alerter's counters) and notes (one line each from outside the engine: the laptop's condor)."""
    g1_rows = _g1_progress(store)
    _g1_today(store, session, g1_rows)
    data = {
        "session": session.isoformat(), "generated": iso(now_wall), "rates_version": rates_for(session).version,
        "books": _book_stats(store, session), "refusals": _refusals(store, session), "halts": _halts(store, session),
        "events": _event_counts(store, session), "twins": _twins(store, session), "g1": g1_rows,
        "checks": _checks(store, session), "latency": _latencies(store, session),
        "whatif": _whatif_totals(reports_dir, session), "alerts": None if alerts is None else dict(alerts),
        "notes": {str(k): str(v) for k, v in (notes or {}).items() if v},
    }
    return DailyReport(session, data, _render_markdown(data), _render_telegram(data))


def write_daily_report(report: DailyReport, reports_dir: str | os.PathLike) -> Path:
    """reports/<date>.md, atomically."""
    return _atomic_write(Path(reports_dir) / f"{report.session.isoformat()}.md", report.markdown.encode("utf-8"))


def _cell(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), sort_keys=True)
    return str(v).replace("|", "/")


def _table(cols: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rows:
        return ["none", ""]
    return (["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            + ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows] + [""])


def _render_markdown(d: Mapping[str, Any]) -> str:
    lines = [f"# titan2 daily report {d['session']}", "",
             f"Generated {d['generated']}. Rates {d['rates_version']}. Net is from fills after every charge; "
             "MTM and stops use the bid. A G1 PROGRESS line is progress, not a decision.", "", "## Books", ""]
    lines += _table(["book", "n", "wins", "wr_net_pct", "exp_net_rs", "exp_net_pct", "net_rs", "charges_rs",
                     "avg_win_rs", "avg_loss_rs"], d["books"])
    lines += ["## Worst trade, exit mix and P&L status", ""]
    lines += _table(["book", "worst", "exit_mix", "pnl_status", "thin_book"], d["books"])
    lines += ["## Slippage and spreads", ""]
    lines += _table(["book", "entry_slip_vs_mid_pct", "exit_slip_vs_mid_pct", "slip_vs_walk_pct", "ioc_fill_rate_pct",
                     "entry_spread_pct"], d["books"])
    lines += ["## Refusals by gate", ""] + _table(["book", "outcome", "gate", "n"], d["refusals"]["routes"])
    lines += ["VOID structures:", ""] + _table(["book", "reason", "n"], d["refusals"]["void"])
    lines += ["Paper orders not filled, by SIM reason:", ""] + _table(["book", "reason", "n"], d["refusals"]["sim"])
    lines += ["## Halts (today, and any still active)", ""]
    lines += _table(["halt_id", "scope", "kind", "reason", "clears", "created_ts", "cleared_ts"], d["halts"])
    lines += ["## WARN and CRITICAL events", ""] + _table(["severity", "kind", "n"], d["events"])
    lines += ["## Paper-live twins", ""]
    lines += _table(["strategy", "twins", "live", "live_coverage_pct", "slip_gap_pp_median", "pairs_with_slip"],
                    d["twins"])
    lines += ["## G1 progress", ""]
    lines += _table(["strategy", "version", "mode", "trial", "n", "sessions", "exp_pct", "lb_pct", "next_look",
                     "futility_ub_pct", "today"], d["g1"])
    c = d["checks"]
    lines += ["## Checks", "", f"- VCN: {_cell(c['vcn'])}", f"- {_bar_check_line(c['bar_checks'])}",
              f"- EOD truth: {_cell(c['eod_truth'])}"]
    lines += [f"- {x}" for x in _side_lines(d) if x]
    lines += ["", "## Latencies (ms)", ""]
    lat = d["latency"]
    top = lat.get("max_ms") or {}
    lines += _table(["what", "n", "p50", "p95", "max"],
                    [{"what": k, **v, "max": top.get(k)} for k, v in lat.items() if isinstance(v, dict) and "p50" in v])
    lines += [f"- p95 quote age at decision: {_cell(lat['decision_quote_age_ms_p95'])} ms",
              f"- loop stalls: {lat['loop_stalls']['n']} "
              f"(max heartbeat gap {_cell(lat['loop_stalls']['max_gap_s'])} s)"]
    if lat.get("quote_calls") is not None:
        lines.append(f"- {_quote_calls_line(lat['quote_calls'])}")
    if lat.get("read_failures"):
        lines.append(f"- broker reads failed: {_counted(lat['read_failures'], shown=None)}")
    lines.append("")
    return "\n".join(lines)


def _counted(counts: Mapping[str, int], shown: int | None = REASONS_SHOWN) -> str:
    """'BOOK_CAPS 24, COOLDOWN 15, +2 more': the largest first."""
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    keep = items if shown is None else items[:shown]
    more = len(items) - len(keep)
    return ", ".join(f"{k} {v}" for k, v in keep) + (f", +{more} more" if more else "")


def _rs(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}Rs {abs(x):,.0f}"


def _refusal_line(r: Mapping[str, Any]) -> str:
    """The day's refusals by gate (no contract chosen yet), voids by reason (refused at the decision) and paper
    orders not filled, in one line for the phone."""
    gates: Counter = Counter()
    for row in r["routes"]:
        gates[row["gate"] or row["outcome"]] += row["n"]
    voids: Counter = Counter()
    for row in r["void"]:
        voids[row["reason"]] += row["n"]
    sims: Counter = Counter()
    for row in r["sim"]:
        sims[row["reason"]] += row["n"]
    parts = [f"refused {sum(gates.values())}" + (f" ({_counted(gates)})" if gates else ""),
             f"voided {sum(voids.values())}" + (f" ({_counted(voids)})" if voids else "")]
    if sims:
        parts.append(f"paper orders not filled {sum(sims.values())} ({_counted(sims)})")
    return "; ".join(parts)


def _bar_check_line(b: Mapping[str, Any]) -> str:
    why = b.get("reasons") or {}
    return f"bar check: {b['n']} checked, {b['mismatched']} mismatched" + (f" ({_counted(why)})" if why else "")


def _whatif_line(w: Mapping[str, Any] | None) -> str | None:
    if w is None:
        return None
    if not w["days"]:
        return "what-if: no day saved yet (titan2 whatif runs after each session)"
    when = "today included" if w["today"] else "saved days before today; today's runs after the session"
    return (f"what-if ({when}): {w['n']} refused entr{'y' if w['n'] == 1 else 'ies'} replayed over {w['days']} "
            f"day{'' if w['days'] == 1 else 's'}: net {_rs(w['net'])}, {w['won']} won")


def _alerts_line(a: Mapping[str, int] | None) -> str | None:
    """The alerter's counters (this run's: an in-process restart starts them again): sent, delayed by the network,
    and not delivered (given up after 2 hours or past 200 kept, or dropped from a full queue)."""
    if a is None:
        return None

    def n(key: str) -> int:
        x = a.get(key, 0)
        return x if isinstance(x, int) and not isinstance(x, bool) else 0

    held, lost = n("held"), n("lost") + n("dropped")
    waiting = max(0, held - n("resent") - n("lost"))
    text = f"alerts this run: {n('sent')} sent"
    text += f", {held} delayed by the network ({waiting} still waiting)" if held else ", none delayed"
    return text + (f", {lost} not delivered (see the log)" if lost else ", none lost")


def _note_lines(d: Mapping[str, Any]) -> list[str]:
    """The notes from outside the engine (the laptop's condor), one line each."""
    return [f"{k}: {v}" for k, v in sorted((d.get("notes") or {}).items())]


def _side_lines(d: Mapping[str, Any]) -> list[str | None]:
    """The what-if, condor (and any other note) and alert lines of the Checks section."""
    return [_whatif_line(d.get("whatif")), *_note_lines(d), _alerts_line(d.get("alerts"))]


def _quote_calls_line(q: Mapping[str, Any]) -> str:
    if not q["n"] and not q["keep_warm"]:
        return "quote calls: none measured"
    cold = (f"cold (no quote call in the {QUOTE_COLD_S:.0f} s before) {q['cold']} ({_cell(q['cold_pct'])}%), median "
            f"{_cell(q['cold_p50_ms'])} ms" if q["n"] else "")
    fails = f"failed {q['failed']}" + (f" ({_counted(q['failures'])})" if q["failures"] else "")
    parts = [f"quote calls: {q['n']}", cold, fails, f"retried {q['retried']}", f"symbols left out {q['missing']}",
             f"keep-warm calls {q['keep_warm']} (failed {q['keep_warm_failed']})"]
    return "; ".join(p for p in parts if p)


def _render_telegram(d: Mapping[str, Any]) -> str:
    out = [f"titan2 {d['session']}"]
    for b in d["books"]:
        name = b["book"] + (" (control trades, placebo: not a strategy result)" if b["book"].startswith("PLACEBO:")
                            else "")
        out.append(f"{name}: n={b['n']} WR {b['wr_net_pct']}% net ₹{b['net_rs']:.2f} "
                   f"(Exp ₹{b['exp_net_rs']:.2f}, worst ₹{b['worst']['net']:.2f}, "
                   f"charges ₹{b['charges_rs']:.2f})")
    if not d["books"]:
        out.append("no trades")
    active = [h for h in d["halts"] if h["cleared_ts"] is None]
    crit = sum(e["n"] for e in d["events"] if e["severity"] == "CRITICAL")
    out.append(f"halts active {len(active)}, today {len(d['halts'])}; CRITICAL events {crit}")
    # 2026-10-14 night: the phone heard only of trades; now also the day's refusals, the what-if, the condor, the bar
    #   check, the alerts not delivered and whether today counts for G1 (display only)
    out.append(_refusal_line(d["refusals"]))
    whatif = _whatif_line(d.get("whatif"))
    out += ([whatif] if whatif else []) + _note_lines(d)
    out.append(_bar_check_line(d["checks"]["bar_checks"]))
    alerts = _alerts_line(d.get("alerts"))
    out += [alerts] if alerts else []
    for g in d["g1"]:
        line = (f"G1 {g['strategy']} {g['trial'] or 'no trial'}: n={_cell(g['n'])} sessions={_cell(g['sessions'])} "
                f"exp {_cell(g['exp_pct'])}% next look {_cell(g['next_look'])}")
        out.append(line + (f"; today counts for G1: {g['today']}" if g.get("today") else ""))
    return "\n".join(out)[:3500]
