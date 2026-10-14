"""Reconciliation and restart recovery as pure functions (spec §9.1-9.4, the §9.6 planner, §9.8).

Nothing here does I/O or touches engine state. Each function reads broker snapshots plus the bot's own rows and
returns findings with the halts, alerts, commands and events the engine should apply. Recon never asks for an
entry and never for a sell: the only order it can propose is a COVER BUY for a short the bot's own fills explain.

Ownership (§9.1): a bot order has a tag that parse_tag() accepts for this account AND an order_pk in the DB. A bot
position is a (symbol, MIS) net fully explained by bot fills. Everything else is foreign and is never traded.

Lag safety (§9.2): a positions or orders snapshot counts for a symbol only if it is ok and was REQUESTED at least
POSITIONS_POST_FILL_LAG_S after the newest bot fill on that symbol (post_fill). Nothing else may cap an exit, verify
flat or declare an external close. A snapshot is in grace if a bot order of this session on its symbol is working
or it was requested within FRESH_FILL_GRACE_S of a fill; a difference counts only when two snapshots requested
outside grace and RECON_CONFIRM_GAP_S apart agree.

Quantities are signed and kept per (symbol, product). Several legs of one account may hold the same symbol (two
PAPER books), so the bot's side is summed per symbol (SymbolView) and exits on one symbol are serialized.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

from titan2 import charges     # the one P&L function (R3); imported here, never at first use (a release switch
#                                 without a restart would otherwise load the new release's code, DETAIL §13.1)
from titan2.clock import ist_datetime, to_ist
from titan2.core.events import (
    Alert, Command, FetchOrders, FetchPositions, FetchTradebook, FetchTrades, OrdersSnapshot, PollOrder,
    PositionsSnapshot, Priority,
)
from titan2.model import (
    MIS, NFO, Account, BrokerOrder, BrokerPosition, BrokerTrade, HaltClears, Leg, LegState, Order, OrderState,
    PnlStatus, Role, Side, StructState, Structure, _StrEnum, external_order_pk, is_bot_tag, leg_fill_totals,
    parse_tag,
)
# Code constants of DETAIL §8.4 live in settings (hashed into SEMANTIC_CONSTANTS); never copied here.
# Changing POSITIONS_POST_FILL_LAG_S needs an ENGINE_SEMANTICS_VERSION bump.
from titan2.settings import (
    BROKER_READS_FAIL_UNCAP_S,   # broker_cap: no usable read this long -> an older read or own fills cap the exit
    EXIT_CAP_PRIOR_READ_S,       # broker_cap: a read requested this long before the decision still caps (B1: 0)
    FRESH_FILL_GRACE_S,          # a symbol is in grace this long after a bot fill
    POSITIONS_POST_FILL_LAG_S,   # a snapshot counts only if requested this long after the newest bot fill
    POSITIONS_WAIT_MAX_S,        # broker_cap: wait this long for post-fill positions, then use the orders view
    RECON_CONFIRM_GAP_S,         # two agreeing countable snapshots this far apart confirm a difference
    VERIFY_FLAT_RECHECK_S,       # re-request a failed or already-used snapshot no more often than this
    VERIFY_FLAT_TIMEOUT_S,       # verify_flat: positions unusable this long -> try the orders view
    DEFAULTS,                    # the code-default Settings (the RECON_INTERVAL_S fallback below)
)

if TYPE_CHECKING:
    from titan2.ledger import CarriedTruth

__all__ = [
    "AUTO_SQUAREOFF", "BotView", "BookTruth", "Cap", "CoverPlan", "ExternalClose", "ExternalFill", "Finding",
    "HaltRequest", "LogEvent", "MANUAL", "ReconClass", "ReconMemory", "ReconResult", "ReconStatus", "RecoveryPlan",
    "Sighting", "StaleStructure", "SymbolView", "TagImport", "TradebookOrder", "TruthCheck", "VerifyDecision",
    "VerifyState",
    "broker_cap", "broker_nets", "carried_closes", "entries_may_resume", "eod_truth_check", "external_wait", "in_grace",
    "is_broker_squareoff", "net_qty", "orders_net", "plan_recovery", "post_fill", "recon_due", "recon_fetches",
    "reconcile", "recovery_fetches", "stale_action", "symbol_view", "symbol_views", "tradebook_orders",
    "verify_flat",
]

# Recon-only rule values from §9.3-9.6 (not in DETAIL §8.4's list)
SHORT_CONFIRM_GAP_S = 2.0         # auto-cover of a bot-explained short: two agreeing snapshots this far apart
VERIFY_FLAT_CRITICAL_S = 30.0     # verify_flat: still unresolved -> CRITICAL once
STALE_RECON_ERRORS = 3            # consecutive failed reads -> HALT(STALE_RECON, ACCOUNT, AUTO)
STALE_RECON_CLEAR_OKS = 2         # consecutive good reads -> clear it
CLEAN_RECONS_TO_RESUME = 2        # recovery: consecutive CLEAN recons per account before entries resume
RECOVERY_FETCH_TRIES = 3          # recovery reads: tries, RECOVERY_FETCH_GAP_S apart, then HALT(STALE_RECON)
RECOVERY_FETCH_GAP_S = 2.0
RECON_INTERVAL_S = DEFAULTS.RECON_INTERVAL_S   # fallback only: the engine passes its Settings.RECON_INTERVAL_S
RECON_OPEN_LEGS_INTERVAL_S = 5.0
_TIME_EPS = 1e-6                  # monotonic float noise at time boundaries
# Periodic recon window: the bot timeline (§12.5), not the exchange close. 15:31 is the last EOD recon at the default
# BROKER_SQUAREOFF_AT (15:26 + 5 min); the engine passes window_end = its own last EOD recon, which moves with that
# [V] setting (up to 15:39 + 5 min), so a later square-off is still reconciled on the 5 s cadence.
RECON_WINDOW = (dtime(9, 0), dtime(15, 31))
EOD_TRUTH_TOLERANCE_LIVE_RS = 1.0
PAISE = 0.005                     # "exactly equal" for rupee amounts kept to 2 dp
RECOVERED_STRATEGY = "_recovered"
MANUAL = "MANUAL"                 # Order.external_kind values (same literals as charges.EXTERNAL_KINDS)
AUTO_SQUAREOFF = "AUTO_SQUAREOFF"

_NOT_SENT = frozenset({OrderState.NEW, OrderState.SUBMITTING, OrderState.UNKNOWN})


class ReconClass(_StrEnum):
    BROKER_ERROR = "BROKER_ERROR"          # a read failed (any error, incl. 429): nothing is inferred
    CLEAN = "CLEAN"
    PENDING = "PENDING"                    # seen once, or in grace, or the snapshot predates the post-fill window
    REPAIRED = "REPAIRED"                  # bot orders' broker fills explain the gap: apply them
    EXTERNAL_CLOSE = "EXTERNAL_CLOSE"      # the bot's qty is gone at the broker (MANUAL)
    BROKER_SQUAREOFF = "BROKER_SQUAREOFF"  # ... gone through the broker's MIS square-off
    PARTIAL_EXTERNAL = "PARTIAL_EXTERNAL"  # the broker holds less than the bot, but not zero
    EXCESS = "EXCESS"                      # the broker holds more on a bot symbol than bot fills explain
    FOREIGN = "FOREIGN"                    # a non-bot symbol or a non-MIS row with net != 0
    SHORT = "SHORT"                        # any NFO net < 0
    # positions show less than the bot holds but today's orders do not back it (no sale, or the views disagree):
    #   a broker glitch or orders() lagging a sale. Nothing is booked, the leg stays managed; HALT(MISMATCH) +
    #   CRITICAL. Beyond §9.3 (post-build review): the spec booked this as an EXTERNAL_CLOSE from positions alone.
    CONTRADICTION = "CONTRADICTION"


_RANK = {c: i for i, c in enumerate((
    ReconClass.CLEAN, ReconClass.PENDING, ReconClass.REPAIRED, ReconClass.BROKER_SQUAREOFF,
    ReconClass.EXTERNAL_CLOSE, ReconClass.PARTIAL_EXTERNAL, ReconClass.CONTRADICTION, ReconClass.FOREIGN,
    ReconClass.EXCESS, ReconClass.SHORT,
))}
_G1_OK = frozenset({ReconClass.CLEAN, ReconClass.PENDING, ReconClass.BROKER_SQUAREOFF})
# §10.9: on LIVE these mean MISMATCH, UNKNOWN_POSITION or SHORT -> global halt + demote every LIVE strategy
_LIVE_DEMOTE = frozenset({ReconClass.EXTERNAL_CLOSE, ReconClass.PARTIAL_EXTERNAL, ReconClass.CONTRADICTION,
                          ReconClass.EXCESS, ReconClass.FOREIGN, ReconClass.SHORT})


# ---------------------------------------------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HaltRequest:
    """A halt for the engine to raise (or, in clear_halts, to clear if active). The engine writes the row."""

    kind: str
    scope: str
    clears: HaltClears
    reason: str


@dataclass(frozen=True, slots=True)
class LogEvent:
    """One events row for the engine to write (DETAIL §4.6 kinds)."""

    kind: str
    severity: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Sighting:
    """A countable snapshot that showed a difference (the first half of a confirmation)."""

    req_mono: float
    broker_qty: int
    expected: int


@dataclass(frozen=True, slots=True)
class ReconMemory:
    """What reconcile() carries between runs for one account. In RAM only: a restart starts afresh."""

    status: str = "UNKNOWN"
    last_clean_mono: float | None = None
    clean_streak: int = 0
    error_streak: int = 0
    ok_streak: int = 0
    sightings: Mapping[tuple[str, str], Sighting] = field(default_factory=dict)
    reported: Mapping[tuple[str, str], str] = field(default_factory=dict)   # key -> last reported signature

    def clean_within(self, now: float, max_age_s: float) -> bool:
        """G07: a CLEAN recon ran within max_age_s (a future timestamp fails closed)."""
        return self.last_clean_mono is not None and 0.0 <= now - self.last_clean_mono <= max_age_s


# The spec's name for a GateContext.recon value (DETAIL §8.6: recon: Mapping[Account, ReconStatus]).
ReconStatus = ReconMemory


@dataclass(frozen=True, slots=True)
class BotView:
    """The bot's side of one account, built by the engine from its own rows.

    legs: every leg of this account that is non-terminal (any session) or belongs to this session, each bound to
    its orders (Leg.bind_orders), EXTERNAL rows included. After a restart pass start_mono: every leg's last fill
    then counts as no earlier than the process start (§9.2).
    """

    account: Account
    session: date
    squareoff_at: dtime                                    # settings.BROKER_SQUAREOFF_AT, IST time of day [V]
    legs: tuple[Leg, ...] = ()
    foreign_acks: Mapping[tuple[str, str], int] = field(default_factory=dict)   # (symbol, product) -> acked qty
    last_rb: Mapping[int, float] = field(default_factory=dict)                  # leg_id -> last realizable bid
    api_placed_by: frozenset[str] = frozenset({"API"})     # placed_by of this account's own API orders [V]
    start_mono: float | None = None


@dataclass(frozen=True, slots=True)
class SymbolView:
    """The bot's position on one (symbol, MIS) of one account, summed over its legs."""

    symbol: str
    expected: int = 0                     # Σ open qty of non-terminal legs (a long leg never counts below 0)
    last_fill_mono: float | None = None   # newest bot fill on the symbol (>= process start after a restart)
    in_flight: bool = False               # some bot order of this session on the symbol is not terminal
    legs: tuple[int, ...] = ()            # non-terminal legs holding qty, oldest first
    today_legs: tuple[int, ...] = ()      # legs of this session on the symbol (bot-owned today)
    orders: tuple[Order, ...] = ()        # every DB order on the symbol, bot and EXTERNAL
    # When the bot's entry into `legs` began: its earliest ENTRY/SELFTEST order's creation (bot clock), else the
    #   earliest entry fill. An order that closed the position (§9.3: non-bot SELLs after the entry) cannot come
    #   earlier, and the bound holds when the bot learns of its entry fill late (WS updates lost, the 3 s poll).
    entry_wall: datetime | None = None
    known_sell_qty: int = 0               # today's DB SELL fills on the symbol (bot exits + recorded EXTERNAL)
    known_sell_value: float = 0.0
    ack: int = 0                          # acked foreign qty on (symbol, MIS) this session
    carried: int = 0                      # signed qty held by legs of earlier sessions (today's orders cannot show
    #                                       it: a PAPER leftover awaiting the simulator's startup square-off)


