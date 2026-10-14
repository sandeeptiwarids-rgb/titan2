"""`titan2 whatif [--date D]`: what a session's refused entries would have made under titan2's own exit rules.

A research aid for the PAPER period, so that a filter is judged on numbers, never on a hunch: does the premium floor,
the spread or the depth rule block more winners than losers? It never trades and opens titan2.db read-only; it
writes only <var>/reports/whatif-<date>.json and prints a table plus the running totals of every saved day. The
laptop runs it after each session (laptop.run_day).

Input: the ENTRY_REFUSED events (core/gateway.py, from 2026-10-13): the contract the bot chose and the quotes it
saw when it refused. For each refused entry (one per intent):
  - buy one lot at the ask the bot saw. Without a quote (a Kite timeout), or with a crossed or locked one (bid at or
    above the ask), at the next minute's open plus an assumed half-spread of 0.75%;
  - replay Kite's 1-minute bars from the next whole minute, every price read as a bid (less the half-spread the bot
    saw), against the strategy's EXIT_PROFILE levels exactly as core/exits.py sets them at a fill: hard stop, lock
    and trail, take profit, time stop, the EOD flatten;
  - net P&L after Zerodha's charges (titan2.charges).
A minute bar hides the order of its moves, so each is read worst first: open, then low, then high. Depth, partial
fills, latency and stop slippage are ignored, which flatters the refused trades: treat a small what-if profit as
zero, and change a filter only on a large, lasting difference.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from titan2 import charges
from titan2.charges import OrderFills, RateSet
from titan2.core import exits
from titan2.model import ExitProfile, PnL, Side

log = logging.getLogger(__name__)

DEFAULT_HALF_SPREAD = 0.0075          # no decision quote: assume a 1.5% spread (the widest the entry gates allow)
HIST_PAUSE_S = 0.35                   # Kite's historical API allows 3 requests a second
MIN_PX = 0.05                         # the smallest option tick: a replayed bid never goes below it
REPORT_PREFIX = "whatif-"
Out = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class Refused:
    ts: datetime
    strategy: str
    book: str
    intent_id: str
    underlying: str
    gate: str
    detail: str
    symbol: str
    token: int
    lot: int
    bid: float | None
    ask: float | None

    @property
    def why(self) -> str:
        return f"{self.gate}:{short_detail(self.detail)}"


@dataclass(frozen=True, slots=True)
class Replay:
    entry: float
    exit: float
    reason: str                       # HARD_SL | LOCK_STOP | TAKE_PROFIT | TIME_STOP | EOD_FLATTEN | DATA_END
    exit_at: datetime


def short_detail(detail: str) -> str:
    """The refusal's code without its free text: 'FETCH_FAILED: AMBIGUOUS:ReadTimeout ...' -> 'FETCH_FAILED'."""
    head = (detail or "").strip().split(":", 1)[0].split(" ", 1)[0]
    return head or "?"


# ---------------------------------------------------------------------------------------------------------------
# Reading the refusals
# ---------------------------------------------------------------------------------------------------------------
def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 else None


def refused_from(event: Mapping[str, Any]) -> Refused | None:
    """One ENTRY_REFUSED events row (payload decoded) as a Refused; None without a usable contract."""
    p = event.get("payload") or {}
    legs = p.get("legs") or []
    if not legs or not isinstance(legs[0], Mapping):
        return None
    leg = legs[0]
    q = (p.get("quotes") or {}).get(leg.get("symbol")) or {}
    try:
        return Refused(ts=datetime.fromisoformat(str(event["ts"])), strategy=str(event.get("strategy") or ""),
                       book=str(event.get("book") or ""), intent_id=str(event.get("intent_id") or ""),
                       underlying=str(p.get("underlying") or event.get("underlying") or ""),
                       gate=str(p.get("gate") or "?"), detail=str(p.get("detail") or ""), symbol=str(leg["symbol"]),
                       token=int(leg["token"]), lot=int(leg["lot_size"]), bid=_num(q.get("bid")),
                       ask=_num(q.get("ask")))
    except (KeyError, TypeError, ValueError):
        return None


def refused_entries(db_path: Path, day: date) -> list[Refused]:
    """The session's refused entries, one per intent (the first book's: PAPER before its SHADOW diversion)."""
    from titan2 import store as store_mod

    st = store_mod.open_readonly(db_path)
    try:
        rows = st.events(session=day, kind="ENTRY_REFUSED")
        underlying = {r["intent_id"]: r["underlying"] for r in
                      st.query("SELECT intent_id, underlying FROM intents WHERE session = ?", (day.isoformat(),))}
    finally:
        st.close()
    out: list[Refused] = []
    seen: set[str] = set()
    for row in rows:
        r = refused_from(row)
        if r is None or r.intent_id in seen or r.book.startswith("PLACEBO:"):
            continue
        seen.add(r.intent_id)
        if not r.underlying:
            r = Refused(**{**asdict(r), "underlying": str(underlying.get(r.intent_id, ""))})
        out.append(r)
    return out