@dataclass(frozen=True, slots=True)
class ExternalFill:
    broker_order_id: str | None           # None: no broker order was found for this qty
    qty: int
    px: float | None                      # None only when UNRESOLVED and no realizable bid is known
    wall: datetime | None
    # One broker order closing the legs of several books (one square-off or manual close of the symbol): this leg's
    #   share (start, end, total_qty, total_value) of the order's qty taken by the plan, legs in leg_id order (the
    #   FIFO order). close_external books it as a charges.OrderPart: the order's charges are booked once, shared.
    part: tuple[int, int, int, float] | None = None


@dataclass(frozen=True, slots=True)
class ExternalClose:
    """close_external() input for one leg (§9.3): what left, through which orders, at what price."""

    leg_id: int
    qty: int                              # the leg's open qty that left the account
    kind: ReconClass                      # EXTERNAL_CLOSE (MANUAL) or BROKER_SQUAREOFF
    pnl_status: PnlStatus
    fills: tuple[ExternalFill, ...]       # Σ qty == qty
    orders: tuple[BrokerOrder, ...]       # broker orders the fills reference: become role EXTERNAL rows

    @property
    def external_kind(self) -> str:
        return AUTO_SQUAREOFF if self.kind is ReconClass.BROKER_SQUAREOFF else MANUAL


@dataclass(frozen=True, slots=True)
class CoverPlan:
    """BUY back exactly qty through the cover loop (§5.7). The COVER order hangs off leg_id."""

    symbol: str
    qty: int
    leg_id: int


@dataclass(frozen=True, slots=True)
class Finding:
    cls: ReconClass
    symbol: str
    product: str
    broker_qty: int                       # signed broker net (0 when there is no row)
    expected: int                         # signed bot expectation (0 for a non-bot symbol)
    legs: tuple[int, ...] = ()            # the bot legs involved
    detail: str = ""
    repairs: tuple[tuple[str, BrokerOrder], ...] = ()   # REPAIRED: (order_pk, snapshot) to apply (source RECOVERY)
    closes: tuple[ExternalClose, ...] = ()              # EXTERNAL_CLOSE / BROKER_SQUAREOFF: one per leg
    cap_qty: int | None = None                          # PARTIAL_EXTERNAL: what the broker still holds for the bot
    cover: CoverPlan | None = None                      # SHORT: only when the bot's own fills explain it
    commands: tuple[Command, ...] = ()
    g1_mismatch: bool = False


@dataclass(frozen=True, slots=True)
class ReconResult:
    """Apply findings in order. halts/alerts/events are emitted when a finding first appears or changes."""

    account: Account
    status: ReconClass
    findings: tuple[Finding, ...]
    memory: ReconMemory
    halts: tuple[HaltRequest, ...] = ()
    clear_halts: tuple[HaltRequest, ...] = ()
    alerts: tuple[Alert, ...] = ()
    commands: tuple[Command, ...] = ()
    events: tuple[LogEvent, ...] = ()

    @property
    def clean(self) -> bool:
        return self.status is ReconClass.CLEAN

    @property
    def g1_mismatch(self) -> bool:
        """PAPER and SHADOW accounts only (§9.3): any class but CLEAN, PENDING or BROKER_SQUAREOFF."""
        if self.account is Account.LIVE:
            return False
        return self.status is ReconClass.BROKER_ERROR or any(f.g1_mismatch for f in self.findings)

    @property
    def live_demote(self) -> bool:
        """§10.9: a LIVE MISMATCH, UNKNOWN_POSITION or SHORT -> the engine halts globally and demotes LIVE."""
        return self.account is Account.LIVE and any(f.cls in _LIVE_DEMOTE for f in self.findings)


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------
def _ok(snap: object) -> bool:
    return snap is not None and bool(getattr(snap, "ok", False))


def _positive(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _scope(acct: Account) -> str:
    return f"ACCOUNT:{acct}"


def _wall_key(b: BrokerOrder) -> tuple:
    return (b.order_wall is None, b.order_wall.timestamp() if b.order_wall else 0.0, b.broker_order_id)


def post_fill(snapshot: object, leg: object) -> bool:
    """§9.2: the snapshot is ok and was requested >= POSITIONS_POST_FILL_LAG_S after the newest bot fill.

    leg: a Leg or SymbolView (its last_fill_mono is used), or a last-fill monotonic time. None = no fill yet.
    """
    if not _ok(snapshot):
        return False
    last = getattr(leg, "last_fill_mono", leg)
    return last is None or snapshot.req_mono >= last + POSITIONS_POST_FILL_LAG_S


def in_grace(sv: SymbolView, at: float) -> bool:
    """§9.2 at monotonic time `at` (for a snapshot: its request time): a bot order on the symbol is working, or the
    newest bot fill is at most FRESH_FILL_GRACE_S old. The boundary counts as grace, so two confirming snapshots
    RECON_CONFIRM_GAP_S apart are both requested strictly later than fill + 8 s: an 8 s positions lag never
    becomes an external close."""
    lf = sv.last_fill_mono
    return sv.in_flight or (lf is not None and at - lf <= FRESH_FILL_GRACE_S)


def _outside_grace_floor(lf: float | None) -> float:
    """Request times above this are outside the fresh-fill grace (and post-fill)."""
    return -math.inf if lf is None else lf + FRESH_FILL_GRACE_S


# Ported from agentic_trader/position_reconciliation.py:248-281 @b4694f3 — changes: one SIGNED net per
#   (symbol, product) from normalized BrokerPosition rows (v1 compared abs() quantities and ignored the product);
#   zero rows are kept (a closed position is information); a failed fetch is never read
def broker_nets(rows: Iterable[BrokerPosition]) -> dict[tuple[str, str], int]:
    nets: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r.symbol, r.product)
        nets[key] = nets.get(key, 0) + int(r.net_qty)
    return nets


def net_qty(snapshot: PositionsSnapshot, symbol: str, product: str = MIS) -> int:
    """Signed broker net for (symbol, product); 0 when the snapshot has no row for it."""
    return sum(int(r.net_qty) for r in snapshot.rows if r.symbol == symbol and r.product == product)


def orders_net(orders: Iterable[BrokerOrder], symbol: str, product: str = MIS) -> int:
    """§9.4 orders view: Σ BUY filled − Σ SELL filled over ALL of today's orders for (symbol, product)."""
    n = 0
    for o in orders:
        if o.symbol == symbol and o.product == product:
            n += o.filled_qty if o.side == Side.BUY else -o.filled_qty
    return n


def is_broker_squareoff(order: BrokerOrder, threshold: datetime, api_placed_by: frozenset[str]) -> bool:
    """§9.3 [V]: placed at/after the MIS square-off time, and either untagged or not placed by our own API."""
    if order.order_wall is None or order.order_wall < threshold:
        return False
    return not order.tag or (order.placed_by or "") not in api_placed_by


def _need_fetch(snap: object, lf: float | None, now: float, in_flight: bool, after: float | None = None,
                failed: float | None = None) -> bool:
    """Ask for a fresh snapshot only when its answer can count, and no faster than VERIFY_FLAT_RECHECK_S. A
    snapshot requested before `after` (an exit decision) does not count either. failed: when the newest failed read
    of this kind was requested; a read that failed is re-asked no faster than VERIFY_FLAT_RECHECK_S either (a
    broken positions API must not turn the exit loop into a read storm)."""
    if in_flight or (lf is not None and now < lf + POSITIONS_POST_FILL_LAG_S):
        return False
    if (failed is not None and now - failed < VERIFY_FLAT_RECHECK_S
            and (not _ok(snap) or failed > snap.req_mono)):
        return False
    if (snap is None or (lf is not None and snap.req_mono < lf + POSITIONS_POST_FILL_LAG_S)
            or (after is not None and snap.req_mono < after)):
        return True
    return now - snap.req_mono >= VERIFY_FLAT_RECHECK_S


def _known_ids(sv: SymbolView) -> set[str]:
    """order_pks and broker ids of every DB order on the symbol (bot and EXTERNAL)."""
    out = {o.order_pk for o in sv.orders}
    out.update(o.broker_order_id for o in sv.orders if o.broker_order_id)
    return out


# ---------------------------------------------------------------------------------------------------------------
# The bot's side, per symbol
# ---------------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _Acc:
    expected: int = 0
    lf: float | None = None
    in_flight: bool = False
    legs: list = field(default_factory=list)
    today: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    entry_wall: datetime | None = None
    sell_qty: int = 0
    sell_value: float = 0.0
    carried: int = 0


def symbol_views(bot: BotView) -> dict[str, SymbolView]:
    """Per-symbol sums of the bot's legs on this account (only MIS: the bot trades nothing else)."""
    acc: dict[str, _Acc] = {}
    for leg in sorted(bot.legs, key=lambda lg: lg.leg_id):
        if leg.account != bot.account:
            continue
        a = acc.setdefault(leg.inst.symbol, _Acc())
        orders = leg.leg_orders()
        a.orders.extend(orders)
        if leg.last_fill_mono is not None:
            a.lf = leg.last_fill_mono if a.lf is None else max(a.lf, leg.last_fill_mono)
        # an order of an earlier session can no longer fill (the exchange day ended; the broker serves only today's
        #   orders, so it may never even turn terminal): it holds no symbol in grace and never makes an exit wait
        if leg.session == bot.session and any(o.role != Role.EXTERNAL and not o.is_terminal for o in orders):
            a.in_flight = True
        if leg.session == bot.session:
            a.today.append(leg.leg_id)
            for o in orders:
                if o.side == Side.SELL and o.filled_qty > 0:
                    a.sell_qty += o.filled_qty
                    a.sell_value += o.filled_qty * (o.avg_fill_px or 0.0)
        if leg.is_terminal:
            continue
        entry, exit_ = leg_fill_totals(orders)
        held = max(0, entry - exit_)
        if held > 0:
            signed = held if leg.side == "LONG" else -held
            a.expected += signed
            a.legs.append(leg.leg_id)
            if leg.session != bot.session:
                a.carried += signed
            began = min((o.created_wall for o in orders if o.role in (Role.ENTRY, Role.SELFTEST)
                         and o.created_wall is not None), default=leg.entry_fill_wall)
            if began is not None and (a.entry_wall is None or began < a.entry_wall):
                a.entry_wall = began
    out = {}
    for sym, a in acc.items():
        lf = a.lf
        if bot.start_mono is not None:
            lf = bot.start_mono if lf is None else max(lf, bot.start_mono)
        out[sym] = SymbolView(sym, a.expected, lf, a.in_flight, tuple(a.legs), tuple(a.today), tuple(a.orders),
                              a.entry_wall, a.sell_qty, round(a.sell_value, 6),
                              int(bot.foreign_acks.get((sym, MIS), 0)), a.carried)
    return out


def symbol_view(bot: BotView, symbol: str) -> SymbolView:
    """The bot's view of one symbol (an empty view when the bot holds nothing there)."""
    sv = symbol_views(bot).get(symbol)
    if sv is None:
        sv = SymbolView(symbol, last_fill_mono=bot.start_mono, ack=int(bot.foreign_acks.get((symbol, MIS), 0)))
    return sv


# ---------------------------------------------------------------------------------------------------------------
# §9.3 periodic reconciliation
# ---------------------------------------------------------------------------------------------------------------
def reconcile(bot: BotView, pos: PositionsSnapshot | None, ords: OrdersSnapshot | None, mem: ReconMemory,
              now: float) -> ReconResult:
    """Classify one account (§9.3 table). Call once per new positions snapshot, with the latest orders snapshot.

    Never infers anything from a failed read. Every difference on a bot symbol needs post-fill snapshots requested
    outside grace, and (except REPAIRED, which is backed by the bot's own order rows) two agreeing ones
    RECON_CONFIRM_GAP_S apart. A broker holding less than the bot is an external close only when the sale shows:
    today's orders (holding the bot's own fills) add up to the positions net, or, when an orders read misses the
    bot's own fills, the positions row's day sells cover it (_drop_unbacked). Otherwise it is a CONTRADICTION:
    HALT(MISMATCH), nothing booked, the leg stays managed. SHORT halts at once; its cover needs the same evidence
    plus an exact bot-fill explanation. `now` (monotonic) stamps last_clean_mono.
    """
    acct = Account(bot.account)
    if not (_ok(pos) and _ok(ords)):
        return _broker_error(acct, pos, ords, mem)
    views = symbol_views(bot)
    rows: dict[tuple[str, str], list[BrokerPosition]] = {}
    for r in pos.rows:
        rows.setdefault((r.symbol, r.product), []).append(r)
    findings: list[Finding] = []
    sightings: dict[tuple[str, str], Sighting] = {}
    for key in sorted(set(rows) | {(sym, MIS) for sym in views}):
        sv = views.get(key[0]) if key[1] == MIS else None
        f, s = _classify(acct, bot, key, rows.get(key, []), sv, pos, ords, mem.sightings.get(key))
        if s is not None:
            sightings[key] = s
        if f is not None:
            if acct is not Account.LIVE and f.cls not in _G1_OK:
                f = replace(f, g1_mismatch=True)
            findings.append(f)

    halts: list[HaltRequest] = []
    alerts: list[Alert] = []
    events: list[LogEvent] = []
    commands: list[Command] = []
    reported: dict[tuple[str, str], str] = {}
    for f in findings:
        key = (f.symbol, f.product)
        commands.extend(f.commands)
        if f.cls is ReconClass.PENDING:
            if key in mem.reported:
                reported[key] = mem.reported[key]     # a pending re-check does not re-announce a known problem
            continue
        sig = f"{f.cls}|{f.broker_qty}|{f.expected}"
        if f.cls is not ReconClass.REPAIRED:
            reported[key] = sig
        if f.cls is ReconClass.REPAIRED or mem.reported.get(key) != sig:
            h, a, e = _finding_actions(acct, f)
            halts.extend(h)
            alerts.extend(a)
            events.extend(e)

    status = max((f.cls for f in findings), key=_RANK.__getitem__, default=ReconClass.CLEAN)
    ok_streak = mem.ok_streak + 1
    clear = ()
    if ok_streak == STALE_RECON_CLEAR_OKS:
        clear = (HaltRequest("STALE_RECON", _scope(acct), HaltClears.AUTO, f"{ok_streak} good broker reads"),)
    clean = status is ReconClass.CLEAN
    if status != mem.status:                       # CLEAN <-> PENDING follows every fill: INFO, not WARN
        events.append(LogEvent("RECON", "INFO" if _RANK[status] <= _RANK[ReconClass.REPAIRED] else "WARN",
                               {"account": str(acct), "status": str(status), "prev": str(mem.status)}))
    memory = replace(mem, status=str(status), error_streak=0, ok_streak=ok_streak,
                     clean_streak=mem.clean_streak + 1 if clean else 0,
                     last_clean_mono=now if clean else mem.last_clean_mono,
                     sightings=sightings, reported=reported)
    return ReconResult(acct, status, tuple(findings), memory, tuple(halts), clear, tuple(alerts), tuple(commands),
                       tuple(events))


def _broker_error(acct: Account, pos: object, ords: object, mem: ReconMemory) -> ReconResult:
    errors = mem.error_streak + 1
    detail = next((getattr(s, "error", None) for s in (pos, ords) if s is not None and not s.ok), None)
    detail = detail or "no snapshot"
    halts: tuple[HaltRequest, ...] = ()
    alerts: tuple[Alert, ...] = ()
    if errors == STALE_RECON_ERRORS:
        reason = f"{errors} broker reads failed in a row: {detail}"
        halts = (HaltRequest("STALE_RECON", _scope(acct), HaltClears.AUTO, reason),)
        alerts = (Alert("WARN", "STALE_RECON", f"{acct}: {reason}", f"recon:{acct}:STALE_RECON"),)
    events = ()
    if mem.status != ReconClass.BROKER_ERROR:
        events = (LogEvent("RECON", "WARN", {"account": str(acct), "status": str(ReconClass.BROKER_ERROR),
                                             "prev": str(mem.status), "error": str(detail)}),)
    memory = replace(mem, status=str(ReconClass.BROKER_ERROR), error_streak=errors, ok_streak=0, clean_streak=0)
    return ReconResult(acct, ReconClass.BROKER_ERROR, (), memory, halts, (), alerts, (), events)


def _confirm(prev: Sighting | None, req_mono: float, net: int, expected: int, lf: float | None,
             gap: float) -> tuple[Sighting, bool]:
    """Keep the first agreeing sighting (still outside grace for the current last fill); confirmed once gap apart."""
    if (prev is not None and prev.broker_qty == net and prev.expected == expected
            and prev.req_mono > _outside_grace_floor(lf)):
        return prev, req_mono - prev.req_mono >= gap
    return Sighting(req_mono, net, expected), False


def _classify(acct: Account, bot: BotView, key: tuple[str, str], rows: list[BrokerPosition],
              sv: SymbolView | None, pos: PositionsSnapshot, ords: OrdersSnapshot,
              prev: Sighting | None) -> tuple[Finding | None, Sighting | None]:
    symbol, product = key
    net = sum(int(r.net_qty) for r in rows)
    exchange = rows[0].exchange if rows else symbol.split(":", 1)[0]
    if net < 0 and exchange == NFO:
        return _short(acct, symbol, product, net, sv, pos, ords, prev)
    if sv is None:                       # a non-bot symbol, or a non-MIS row of any symbol
        ack = bot.foreign_acks.get(key)
        if net == 0 or ack == net:
            return None, None
        sighting, confirmed = _confirm(prev, pos.req_mono, net, 0, None, RECON_CONFIRM_GAP_S)
        if not confirmed:
            return Finding(ReconClass.PENDING, symbol, product, net, 0, detail="foreign qty seen once"), sighting
        detail = "non-MIS row" if product != MIS else "position not placed by the bot"
        if ack is not None:
            detail += f"; acked qty {ack} differs"
        return Finding(ReconClass.FOREIGN, symbol, product, net, 0, detail=detail), sighting

    bot_share = net - sv.ack                       # acked foreign qty on this symbol is never the bot's
    if bot_share == sv.expected or (net == 0 and sv.expected == 0):
        return None, None
    base = dict(symbol=symbol, product=product, broker_qty=net, expected=sv.expected, legs=sv.legs)
    if not post_fill(pos, sv):
        return Finding(ReconClass.PENDING, detail="snapshot requested before last fill + lag", **base), None
    if in_grace(sv, pos.req_mono):
        return Finding(ReconClass.PENDING, detail="in grace (working order or fresh fill)", **base), None
    repairs, delta = _repairs(acct, sv, ords)
    if repairs and delta == bot_share - sv.expected:
        cmds = tuple(FetchTrades(acct, pk, b.broker_order_id) for pk, b in repairs)
        return Finding(ReconClass.REPAIRED, detail=f"bot order fills explain {delta:+d}", repairs=tuple(repairs),
                       commands=cmds, **base), None
    sighting, confirmed = _confirm(prev, pos.req_mono, net, sv.expected, sv.last_fill_mono, RECON_CONFIRM_GAP_S)
    if not confirmed:
        return Finding(ReconClass.PENDING, detail="difference seen once", **base), sighting
    if sv.expected <= 0 or bot_share > sv.expected:
        detail = "broker qty not explained by bot fills" + _lost_note(_lost_orders(acct, sv, ords, Side.BUY))
        return Finding(ReconClass.EXCESS, detail=detail, **base), sighting
    # Less at the broker than the bot holds. A positions net alone (a glitch, or a read that contradicts the orders)
    #   must never book a close and abandon a position the broker may still hold: the sale must show somewhere.
    if not post_fill(ords, sv):
        return Finding(ReconClass.PENDING, detail="orders view requested before last fill + lag", **base), sighting
    why = _drop_unbacked(sv, net, bot_share, rows, ords, product)
    if why is not None:
        return Finding(ReconClass.CONTRADICTION, detail=f"{why}: nothing inferred, the leg stays managed",
                       **base), sighting
    if bot_share > 0:
        return Finding(ReconClass.PARTIAL_EXTERNAL, detail="part of the bot qty left the account",
                       cap_qty=bot_share, **base), sighting
    cls, closes, cmds = _plan_closes(acct, bot, sv, rows, ords)
    detail = (f"{sum(c.qty for c in closes)} left the account; pnl {closes[0].pnl_status}"
              + _lost_note(_lost_orders(acct, sv, ords, Side.SELL)))
    return Finding(cls, detail=detail, closes=closes, commands=cmds, **base), sighting