# ---------------------------------------------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------------------------------------------
def replay(entry: float, half_spread: float, qty: int, entry_at: datetime, bars: Sequence[Bar],
           profile: ExitProfile, rates: RateSet, flatten_at: datetime) -> Replay | None:
    """Hold one position bought at `entry` from `entry_at` through `bars` (each starting after entry_at), every
    price read as a bid `half_spread` rupees lower, under core/exits.py's levels; None without a bar."""
    c_in = charges.order_charges(OrderFills(Side.BUY, qty, qty * entry), rates).total
    p = exits.compute_exit_params(profile, entry, qty, c_in, rates, entry_at)

    def bid(px: float) -> float:
        return max(px - half_spread, MIN_PX)

    stop, armed, peak = p.hard_sl_px, False, None
    last: Bar | None = None
    for b in bars:
        if b.start <= entry_at:
            continue
        if b.start >= flatten_at:
            return Replay(entry, bid(b.open), "EOD_FLATTEN", b.start)
        if b.start >= p.time_stop_at and (not armed or profile.time_stop_when_locked):
            return Replay(entry, bid(b.open), "TIME_STOP", b.start)
        o, lo, hi = bid(b.open), bid(b.low), bid(b.high)
        stopped = "LOCK_STOP" if armed else "HARD_SL"
        if o <= stop:                                       # opened through the stop: sold at the open
            return Replay(entry, o, stopped, b.start)
        if o >= p.tp_px:
            return Replay(entry, o, "TAKE_PROFIT", b.start)
        if lo <= stop:                                      # worst first: the low before the high
            return Replay(entry, stop, stopped, b.start)
        peak = hi if peak is None else max(peak, hi)
        if not armed and hi >= p.arm_px:
            armed = True
        if armed:
            stop = max(stop, p.floor_px, p.be_px + profile.trail_retain * (peak - p.be_px))
        if hi >= p.tp_px:
            return Replay(entry, p.tp_px, "TAKE_PROFIT", b.start)
        last = b
    if last is None:
        return None
    return Replay(entry, bid(last.close), "DATA_END", last.start)


def net_of(entry: float, exit_px: float, qty: int, rates: RateSet) -> PnL:
    return charges.net_pnl([OrderFills(Side.BUY, qty, qty * entry), OrderFills(Side.SELL, qty, qty * exit_px)],
                           rates)


def evaluate(r: Refused, bars: Sequence[Bar], profile: ExitProfile, rates: RateSet,
             flatten_at: datetime) -> dict[str, Any]:
    """One report row: the refusal plus its replayed outcome (or why there is none)."""
    row: dict[str, Any] = {"time": r.ts.strftime("%H:%M:%S"), "strategy": r.strategy, "underlying": r.underlying,
                           "symbol": r.symbol, "why": r.why, "detail": r.detail[:120], "bid": r.bid, "ask": r.ask,
                           "lot": r.lot}
    bars = [b for b in bars if b.start > r.ts]
    # a crossed or locked quote (bid at or above the ask: a side left behind, which no exchange shows) is no quote,
    #   as for the paper exchange (paper model 2): its stale ask is under the bid, a price nobody offers any more
    crossed = r.ask is not None and r.bid is not None and r.bid >= r.ask
    if r.ask is not None and not crossed:
        half = (r.ask - r.bid) / 2 if r.bid is not None else DEFAULT_HALF_SPREAD * r.ask
        entry, entry_at, rest, assumed = r.ask, r.ts, bars, r.bid is None
    elif bars:                                              # no quote: the next minute's open plus the assumed spread
        half = DEFAULT_HALF_SPREAD * bars[0].open
        entry, entry_at, rest, assumed = bars[0].open + half, bars[0].start, bars[1:], True
    else:
        return {**row, "outcome": None, "note": "no quote and no bars after the refusal"}
    rp = replay(entry, half, r.lot, entry_at, rest, profile, rates, flatten_at)
    if rp is None:
        return {**row, "outcome": None, "note": "no minute bars after the entry"}
    pnl = net_of(rp.entry, rp.exit, r.lot, rates)
    return {**row, "outcome": {"entry": round(rp.entry, 2), "exit": round(rp.exit, 2), "reason": rp.reason,
                               "exit_at": rp.exit_at.strftime("%H:%M"), "gross": pnl.gross,
                               "charges": pnl.charges.total, "net": pnl.net, "assumed_spread": assumed}}


# ---------------------------------------------------------------------------------------------------------------
# Kite, files and text
# ---------------------------------------------------------------------------------------------------------------
def kite_from_session(env: Mapping[str, str], var: Path) -> Any:
    from titan2 import token as token_mod

    sess = token_mod.read_session(token_mod.session_path(var))
    return token_mod.kite_client(str(env.get("ZERODHA_API_KEY", "")).strip(), sess.access_token)