def _drop_unbacked(sv: SymbolView, net: int, bot_share: int, rows: list[BrokerPosition], ords: OrdersSnapshot,
                   product: str) -> str | None:
    """Why the broker's views do not back a drop in the bot's qty (None: they do).

    Today's orders decide when they hold every fill the bot recorded on the symbol today: their net (Σ BUY − Σ SELL
    filled, plus what the bot carries from earlier sessions) must equal the positions net; a sale missing from them
    (orders lagging a manual close) or a positions read that dropped the qty (a glitch) is a disagreement either way.
    An orders read that misses the bot's own fills proves nothing; then only the positions row's own day sell qty,
    beyond the bot's known sells, can show that the qty was sold."""
    by_id = {b.broker_order_id: b for b in ords.orders if b.broker_order_id}
    by_tag = {b.tag: b for b in ords.orders if b.tag}
    unseen = []
    for o in sv.orders:
        if o.role == Role.EXTERNAL or o.filled_qty <= 0 or o.leg_id not in sv.today_legs:
            continue
        b = (by_id.get(o.broker_order_id) if o.broker_order_id else None) or by_tag.get(o.order_pk)
        if b is None or b.filled_qty < o.filled_qty:
            unseen.append(o.order_pk)
    if not unseen:
        onet = orders_net(ords.orders, sv.symbol, product) + sv.carried
        return None if onet == net else f"positions net {net} but today's orders add up to {onet}"
    sold = sum(r.sell_qty for r in rows) - sv.known_sell_qty
    if sold >= sv.expected - bot_share:
        return None
    return (f"no sale shows: today's orders lack the bot's fills of {', '.join(sorted(unseen))} and the positions "
            f"row has {max(sold, 0)} sold beyond the bot's own sells")


def _lost_orders(acct: Account, sv: SymbolView, ords: OrdersSnapshot, side: Side) -> list[BrokerOrder]:
    """Bot-tagged broker orders of `side` on the symbol that the DB recorded NOT_PLACED (every tag lookup missed
    them) but that the broker shows filled: real fills the bot's rows do not have. One whose broker id a DB row
    already holds (adopted later, or booked as an EXTERNAL row) is not lost."""
    by_pk = {o.order_pk: o for o in sv.orders}
    held = {o.broker_order_id for o in sv.orders if o.broker_order_id}
    out = []
    for b in sorted(ords.orders, key=_wall_key):
        if (b.symbol != sv.symbol or b.product != MIS or b.side != side or b.filled_qty <= 0
                or not is_bot_tag(b.tag, acct) or b.broker_order_id in held):
            continue
        o = by_pk.get(b.tag)
        if o is not None and o.role != Role.EXTERNAL and o.state == OrderState.NOT_PLACED and o.filled_qty == 0:
            out.append(b)
    return out


def _lost_note(lost: Sequence[BrokerOrder]) -> str:
    if not lost:
        return ""
    return (f"; bot order(s) {', '.join(b.tag for b in lost)} recorded NOT_PLACED are filled at the broker "
            f"({', '.join(f'{b.side} {b.filled_qty}' for b in lost)})")


def _repairs(acct: Account, sv: SymbolView, ords: OrdersSnapshot) -> tuple[list[tuple[str, BrokerOrder]], int]:
    """Bot orders whose broker filled_qty is ahead of the DB, and the signed open qty they add.

    Only a non-terminal order can still take the broker's fills. A terminal row is immutable (I9, DB trigger) and
    orders.handle records a contradicting report without applying it, so a terminal order the broker shows with more
    fills (a NOT_PLACED that existed, a short CANCELLED) leaves the gap unexplained: EXCESS (UNKNOWN_POSITION) or
    an external close once confirmed, never a REPAIRED that cannot be applied and would hold the account non-CLEAN
    with no halt."""
    by_id = {o.broker_order_id: o for o in sv.orders if o.broker_order_id}
    by_pk = {o.order_pk: o for o in sv.orders}
    out: list[tuple[str, BrokerOrder]] = []
    delta = 0
    for b in sorted(ords.orders, key=_wall_key):
        if b.symbol != sv.symbol or b.product != MIS or not is_bot_tag(b.tag, acct):
            continue
        o = by_id.get(b.broker_order_id) or by_pk.get(b.tag)
        # COVER fills never change the bot's open qty (model.leg_fill_totals), so they cannot explain a gap
        if (o is None or o.is_terminal or o.role in (Role.EXTERNAL, Role.COVER)
                or b.filled_qty <= o.filled_qty):
            continue
        out.append((o.order_pk, b))
        delta += (b.filled_qty - o.filled_qty) * (1 if o.side == Side.BUY else -1)
    return out, delta


def _short(acct: Account, symbol: str, product: str, net: int, sv: SymbolView | None, pos: PositionsSnapshot,
           ords: OrdersSnapshot, prev: Sighting | None) -> tuple[Finding, Sighting | None]:
    expected = sv.expected if sv else 0
    legs = sv.legs if sv else ()
    if sv is None or not post_fill(pos, sv) or in_grace(sv, pos.req_mono):
        return Finding(ReconClass.SHORT, symbol, product, net, expected, legs, "short; cover not evaluated"), None
    sighting, confirmed = _confirm(prev, pos.req_mono, net, expected, sv.last_fill_mono, SHORT_CONFIRM_GAP_S)
    cover, detail = None, "short seen once"
    if confirmed:
        sold = _bot_oversold(acct, sv, ords) if post_fill(ords, sv) else None
        if not sv.today_legs:
            detail = "short on a symbol the bot did not trade today: alert only"
        elif sold != -net:
            detail = f"bot fills explain {sold} of {-net}: alert only"
        else:
            cover, detail = CoverPlan(symbol, -net, _cover_leg(acct, sv, ords)), "bot oversold: cover"
    return Finding(ReconClass.SHORT, symbol, product, net, expected, legs, detail, cover=cover), sighting


def _cover_leg(acct: Account, sv: SymbolView, ords: OrdersSnapshot) -> int:
    """The leg a COVER hangs off: the newest of today's legs whose own bot-tagged broker fills are net short (the leg
    that oversold), else the newest of today's legs. close() books a leg's COVER orders with its trade, so the cover
    must not land on another book's leg holding the same symbol (its trade would turn UNRESOLVED and stall its G1)."""
    by_id = {o.broker_order_id: o for o in sv.orders if o.broker_order_id}
    by_pk = {o.order_pk: o for o in sv.orders}
    nets: dict[int, int] = {}
    for b in ords.orders:
        if b.symbol != sv.symbol or b.product != MIS or not is_bot_tag(b.tag, acct):
            continue
        o = by_id.get(b.broker_order_id) or by_pk.get(b.tag)
        if o is not None and o.leg_id in sv.today_legs:
            nets[o.leg_id] = nets.get(o.leg_id, 0) + (b.filled_qty if b.side == Side.BUY else -b.filled_qty)
    short = [lid for lid, n in nets.items() if n < 0]
    return max(short) if short else max(sv.today_legs)


def _bot_oversold(acct: Account, sv: SymbolView, ords: OrdersSnapshot) -> int | None:
    """Bot-tagged SELL fills − bot-tagged BUY fills on the symbol today; None if a bot tag is unknown to the DB."""
    known = _known_ids(sv)
    n = 0
    for b in ords.orders:
        if b.symbol != sv.symbol or b.product != MIS or not is_bot_tag(b.tag, acct):
            continue
        if b.tag not in known and b.broker_order_id not in known:
            return None
        n += b.filled_qty if b.side == Side.SELL else -b.filled_qty
    return n


def _plan_closes(acct: Account, bot: BotView, sv: SymbolView, rows: list[BrokerPosition],
                 ords: OrdersSnapshot) -> tuple[ReconClass, tuple[ExternalClose, ...], tuple[Command, ...]]:
    """close_external() inputs for every open leg on a symbol the broker shows flat (§9.3 EXTERNAL_CLOSE)."""
    by_id = {lg.leg_id: lg for lg in bot.legs}
    needs = []
    for lid in sv.legs:
        entry, exit_ = leg_fill_totals(by_id[lid].leg_orders())
        needs.append((by_id[lid], max(0, entry - exit_)))
    missing = sum(q for _, q in needs)
    known = _known_ids(sv)
    # a bot SELL recorded NOT_PLACED that the broker shows filled closed the qty too: its fills are real, and its
    #   price makes the close FINAL (it becomes an EXTERNAL row like any sale the bot's rows do not hold)
    lost = {b.broker_order_id for b in _lost_orders(acct, sv, ords, Side.SELL)}
    cands = sorted((b for b in ords.orders
                    if b.symbol == sv.symbol and b.product == MIS and b.side == Side.SELL and b.filled_qty > 0
                    and ((b.broker_order_id not in known and b.tag not in known) or b.broker_order_id in lost)
                    and (sv.entry_wall is None or (b.order_wall is not None and b.order_wall >= sv.entry_wall))),
                   key=_wall_key)
    if cands and sum(b.filled_qty for b in cands) == missing and all(_positive(b.avg_px) for b in cands):
        status, est = PnlStatus.FINAL, None
    else:
        q = sum(r.sell_qty for r in rows) - sv.known_sell_qty
        v = sum(r.sell_value for r in rows) - sv.known_sell_value
        if q >= missing > 0 and v > 0:
            status, est = PnlStatus.ESTIMATED, round(v / q, 4)   # (sell_value − bot sell value)/(sell_qty − bot sells)
        else:
            status, est = PnlStatus.UNRESOLVED, None

    queue = [[b, b.filled_qty] for b in cands]
    used: list[BrokerOrder] = []
    per_leg = []
    i = 0
    for leg, need in needs:
        px_leg = est if status is PnlStatus.ESTIMATED else bot.last_rb.get(leg.leg_id)
        fills, leg_orders, left = [], [], need
        while left > 0 and i < len(queue):
            b, avail = queue[i]
            take = min(left, avail)
            fills.append(ExternalFill(b.broker_order_id, take, b.avg_px if status is PnlStatus.FINAL else px_leg,
                                      b.order_wall))
            if b not in leg_orders:
                leg_orders.append(b)
            if b not in used:
                used.append(b)
            left -= take
            queue[i][1] -= take
            if queue[i][1] == 0:
                i += 1
        if left > 0:
            fills.append(ExternalFill(None, left, px_leg, None))
        per_leg.append((leg, need, fills, tuple(leg_orders)))
    _mark_shared_orders(per_leg)

    threshold = ist_datetime(max(leg.session for leg, _ in needs), bot.squareoff_at)
    squareoff = bool(used) and all(is_broker_squareoff(b, threshold, bot.api_placed_by) for b in used)
    kind = ReconClass.BROKER_SQUAREOFF if squareoff else ReconClass.EXTERNAL_CLOSE
    closes = tuple(ExternalClose(leg.leg_id, need, kind, status, tuple(fills), orders)
                   for leg, need, fills, orders in per_leg)
    cmds = tuple(FetchTrades(acct, external_order_pk(b.broker_order_id), b.broker_order_id) for b in used)
    return kind, closes, cmds


def _mark_shared_orders(per_leg: list) -> None:
    """Annotate the fills of a broker order that the plan splits across several legs with each leg's part
    (ExternalFill.part), when every part is priced. per_leg is in leg_id order, each leg taking one consecutive
    piece of the order, so the parts are [0, a), [a, b), ... of the qty the plan takes from it."""
    takes: dict[str, list[tuple[list, int]]] = {}
    for _, _, fills, _ in per_leg:
        for i, f in enumerate(fills):
            if f.broker_order_id is not None:
                takes.setdefault(f.broker_order_id, []).append((fills, i))
    for spots in takes.values():
        if len(spots) < 2 or not all(_positive(fills[i].px) for fills, i in spots):
            continue
        total = sum(fills[i].qty for fills, i in spots)
        value = math.fsum(fills[i].qty * fills[i].px for fills, i in spots)
        start = 0
        for fills, i in spots:
            f = fills[i]
            fills[i] = replace(f, part=(start, start + f.qty, total, value))
            start += f.qty


def _finding_actions(acct: Account, f: Finding) -> tuple[list[HaltRequest], list[Alert], list[LogEvent]]:
    """Halts, alerts and events for a finding that is new or changed (§9.3 Action column, §12.6 severities)."""
    payload = {"account": str(acct), "class": str(f.cls), "symbol": f.symbol, "product": f.product,
               "broker_qty": f.broker_qty, "expected": f.expected, "legs": list(f.legs), "detail": f.detail}
    text = f"{acct} {f.cls} {f.symbol} {f.product}: broker {f.broker_qty}, bot {f.expected}. {f.detail}"
    dedup = f"recon:{acct}:{f.cls}:{f.symbol}:{f.product}:{f.broker_qty}"
    scope = _scope(acct)
    if f.cls is ReconClass.SHORT:
        return ([HaltRequest("SHORT_DETECTED", "GLOBAL", HaltClears.ACK, text)],
                [Alert("CRITICAL", "SHORT_DETECTED", text, dedup)], [LogEvent("SHORT_DETECTED", "CRITICAL", payload)])
    if f.cls in (ReconClass.FOREIGN, ReconClass.EXCESS):
        return ([HaltRequest("UNKNOWN_POSITION", scope, HaltClears.ACK, text)],
                [Alert("CRITICAL", "UNKNOWN_POSITION", text, dedup)],
                [LogEvent("UNKNOWN_POSITION", "CRITICAL", payload)])
    if f.cls in (ReconClass.PARTIAL_EXTERNAL, ReconClass.EXTERNAL_CLOSE, ReconClass.CONTRADICTION):
        return ([HaltRequest("MISMATCH", scope, HaltClears.ACK, text)], [Alert("CRITICAL", "MISMATCH", text, dedup)],
                [LogEvent("MISMATCH", "CRITICAL", payload)])
    if f.cls is ReconClass.BROKER_SQUAREOFF:       # CRITICAL, no MISMATCH halt; close_external logs the close
        return [], [Alert("CRITICAL", "BROKER_SQUAREOFF", text, dedup)], []
    if f.cls is ReconClass.REPAIRED:
        return [], [], [LogEvent("FILLS_REPAIRED", "INFO",
                                 {**payload, "orders": [pk for pk, _ in f.repairs]})]
    return [], [], []


# ---------------------------------------------------------------------------------------------------------------
# §9.4 exit cap, external wait, verify-flat
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Cap:
    """broker_cap() result. qty None means WAIT: carry wait_start into the next call."""

    qty: int | None
    source: str                           # "POSITIONS" | "ORDERS" | "POSITIONS_BEFORE_DECISION" |
    #                                       "ORDERS_BEFORE_DECISION" | "UNCAPPED" | "WAIT"
    wait_start: float | None
    commands: tuple[Command, ...] = ()
    alerts: tuple[Alert, ...] = ()
    events: tuple[LogEvent, ...] = ()

    @property
    def wait(self) -> bool:
        return self.qty is None


def broker_cap(sv: SymbolView, open_qty: int, pos: PositionsSnapshot | None, ords: OrdersSnapshot | None,
               wait_start: float | None, now: float, *, account: Account, pos_in_flight: bool = False,
               ords_in_flight: bool = False, decided_mono: float | None = None, pos_failed_mono: float | None = None,
               ords_failed_mono: float | None = None) -> Cap:
    """The most the exit loop may sell on this symbol now (§9.4), from the first usable source:
    post-fill positions net long; else (after POSITIONS_WAIT_MAX_S) the post-fill orders view; else (after
    BROKER_READS_FAIL_UNCAP_S) a post-fill read from before the decision, CRITICAL, or with none the bot's own open
    qty with EXIT_UNCAPPED. Acked foreign qty is never sellable. While another bot order on the symbol is working the
    answer is WAIT, so exits on one symbol never overlap.

    decided_mono: when this attempt was decided: the leg's exit decision, or the end of its previous exit attempt if
    later (exit_loop). Decision B1 (strict, EXIT_CAP_PRIOR_READ_S = 0): only a read requested at or after it caps the
    attempt. A position closed before it (a manual close in the Kite app, an RMS square-off) would otherwise be sold a
    second time, as a naked short (G3). Such an attempt waits for one read made after its decision; the engine steps
    the exit loop as soon as a read arrives, so this costs one read round trip per attempt.
    pos_failed_mono / ords_failed_mono: request times of the newest failed reads. When the positions read made after
    the decision failed, the orders view is asked at once; failed reads are re-asked no faster than
    VERIFY_FLAT_RECHECK_S. However those reads fail or hang, a read from before the decision caps nothing until
    BROKER_READS_FAIL_UNCAP_S has passed. Then the newest post-fill positions read (else orders read) from before it is
    the best evidence left (EXIT_CAP_BEFORE_DECISION, CRITICAL: a close made after that read would be sold again); it
    never lets more go than EXIT_UNCAPPED, which ignores the broker altogether.
    """
    acct = Account(account)
    if sv.in_flight:
        return Cap(None, "WAIT", wait_start)
    lf = sv.last_fill_mono

    def usable(snap: object) -> bool:
        return post_fill(snap, lf) and (decided_mono is None
                                        or snap.req_mono >= decided_mono - EXIT_CAP_PRIOR_READ_S - _TIME_EPS)

    if usable(pos):
        return Cap(max(0, net_qty(pos, sv.symbol) - sv.ack), "POSITIONS", None)
    start = now if wait_start is None else wait_start
    cmds: list[Command] = []
    if _need_fetch(pos, lf, now, pos_in_flight, decided_mono, pos_failed_mono):
        cmds.append(FetchPositions(acct, Priority.EXIT))
    pos_failed = (decided_mono is not None and pos_failed_mono is not None and pos_failed_mono >= decided_mono
                  and (not _ok(pos) or pos_failed_mono > pos.req_mono))   # the newest positions read failed
    if not pos_failed and now - start < POSITIONS_WAIT_MAX_S:
        return Cap(None, "WAIT", start, tuple(cmds))
    if usable(ords):
        return Cap(max(0, orders_net(ords.orders, sv.symbol) - sv.ack), "ORDERS", None, tuple(cmds))
    if _need_fetch(ords, lf, now, ords_in_flight, decided_mono, ords_failed_mono):
        cmds.append(FetchOrders(acct, Priority.EXIT))
    if now - start < BROKER_READS_FAIL_UNCAP_S:
        return Cap(None, "WAIT", start, tuple(cmds))
    older = None                                         # a post-fill read that usable() refused (before the decision)
    if post_fill(pos, lf):
        older = ("POSITIONS", pos.req_mono, net_qty(pos, sv.symbol))
    elif post_fill(ords, lf):
        older = ("ORDERS", ords.req_mono, orders_net(ords.orders, sv.symbol))
    if older is not None:
        name, req, net = older
        qty = max(0, net - sv.ack)
        before = 0.0 if decided_mono is None else max(0.0, decided_mono - req)
        text = (f"{acct} {sv.symbol}: no broker read made after the exit decision for {now - start:.0f}s; selling at "
                f"most {qty}, the {name.lower()} read from {before:.1f}s before it. A close since then (by hand, or "
                "an RMS square-off) would be sold again: check Kite positions")
        key = f"exit_cap_before_decision:{acct}:{sv.symbol}"
        payload = {"account": str(acct), "symbol": sv.symbol, "source": name, "qty": qty, "before_s": round(before, 3)}
        return Cap(qty, f"{name}_BEFORE_DECISION", None, tuple(cmds),
                   (Alert("CRITICAL", "EXIT_CAP_BEFORE_DECISION", text, key),),
                   (LogEvent("EXIT_CAP_BEFORE_DECISION", "CRITICAL", payload),))
    text = f"{acct} {sv.symbol}: no post-fill broker read for {now - start:.0f}s; exiting on own fills ({open_qty})"
    return Cap(max(0, open_qty), "UNCAPPED", None, tuple(cmds),
               (Alert("WARN", "EXIT_UNCAPPED", text, f"exit_uncapped:{acct}:{sv.symbol}"),),
               (LogEvent("EXIT_UNCAPPED", "WARN", {"account": str(acct), "symbol": sv.symbol, "qty": open_qty}),))


def external_wait(sv: SymbolView, pos: PositionsSnapshot | None, ords: OrdersSnapshot | None, now: float, *,
                  account: Account, pos_in_flight: bool = False, ords_in_flight: bool = False,
                  pos_failed_mono: float | None = None, ords_failed_mono: float | None = None) -> tuple[Command, ...]:
    """§9.4 cap == 0 while the bot still holds qty: place nothing and ask for a recon at exit priority.

    Recon then confirms EXTERNAL_CLOSE, or a later post-fill snapshot shows the qty again (lag) and exits resume.
    """
    acct = Account(account)
    cmds: list[Command] = []
    if _need_fetch(pos, sv.last_fill_mono, now, pos_in_flight, failed=pos_failed_mono):
        cmds.append(FetchPositions(acct, Priority.EXIT))
    if _need_fetch(ords, sv.last_fill_mono, now, ords_in_flight, failed=ords_failed_mono):
        cmds.append(FetchOrders(acct, Priority.EXIT))
    return tuple(cmds)


@dataclass(frozen=True, slots=True)
class VerifyState:
    """Per-leg verify-flat memory, kept by the engine in RAM (re-anchored to now after a restart)."""

    since_mono: float
    residual: Sighting | None = None
    critical_sent: bool = False