def fetch_bars(kite: Any, token: int, start: datetime, end: datetime) -> list[Bar]:
    rows = kite.historical_data(token, start.replace(second=0, microsecond=0), end, "minute")
    bars = []
    for x in rows or []:
        t = x["date"]
        t = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
        if t.tzinfo is None:
            t = t.replace(tzinfo=start.tzinfo)
        bars.append(Bar(t, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"])))
    return bars


def report_path(var: Path, day: date) -> Path:
    return Path(var) / "reports" / f"{REPORT_PREFIX}{day.isoformat()}.json"


def saved_rows(var: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((Path(var) / "reports").glob(f"{REPORT_PREFIX}*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows += [dict(r, day=data.get("day")) for r in data.get("rows", []) if isinstance(r, dict)]
    return rows


def _rs(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}Rs {abs(x):,.0f}"


def render(day: date, rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [f"What-if for {day}: {len(rows)} refused entr{'y' if len(rows) == 1 else 'ies'}, each replayed as one "
             "lot under titan2's exit rules after real charges (approximate: it flatters them)"]
    for r in rows:
        o = r.get("outcome")
        head = f"  {r['time'][:5]}  {r['symbol'].removeprefix('NFO:'):24} {r['why']:24}"
        if not o:
            lines.append(f"{head} not replayed: {r.get('note')}")
            continue
        flag = " (spread assumed)" if o.get("assumed_spread") else ""
        lines.append(f"{head} {o['entry']:>8.2f} -> {o['exit']:<8.2f} {o['reason']:<11} at {o['exit_at']}  "
                     f"{_rs(o['net'])}{flag}")
    return "\n".join(lines)


def totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """Per refusal reason over the given rows: n replayed, winners (net > 0), net sum."""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        o = r.get("outcome")
        if not o:
            continue
        t = out.setdefault(r["why"], {"n": 0, "won": 0, "net": 0.0})
        t["n"] += 1
        t["won"] += 1 if o["net"] > 0 else 0
        t["net"] += float(o["net"])
    return out


def render_totals(rows: Sequence[Mapping[str, Any]]) -> str:
    t = totals(rows)
    days = len({r.get("day") for r in rows})
    if not t:
        return "What-if totals: nothing replayed yet"
    lines = [f"What-if totals over {days} saved day{'s' if days != 1 else ''} (blocked by -> what they would have made):"]
    for why, x in sorted(t.items(), key=lambda kv: -kv[1]["n"]):
        n = int(x["n"])
        lines.append(f"  {why:28} {n:3} trades, {int(x['won']):3} winners, net {_rs(x['net'])} "
                     f"(avg {_rs(x['net'] / n)} a trade)")
    lines.append("  Under 30 trades a line is noise; a filter is worth revisiting only when its line stays clearly "
                 "positive for weeks.")
    return "\n".join(lines)


def run(var: Path, day: date, env: Mapping[str, str], *, out: Out = print, kite: Any = None,
        sleep: Callable[[float], Any] = time.sleep) -> int:
    """Replay `day`'s refused entries, save reports/whatif-<day>.json and print it with the running totals.
    0 done (also: nothing to replay), 2 no database or no Kite session."""
    from titan2 import strategies, store as store_mod
    from titan2.settings import DEFAULTS

    var = Path(var)
    db = var / "db" / store_mod.DB_FILENAME
    if not db.is_file():
        out(f"what-if: no {db}")
        return 2
    refused = refused_entries(db, day)
    if not refused:
        out(f"What-if for {day}: no refused entry with a recorded contract")
        return 0
    if kite is None:
        try:
            kite = kite_from_session(env, var)
        except Exception as exc:
            out(f"what-if: no usable Kite session ({exc}); run it while today's login is valid")
            return 2
    flatten_at = exits.EodTimes.for_session(day, DEFAULTS).flatten
    rates = charges.rates_for(day)
    rows = []
    for i, r in enumerate(refused):
        cls = strategies.REGISTRY.get(r.strategy)
        profile = getattr(cls, "EXIT_PROFILE", None) or ExitProfile()
        end = min(r.ts + timedelta(minutes=profile.time_stop_min + 3), flatten_at + timedelta(minutes=1))
        try:
            if i:
                sleep(HIST_PAUSE_S)
            bars = fetch_bars(kite, r.token, r.ts, end)
        except Exception as exc:
            log.warning("what-if: no bars for %s: %r", r.symbol, exc)
            rows.append({"time": r.ts.strftime("%H:%M:%S"), "strategy": r.strategy, "underlying": r.underlying,
                         "symbol": r.symbol, "why": r.why, "detail": r.detail[:120], "bid": r.bid, "ask": r.ask,
                         "lot": r.lot, "outcome": None, "note": f"no minute bars: {type(exc).__name__}"})
            continue
        rows.append(evaluate(r, bars, profile, rates, flatten_at))
    path = report_path(var, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"day": day.isoformat(), "rows": rows}, indent=1, default=str), encoding="utf-8")
    out(render(day, rows))
    out(render_totals(saved_rows(var)))
    return 0