@dataclass(frozen=True, slots=True)
class VerifyDecision:
    outcome: str                          # "CLOSED" | "RESIDUAL" (CLOSED + residual, never sold) | "SHORT" | "WAIT"
    state: VerifyState
    residual_qty: int = 0
    via: str | None = None                # "POSITIONS" | "ORDERS" (VERIFY_FLAT_VIA_ORDERS)
    commands: tuple[Command, ...] = ()
    halts: tuple[HaltRequest, ...] = ()
    alerts: tuple[Alert, ...] = ()
    events: tuple[LogEvent, ...] = ()


def verify_flat(leg_id: int, sv: SymbolView, pos: PositionsSnapshot | None, ords: OrdersSnapshot | None,
                st: VerifyState, now: float, *, account: Account, pos_in_flight: bool = False,
                ords_in_flight: bool = False, pos_failed_mono: float | None = None,
                ords_failed_mono: float | None = None) -> VerifyDecision:
    """§9.4, after the leg's own open qty reached 0. sv.expected is what OTHER open legs still hold there.

    post-fill net matches the bot -> CLOSED; a larger net confirmed twice (RECON_CONFIRM_GAP_S apart, both
    requested more than FRESH_FILL_GRACE_S after the last fill) -> RESIDUAL (closed, MISMATCH, the residual becomes FOREIGN and is
    never sold); net < 0 -> SHORT (recon raises the halt); positions unusable for VERIFY_FLAT_TIMEOUT_S -> the
    post-fill orders view may close it; otherwise WAIT, CRITICAL once after VERIFY_FLAT_CRITICAL_S. A failed read
    (pos_failed_mono / ords_failed_mono: request times of the newest failed reads) is re-asked no faster than
    VERIFY_FLAT_RECHECK_S.
    """
    acct = Account(account)
    lf = sv.last_fill_mono
    base = {"account": str(acct), "leg_id": leg_id, "symbol": sv.symbol}
    if not sv.in_flight and post_fill(pos, lf):
        net = net_qty(pos, sv.symbol)
        if net < 0:
            return VerifyDecision("SHORT", st, events=(LogEvent("VERIFY_FLAT", "CRITICAL",
                                                                {**base, "outcome": "SHORT", "net": net}),))
        excess = net - sv.expected - sv.ack
        if excess <= 0:
            return VerifyDecision("CLOSED", st, via="POSITIONS",
                                  events=(LogEvent("VERIFY_FLAT", "INFO", {**base, "outcome": "CLOSED", "net": net}),))
        if pos.req_mono > _outside_grace_floor(lf):
            prev = st.residual
            if not (prev is not None and prev.broker_qty == net and prev.expected == sv.expected
                    and prev.req_mono > _outside_grace_floor(lf)):
                st = replace(st, residual=Sighting(pos.req_mono, net, sv.expected))
            elif pos.req_mono - prev.req_mono >= RECON_CONFIRM_GAP_S:
                text = f"{acct} {sv.symbol}: broker still shows {excess} after leg {leg_id} closed; never sold"
                return VerifyDecision(
                    "RESIDUAL", st, residual_qty=excess, via="POSITIONS",
                    halts=(HaltRequest("MISMATCH", _scope(acct), HaltClears.ACK, text),),
                    alerts=(Alert("CRITICAL", "MISMATCH", text, f"verify_flat:{acct}:{leg_id}"),),
                    events=(LogEvent("VERIFY_FLAT", "CRITICAL",
                                     {**base, "outcome": "RESIDUAL", "net": net, "residual": excess}),))
    usable_from = st.since_mono if lf is None else max(st.since_mono, lf + POSITIONS_POST_FILL_LAG_S)
    positions_failing = not post_fill(pos, lf) and now - usable_from > VERIFY_FLAT_TIMEOUT_S
    if not sv.in_flight and positions_failing and post_fill(ords, lf):
        onet = orders_net(ords.orders, sv.symbol)
        if onet >= 0 and onet - sv.expected - sv.ack <= 0:
            return VerifyDecision("CLOSED", st, via="ORDERS", events=(LogEvent(
                "VERIFY_FLAT", "WARN", {**base, "outcome": "VERIFY_FLAT_VIA_ORDERS", "orders_net": onet}),))
    cmds: list[Command] = []
    if not sv.in_flight:
        if _need_fetch(pos, lf, now, pos_in_flight, failed=pos_failed_mono):
            cmds.append(FetchPositions(acct, Priority.EXIT))
        if positions_failing and _need_fetch(ords, lf, now, ords_in_flight, failed=ords_failed_mono):
            cmds.append(FetchOrders(acct, Priority.EXIT))
    alerts: tuple[Alert, ...] = ()
    if now - st.since_mono >= VERIFY_FLAT_CRITICAL_S and not st.critical_sent:
        text = f"{acct} {sv.symbol}: leg {leg_id} not verified flat after {now - st.since_mono:.0f}s"
        alerts = (Alert("CRITICAL", "VERIFY_FLAT_STUCK", text, f"verify_flat_stuck:{acct}:{leg_id}"),)
        st = replace(st, critical_sent=True)
    return VerifyDecision("WAIT", st, commands=tuple(cmds), alerts=alerts)


# ---------------------------------------------------------------------------------------------------------------
# Cadence and fetch helpers
# ---------------------------------------------------------------------------------------------------------------
def recon_due(now_mono: float, now_wall: datetime, last_req_mono: float | None, open_legs: bool,
              interval_s: float = RECON_INTERVAL_S, window_end: dtime | None = None) -> bool:
    """§9.3 periodic cadence inside RECON_WINDOW: every interval_s, every 5 s while the account has open legs.
    window_end (the engine's last EOD recon) can only extend the window past RECON_WINDOW's end, never shorten it.

    On-demand recons (verify-flat, external-wait, RUN_RECON), startup and 15:27/15:31 are the engine's calls.
    """
    t = to_ist(now_wall).time()
    end = RECON_WINDOW[1] if window_end is None else max(RECON_WINDOW[1], window_end)
    if not RECON_WINDOW[0] <= t <= end:
        return False
    every = min(interval_s, RECON_OPEN_LEGS_INTERVAL_S) if open_legs else interval_s
    return last_req_mono is None or now_mono - last_req_mono >= every


def recon_fetches(account: Account, priority: int = Priority.RECON) -> tuple[Command, ...]:
    """The two reads one reconcile() needs."""
    return FetchPositions(Account(account), priority), FetchOrders(Account(account), priority)


def recovery_fetches(account: Account) -> tuple[Command, ...]:
    """§9.6 step 1 reads (retry RECOVERY_FETCH_TRIES times, RECOVERY_FETCH_GAP_S apart, then plan anyway)."""
    acct = Account(account)
    return FetchOrders(acct, Priority.EXIT), FetchPositions(acct, Priority.EXIT), FetchTradebook(acct)


def entries_may_resume(recon: Mapping[Account, ReconMemory], accounts: Iterable[Account], *, feed_warm: bool,
                       prep_done: bool, entry_halted: bool) -> bool:
    """§9.6: entries resume only after CLEAN_RECONS_TO_RESUME consecutive CLEAN recons on every account, a warm
    feed, finished prep and no entry halt. A missing account fails closed."""
    if not feed_warm or not prep_done or entry_halted:
        return False
    for a in accounts:
        m = recon.get(Account(a))
        if m is None or m.clean_streak < CLEAN_RECONS_TO_RESUME:
            return False
    return True


# ---------------------------------------------------------------------------------------------------------------
# §9.6 recovery planner
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TagImport:
    """Bot-tagged broker orders of today missing from the DB (lost writes), grouped by leg seq_no."""

    seq_no: int
    leg_id: int | None                    # the existing leg (account, session, seq_no); None: create a RECOVERED one
    book: str                             # that leg's book, or "RECOVERED:<account>" (strategy "_recovered")
    orders: tuple[BrokerOrder, ...]


@dataclass(frozen=True, slots=True)
class StaleStructure:
    """A structure of an earlier session still non-terminal at startup. The engine never trades it again.

    action: CLOSE_UNRESOLVED (LIVE holding or unresolved: CLOSED_EXTERNAL, pnl UNRESOLVED, HALT(MISMATCH));
    AWAIT_PAPER_SQUAREOFF (PAPER/SHADOW holding: the simulator's startup square-off, then recon, closes it);
    CLOSE (fully exited before the stop: close() from its fills); VOID (nothing was executed).
    """

    structure_id: int
    session: date
    action: str
    broker_qty: int | None                # broker MIS net on its symbols now; None when positions are unknown
    working: tuple[str, ...] = ()         # its orders still non-terminal in the DB (outcome unknowable today)


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """What the engine does in phase RECOVERING for one account (then reconcile() twice >= 3 s apart).

    Intents are never replayed; nothing here places an order. Cascades (an applied snapshot ending an ENTERING
    leg, a lookup ending in NOT_PLACED) run through the normal order and position state machines.
    """

    account: Account
    apply: tuple[tuple[str, BrokerOrder], ...] = ()     # (order_pk, broker order): apply as source RECOVERY
    lookups: tuple[str, ...] = ()                       # SUBMITTING/UNKNOWN not found: LookupTag twice -> NOT_PLACED
    vanished: tuple[str, ...] = ()                      # a working order sent before the orders read was asked that
    #                                                     the read does not show (INVARIANT)
    imports: tuple[TagImport, ...] = ()
    open_legs: tuple[int, ...] = ()                     # ENTERING, entry terminal with fills -> OPEN
    void_legs: tuple[tuple[int, str], ...] = ()         # ENTERING, entry terminal without fills -> VOID(reason)
    void_structures: tuple[tuple[int, str], ...] = ()   # RESERVED -> VOID("RESTART")
    flatten_legs: tuple[int, ...] = ()                  # restart at/after EOD_FLATTEN -> EXITING(RECOVERY_FLATTEN)
    stale: tuple[StaleStructure, ...] = ()
    halts: tuple[HaltRequest, ...] = ()
    alerts: tuple[Alert, ...] = ()
    commands: tuple[Command, ...] = ()
    events: tuple[LogEvent, ...] = ()


def stale_action(account: Account, legs: Sequence[Leg]) -> str:
    """§9.6 step 7: what becomes of one structure of an earlier session, from its legs' orders (StaleStructure):
    VOID when it never sent an order; on LIVE, CLOSE_UNRESOLVED while it holds qty or an order is still open
    (earlier days' orders are not in the API); otherwise AWAIT_PAPER_SQUAREOFF while it holds qty (the simulator
    settles it, recon books the BROKER_SQUAREOFF), CLOSE when it exited fully, VOID when nothing executed."""
    sent = [o for lg in legs for o in lg.leg_orders() if o.role != Role.EXTERNAL]
    entry = held = 0
    for lg in legs:
        e, x = leg_fill_totals(lg.leg_orders())
        entry, held = entry + e, held + max(0, e - x)
    if not sent:
        return "VOID"
    if Account(account) is Account.LIVE and (held > 0 or any(not o.is_terminal for o in sent)):
        return "CLOSE_UNRESOLVED"
    if held > 0:
        return "AWAIT_PAPER_SQUAREOFF"
    return "CLOSE" if entry > 0 else "VOID"


def carried_closes(bot: BotView, symbol: str, record: Sequence[BrokerOrder]) -> tuple[ExternalClose, ...]:
    """§9.6 step 7 backstop (PAPER/SHADOW; review round 2): close the legs of earlier sessions that still hold
    `symbol` after the broker closed the position on an earlier day (FetchSquareoffs: its net at the start of today
    is 0): a square-off or settlement the bot never booked (it was down, or stopped before recon could confirm it).
    Today's orders cannot show that close, so recon held it as a CONTRADICTION for good. record: the broker's own
    square-off orders of those days. Planned as recon plans a confirmed close (_plan_closes) with the record as
    the orders view: BROKER_SQUAREOFF, FINAL at the record's prices when its orders explain the qty; otherwise
    UNRESOLVED, each fill priced at the last realizable bid but never above the record's lowest price (never better
    than the record). () when the record shows no close at all: the caller closes the legs UNRESOLVED, as a LIVE
    leftover is (CLOSE_UNRESOLVED)."""
    acct = Account(bot.account)
    legs = tuple(lg for lg in bot.legs if lg.account == acct and lg.inst.symbol == symbol
                 and lg.session < bot.session and not lg.is_terminal)
    sub = replace(bot, legs=legs)
    sv = symbol_views(sub).get(symbol)
    if sv is None or not sv.legs:
        return ()
    ords = OrdersSnapshot(acct, True, tuple(b for b in record if b.symbol == symbol), 0.0, 0.0)
    _, closes, _ = _plan_closes(acct, sub, sv, [], ords)
    if not any(f.broker_order_id for c in closes for f in c.fills):
        return ()
    if closes[0].pnl_status is not PnlStatus.FINAL:
        prices = [b.avg_px for b in ords.orders if _positive(b.avg_px)]
        cap = min(prices) if prices else None
        closes = tuple(replace(c, fills=tuple(       # a capped share is booked on its own (no shared OrderPart)
            replace(f, px=min(f.px, cap), part=None) if cap is not None and f.px is not None else f
            for f in c.fills)) for c in closes)
    return closes


def plan_recovery(*, account: Account, session: date, now_wall: datetime, eod_flatten_at: datetime,
                  structures: Sequence[Structure], legs: Sequence[Leg], trade_qty: Mapping[str, int],
                  orders: OrdersSnapshot | None, positions: PositionsSnapshot | None,
                  tradebook_ok: bool = True, import_tags: bool = True) -> RecoveryPlan:
    """§9.6 steps 2-5 and 7 for one account, from the step-1 reads (orders, positions, tradebook).

    structures: this account's non-terminal structures (any session). legs: their legs plus every leg of this
    session, bound to their orders. trade_qty: order_pk -> Σ qty of its TRADE fills.

    import_tags=False (LIVE in the laptop profile, which sends no LIVE order): today's bot-tagged orders that the DB
    lacks are another titan2's (EC2's), never this process's. Step 3 imports none of them, so nothing here manages or
    sells them; it raises HALT(UNKNOWN_POSITION, ACCOUNT, ACK) with one CRITICAL alert instead.

    Step 2 leaves out a non-terminal bot order sent after the orders read was asked (submitted_mono >
    orders.req_mono): that read could not show it. Exits run during RECOVERING, and the plan waits for the tradebook
    read and its retries, so such an order is routine, not vanished (review F-7: it raised a false INVARIANT halt).
    It is not looked up or applied either; its own order timers resolve it (polls, the cancel, tag lookups, the
    late-accept window). An order from before the restart has its submitted_mono re-anchored to the first load,
    which is never after a recovery read is asked (it may be the same instant, hence the strict >), so a working
    order really missing at the broker is still reported.
    """
    acct = Account(account)
    scope = _scope(acct)
    halts: list[HaltRequest] = []
    alerts: list[Alert] = []
    cmds: list[Command] = []
    events: list[LogEvent] = []
    reads_ok = _ok(orders) and _ok(positions) and tradebook_ok
    if not reads_ok:
        text = f"{acct}: recovery broker reads failed after {RECOVERY_FETCH_TRIES} tries; exits use §9.4 caps"
        halts.append(HaltRequest("STALE_RECON", scope, HaltClears.AUTO, text))
        alerts.append(Alert("WARN", "STALE_RECON", text, f"recovery:{acct}:STALE_RECON"))

    legs = sorted((lg for lg in legs if lg.account == acct), key=lambda lg: lg.leg_id)
    live_structs = {s.structure_id: s for s in structures if s.account == acct and not s.is_terminal}
    stale_ids = {sid for sid, s in live_structs.items() if s.session < session}
    current = [lg for lg in legs if lg.session == session and lg.structure_id not in stale_ids]
    db_pks: set[str] = set()
    db_ids: set[str] = set()
    for lg in legs:
        for o in lg.leg_orders():
            db_pks.add(o.order_pk)
            if o.broker_order_id:
                db_ids.add(o.broker_order_id)

    # 2. match every non-terminal bot order of this session to the broker's orders
    apply: list[tuple[str, BrokerOrder]] = []
    lookups: list[str] = []
    vanished: list[str] = []
    by_id = {b.broker_order_id: b for b in orders.orders} if _ok(orders) else {}
    by_tag = {b.tag: b for b in orders.orders if b.tag} if _ok(orders) else {}
    for lg in current:
        for o in lg.leg_orders():
            if o.role == Role.EXTERNAL or o.is_terminal:
                continue
            if not _ok(orders):                         # resolve each order on its own
                if o.broker_order_id:
                    cmds.append(PollOrder(acct, o.order_pk, o.broker_order_id))
                else:
                    lookups.append(o.order_pk)
                continue
            if o.submitted_mono is not None and o.submitted_mono > orders.req_mono:
                continue                                # sent after the read was asked: it could not show it (F-7)
            b = (by_id.get(o.broker_order_id) if o.broker_order_id else None) or by_tag.get(o.order_pk)
            if b is not None:
                apply.append((o.order_pk, b))
            elif o.state in _NOT_SENT:
                lookups.append(o.order_pk)
            else:
                vanished.append(o.order_pk)
                text = f"{acct} order {o.order_pk} ({o.state}) is not in the broker's orders: working order vanished"
                halts.append(HaltRequest("INVARIANT", "GLOBAL", HaltClears.ACK, text))
                alerts.append(Alert("CRITICAL", "INVARIANT", text, f"recovery:{acct}:vanished:{o.order_pk}"))
                events.append(LogEvent("INVARIANT_VIOLATION", "CRITICAL",
                                       {"code": "WORKING_ORDER_VANISHED", "account": str(acct),
                                        "order_pk": o.order_pk, "state": str(o.state)}))
                cmds.append(PollOrder(acct, o.order_pk, o.broker_order_id))

    # 3. today's bot tags missing from the DB
    imports: list[TagImport] = []
    if _ok(orders):
        groups: dict[int, list[BrokerOrder]] = {}
        for b in sorted(orders.orders, key=_wall_key):
            p = parse_tag(b.tag)
            if p is None or p.account != acct or p.session != session:
                continue
            if b.tag in db_pks or b.broker_order_id in db_ids:
                continue
            groups.setdefault(p.seq_no, []).append(b)
        if groups and not import_tags:
            found = [b for seq_no in sorted(groups) for b in groups[seq_no]]
            text = (f"titan2 {acct} orders exist in this Kite account today ({len(found)} order(s) on "
                    f"{', '.join(sorted({b.symbol for b in found}))}) that this titan2.db did not place (another "
                    f"titan2 did, such as EC2's). This process sends no {acct} order (the laptop profile), so it does "
                    "not import or manage them: manage them in Kite or on EC2")
            halts.append(HaltRequest("UNKNOWN_POSITION", scope, HaltClears.ACK, text))
            alerts.append(Alert("CRITICAL", "UNKNOWN_POSITION", text, f"recovery:{acct}:not_imported"))
            events.append(LogEvent("UNKNOWN_POSITION", "CRITICAL", {
                "account": str(acct), "not_imported": [b.tag for b in found],
                "broker_order_ids": [b.broker_order_id for b in found]}))
            groups = {}
        by_seq = {lg.seq_no: lg for lg in legs if lg.session == session}
        for seq_no in sorted(groups):
            lg = by_seq.get(seq_no)
            book = lg.book if lg is not None else f"RECOVERED:{acct}"
            imports.append(TagImport(seq_no, lg.leg_id if lg else None, book, tuple(groups[seq_no])))
            text = (f"{acct}: {len(groups[seq_no])} bot order(s) of seq {seq_no} missing from the DB; "
                    + (f"attached to leg {lg.leg_id}" if lg else f"new structure in {book}"))
            alerts.append(Alert("CRITICAL", "RECOVERED_ORDERS", text, f"recovery:{acct}:import:{seq_no}"))

    # 4. LIVE: trades for terminal orders whose TRADE fills do not yet explain filled_qty
    if acct is Account.LIVE:
        for lg in current:
            for o in lg.leg_orders():
                if (o.is_terminal and o.filled_qty > 0 and o.broker_order_id
                        and trade_qty.get(o.order_pk, 0) != o.filled_qty):
                    cmds.append(FetchTrades(acct, o.order_pk, o.broker_order_id))

    # 5. structures and legs the DB left half-way
    void_structures = [(sid, "RESTART") for sid, s in sorted(live_structs.items())
                       if sid not in stale_ids and s.state == StructState.RESERVED]
    open_legs: list[int] = []
    void_legs: list[tuple[int, str]] = []
    for lg in current:
        if lg.state != LegState.ENTERING:
            continue
        orders_ = lg.leg_orders()
        if any(o.role != Role.EXTERNAL and not o.is_terminal for o in orders_):
            continue                                    # the order state machine finishes it
        entries = [o for o in orders_ if o.role == Role.ENTRY]
        if leg_fill_totals(orders_)[0] > 0:
            open_legs.append(lg.leg_id)
        elif not entries:
            void_legs.append((lg.leg_id, "RESTART"))
        elif entries[-1].state == OrderState.REJECTED:
            void_legs.append((lg.leg_id, f"REJECTED:{entries[-1].status_msg or ''}"))
        else:
            void_legs.append((lg.leg_id, "ENTRY_NO_FILL"))

    # 7. after EOD_FLATTEN every open leg exits; EXITING legs keep their persisted reason
    flatten = []
    if now_wall >= eod_flatten_at:
        flatten = [lg.leg_id for lg in current if lg.state == LegState.OPEN or lg.leg_id in open_legs]

    # 7. leftovers of an earlier session: never traded again
    stale: list[StaleStructure] = []
    for sid in sorted(stale_ids):
        s = live_structs[sid]
        s_legs = [lg for lg in legs if lg.structure_id == sid]
        working = tuple(o.order_pk for lg in s_legs for o in lg.leg_orders()
                        if o.role != Role.EXTERNAL and not o.is_terminal)
        held = sum(max(0, e - x) for e, x in (leg_fill_totals(lg.leg_orders()) for lg in s_legs))
        qty = sum(net_qty(positions, sym) for sym in {lg.inst.symbol for lg in s_legs}) if _ok(positions) else None
        action = stale_action(acct, s_legs)
        if action == "CLOSE_UNRESOLVED":
            text = (f"{acct} structure {sid} of {s.session} still open at startup (held {held}, working "
                    f"{len(working)}): closed as UNRESOLVED; broker net now {qty}")
            halts.append(HaltRequest("MISMATCH", scope, HaltClears.ACK, text))
            alerts.append(Alert("CRITICAL", "MISMATCH", text, f"recovery:{acct}:stale:{sid}"))
            events.append(LogEvent("MISMATCH", "CRITICAL", {"account": str(acct), "structure_id": sid,
                                                             "session": s.session.isoformat(), "broker_qty": qty}))
        stale.append(StaleStructure(sid, s.session, action, qty, working))

    return RecoveryPlan(acct, tuple(apply), tuple(lookups), tuple(vanished), tuple(imports), tuple(open_legs),
                        tuple(void_legs), tuple(void_structures), tuple(flatten), tuple(stale), tuple(halts),
                        tuple(alerts), tuple(cmds), tuple(events))


# ---------------------------------------------------------------------------------------------------------------
# §9.8 EOD truth check
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TradebookOrder:
    """One broker order's executed trades, summed into the fields charges.OrderFills takes."""

    broker_order_id: str
    side: Side
    filled_qty: int
    value: float
    external_kind: str | None = None
    part: tuple[int, int] | None = None   # one book's [start, end) share of an order several legs shared


@dataclass(frozen=True, slots=True)
class BookTruth:
    book: str
    ledger_net: float
    broker_net: float
    diff: float                           # ledger − broker


@dataclass(frozen=True, slots=True)
class TruthCheck:
    account: Account
    ok: bool
    ledger_net: float
    broker_net: float | None              # None: the tradebook was unavailable
    tolerance: float
    books: tuple[BookTruth, ...]
    unattributed: tuple[str, ...]         # tradebook order ids the ledger does not know (foreign or lost)
    g1_mismatch_books: tuple[str, ...]    # PAPER/SHADOW books whose G1 must count a mismatch
    alerts: tuple[Alert, ...] = ()
    events: tuple[LogEvent, ...] = ()
    carried: tuple[tuple[date, TruthCheck], ...] = ()   # (session, its check) for earlier sessions' leftovers


def tradebook_orders(trades: Iterable[BrokerTrade], include: Mapping[str, str | None]) -> tuple[TradebookOrder, ...]:
    """Sum the tradebook per broker order, for the orders in include (broker_order_id -> external_kind)."""
    agg: dict[str, tuple[Side, list[int], list[float]]] = {}
    for t in trades:
        if t.broker_order_id not in include:
            continue
        side, qtys, values = agg.setdefault(t.broker_order_id, (Side(t.side), [], []))
        if Side(t.side) is not side:
            raise ValueError(f"order {t.broker_order_id} has trades on both sides")
        qtys.append(int(t.qty))
        values.append(t.px * t.qty)
    return tuple(TradebookOrder(oid, side, sum(q), math.fsum(v), include[oid])
                 for oid, (side, q, v) in sorted(agg.items()))


def _default_net_fn(session: date) -> Callable[[Sequence[TradebookOrder]], float]:
    rates = charges.rates_for(session)

    def net(orders: Sequence[TradebookOrder]) -> float:
        fills = []
        for o in orders:
            f = charges.OrderFills(o.side, o.filled_qty, o.value, o.external_kind)
            fills.append(f if o.part is None else charges.OrderPart(f, *o.part))
        return charges.net_pnl(fills, rates).net

    return net


def eod_truth_check(account: Account, session: date, ledger_by_book: Mapping[str, float],
                    order_books: Mapping[str, tuple[str, str | None]], trades: Sequence[BrokerTrade] | None,
                    net_fn: Callable[[Sequence[TradebookOrder]], float] | None = None,
                    parts: Mapping[str, Sequence[tuple[str, int]]] | None = None,
                    carried: Sequence[CarriedTruth] = ()) -> TruthCheck:
    """§9.8 at 15:33: today's ledger (Σ trades.net per book) against the broker tradebook recomputed with the same
    charges function. order_books: broker_order_id -> (book, external_kind) for every order the DB attributes to
    a book (bot orders and EXTERNAL rows). parts: broker_order_id -> [(book, qty)] in leg order for one broker
    order that closed the legs of several books (ledger.truth_attribution builds both from titan2.db): each book
    is compared on its share, value and charges allocated as the ledger booked them (charges.OrderPart). LIVE
    passes within ₹1 in total; PAPER/SHADOW must match to the paisa per book, and any trade the ledger cannot
    attribute is a mismatch for every book of the account.

    carried (ledger.truth_inputs, review round 2): earlier sessions' structures with a broker order executed today
    (a leftover settled at today's start, its trade booked to its own session). Their tradebook rows leave today's
    check and each such session's structures are checked against their own trades instead (today's tradebook rows
    of their orders plus their earlier days' fills, with that session's charges). Their rows join the event's
    `books` with a `session`, so a difference counts against the book that holds it, as one of today's does.
    """
    acct = Account(account)
    carried = tuple(carried)
    if carried:
        dropped = frozenset().union(*(c.today_ids for c in carried))
        subs = tuple((c.session, eod_truth_check(
            acct, c.session, c.ledger_by_book, c.order_books,
            None if trades is None else [t for t in trades if t.broker_order_id in c.order_books] + list(c.earlier),
            net_fn, c.parts)) for c in carried)
        today = eod_truth_check(acct, session, ledger_by_book, order_books,
                                None if trades is None else [t for t in trades if t.broker_order_id not in dropped],
                                net_fn, parts)
        return _with_carried(today, subs, sorted(dropped))
    fn = net_fn if net_fn is not None else _default_net_fn(session)
    parts = parts or {}
    names = sorted(set(ledger_by_book) | {book for book, _ in order_books.values()}
                   | {book for split in parts.values() for book, _ in split})
    ledger_total = round(math.fsum(float(v) for v in ledger_by_book.values()), 2)
    tol = EOD_TRUTH_TOLERANCE_LIVE_RS if acct is Account.LIVE else 0.0
    if trades is None:
        books: tuple[BookTruth, ...] = ()
        unattributed: tuple[str, ...] = ()
        broker_total = None
        ok = False
        bad = tuple(names) if acct is not Account.LIVE else ()
        detail = "tradebook unavailable"
    else:
        include = {oid: kind for oid, (_, kind) in order_books.items()}
        grouped: dict[str, list[TradebookOrder]] = {}
        for o in tradebook_orders(trades, include):
            split = parts.get(o.broker_order_id)
            if split and sum(q for _, q in split) == o.filled_qty:
                start = 0
                for book, q in split:
                    grouped.setdefault(book, []).append(replace(o, part=(start, start + q)))
                    start += q
            else:          # not shared, or the shares do not add up to the order: the difference shows as a mismatch
                grouped.setdefault(order_books[o.broker_order_id][0], []).append(o)
        unattributed = tuple(sorted({t.broker_order_id for t in trades if t.broker_order_id not in order_books}))
        rows = []
        for book in names:
            b = round(fn(grouped[book]), 2) if grouped.get(book) else 0.0
            led = round(float(ledger_by_book.get(book, 0.0)), 2)
            rows.append(BookTruth(book, led, b, round(led - b, 2)))
        books = tuple(rows)
        broker_total = round(math.fsum(r.broker_net for r in rows), 2)
        if acct is Account.LIVE:
            ok = abs(ledger_total - broker_total) <= tol + 1e-9
            bad = ()
        else:
            bad = tuple(names) if unattributed else tuple(r.book for r in rows if abs(r.diff) > PAISE)
            ok = not bad
        detail = f"ledger {ledger_total:.2f} vs broker {broker_total:.2f}"
        if unattributed:
            detail += f"; {len(unattributed)} unattributed order(s)"
    payload = {"account": str(acct), "session": session.isoformat(), "ok": ok, "ledger_net": ledger_total,
               "broker_net": broker_total, "tolerance": tol, "unattributed": list(unattributed),
               "books": [{"book": r.book, "ledger": r.ledger_net, "broker": r.broker_net, "diff": r.diff}
                         for r in books]}
    events = (LogEvent("EOD_TRUTH_CHECK", "INFO" if ok else "CRITICAL", payload),)
    alerts: tuple[Alert, ...] = ()
    if not ok:
        alerts = (Alert("CRITICAL", "EOD_TRUTH_MISMATCH", f"{acct} {session}: {detail}",
                        f"eod_truth:{acct}:{session.isoformat()}"),)
    return TruthCheck(acct, ok, ledger_total, broker_total, tol, books, unattributed, bad, alerts, events)


def _with_carried(today: TruthCheck, subs: Sequence[tuple[date, TruthCheck]], dropped: Sequence[str]) -> TruthCheck:
    """Today's check with the carried sessions' checks folded in: one result and one EOD_TRUTH_CHECK event. Today's
    totals stay today's; the carried rows are listed under `carried` and join `books` with their `session`."""
    ok = today.ok and all(c.ok for _, c in subs)
    bad = tuple(dict.fromkeys([*today.g1_mismatch_books, *(b for _, c in subs for b in c.g1_mismatch_books)]))
    payload = dict(today.events[0].payload)
    payload["ok"] = ok
    payload["books"] = list(payload["books"]) + [
        {"book": r.book, "ledger": r.ledger_net, "broker": r.broker_net, "diff": r.diff, "session": s.isoformat()}
        for s, c in subs for r in c.books]
    payload["carried"] = [{"session": s.isoformat(), "ok": c.ok, "ledger_net": c.ledger_net,
                           "broker_net": c.broker_net} for s, c in subs]
    payload["carried_orders"] = list(dropped)
    alerts = today.alerts
    if not ok and not alerts:
        detail = "; ".join(f"ledger {c.ledger_net:.2f} vs broker {c.broker_net}" for _, c in subs if not c.ok)
        when = ", ".join(sorted({s.isoformat() for s, c in subs if not c.ok}))
        alerts = (Alert("CRITICAL", "EOD_TRUTH_MISMATCH", f"{today.account} {payload['session']}: a leftover of "
                        f"{when} settled today: {detail}", f"eod_truth:{today.account}:{payload['session']}"),)
    return replace(today, ok=ok, g1_mismatch_books=bad, alerts=alerts,
                   events=(LogEvent("EOD_TRUTH_CHECK", "INFO" if ok else "CRITICAL", payload),), carried=tuple(subs))
