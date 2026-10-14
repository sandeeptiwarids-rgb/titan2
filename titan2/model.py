"""Shared types for every titan2 unit (spec §4.1; field names are binding).

Enums, frozen market/broker values, the mutable core-owned Order/Leg/Structure, the depth walk, tick math and
order tags. Imports only the stdlib, so strategies, core, broker and dash code can all depend on it.

Money is rupees as float. Wall times are IST-aware datetimes; *_mono values come from time.monotonic() and are
never persisted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping


# ---------------------------------------------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------------------------------------------
class _StrEnum(str, Enum):
    """str-valued Enum whose str() and format() are the plain value on every Python version.

    Python 3.11 changed format()/f-strings of (str, Enum) members to "Account.LIVE"; halt scopes such as
    f"ACCOUNT:{account}" and SQL/JSON text must read "ACCOUNT:LIVE" on 3.10 and 3.12 alike.
    """

    def __str__(self) -> str:
        return self.value

    def __format__(self, spec: str) -> str:
        return format(self.value, spec)


class Account(_StrEnum):
    LIVE = "LIVE"
    PAPER = "PAPER"
    SHADOW = "SHADOW"


class Policy(_StrEnum):
    FULL = "FULL"
    QUALITY = "QUALITY"


class Mode(_StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Direction(_StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class StructureType(_StrEnum):  # phase 2 extends this (§14)
    LONG_CE = "LONG_CE"
    LONG_PE = "LONG_PE"


class StrikePref(_StrEnum):
    ATM = "ATM"
    ITM1 = "ITM1"


class Role(_StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    COVER = "COVER"
    SELFTEST = "SELFTEST"
    EXTERNAL = "EXTERNAL"


class Side(_StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(_StrEnum):
    NEW = "NEW"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    WORKING = "WORKING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    NOT_PLACED = "NOT_PLACED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_ORDER


class LegState(_StrEnum):
    PLANNED = "PLANNED"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    VERIFY_FLAT = "VERIFY_FLAT"
    CLOSED = "CLOSED"
    CLOSED_EXTERNAL = "CLOSED_EXTERNAL"
    VOID = "VOID"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_LEG


class StructState(_StrEnum):
    RESERVED = "RESERVED"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    VERIFY_FLAT = "VERIFY_FLAT"
    CLOSED = "CLOSED"
    CLOSED_EXTERNAL = "CLOSED_EXTERNAL"
    VOID = "VOID"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STRUCT


class Urgency(_StrEnum):
    NORMAL = "NORMAL"
    PROTECTIVE = "PROTECTIVE"


class PnlStatus(_StrEnum):
    FINAL = "FINAL"
    ESTIMATED = "ESTIMATED"
    UNRESOLVED = "UNRESOLVED"


class HaltClears(_StrEnum):
    NEXT_SESSION = "NEXT_SESSION"
    ACK = "ACK"
    AUTO = "AUTO"
    RESUME = "RESUME"


TERMINAL_ORDER = frozenset({OrderState.COMPLETE, OrderState.CANCELLED, OrderState.REJECTED, OrderState.NOT_PLACED})
TERMINAL_LEG = frozenset({LegState.CLOSED, LegState.CLOSED_EXTERNAL, LegState.VOID})
TERMINAL_STRUCT = frozenset({StructState.CLOSED, StructState.CLOSED_EXTERNAL, StructState.VOID})

EXIT_REASONS = (
    "HARD_SL", "LOCK_STOP", "TAKE_PROFIT", "TIME_STOP", "EOD_FLATTEN", "STALE_QUOTE_PROTECTIVE", "NO_BID_PROTECTIVE",
    "KILL_SWITCH", "OPERATOR_FLATTEN", "DAILY_STOP", "ANOMALY_EXIT", "RECOVERY_FLATTEN", "SELFTEST_FILLED",
    "CLOSED_EXTERNAL", "BROKER_SQUAREOFF",
)

# I7 (phase 1): the only side each bot role may trade. EXTERNAL orders are not placed by the bot.
ROLE_SIDE: Mapping[Role, Side] = MappingProxyType(
    {Role.ENTRY: Side.BUY, Role.EXIT: Side.SELL, Role.COVER: Side.BUY, Role.SELFTEST: Side.BUY}
)

# Every bot order is LIMIT + IOC + MIS, variety regular, on NFO (§5.1). OrderRequest implies these values.
NFO = "NFO"
LIMIT = "LIMIT"
IOC = "IOC"
MIS = "MIS"
REGULAR = "regular"

EXPIRY_RULE_NEAREST = "NEAREST_ELIGIBLE"  # the phase-1 Intent.expiry_rule (§11.4)


class InvariantViolation(Exception):
    """A broken runtime invariant (I1-I16 and their DB mirrors).

    scope is "GLOBAL", "ACCOUNT:<A>" or "LEG:<leg_id>". Global/account scope -> HALT(INVARIANT, ACK) + CRITICAL;
    leg scope -> that leg goes to EXITING(ANOMALY_EXIT). Exits always continue (§3.6).
    """

    def __init__(self, code: str, detail: str = "", scope: str = "GLOBAL") -> None:
        super().__init__(f"{code} [{scope}] {detail}".rstrip())
        self.code = code
        self.detail = detail
        self.scope = scope

    @property
    def leg_id(self) -> int | None:
        if self.scope.startswith("LEG:"):
            return int(self.scope[4:])
        return None


# ---------------------------------------------------------------------------------------------------------------
# Market values (frozen)
# ---------------------------------------------------------------------------------------------------------------
PRICE_EPS = 1e-9  # float slack when comparing a rupee price with a limit; real prices are tick multiples
# Quote.age(): a receipt up to this long AFTER the caller's now_mono is a tick that arrived while the event was
# being handled (age 0); further ahead it is a clock-domain bug and reads as infinitely stale (fail closed).
RECEIPT_SKEW_S = 2.0


def age_since_receipt(recv_mono: float, now_mono: float) -> float:
    """Seconds from a receipt (monotonic) to now_mono, by Quote.age's rule for any receipt time: up to
    RECEIPT_SKEW_S after now_mono it arrived while the event was handled (age 0); further ahead, or NaN, it reads as
    infinitely stale. The entry gates use it for the feed's last underlying tick (G06) and the underlying's tick
    (G16), which the WS thread keeps writing during the event (review round 3)."""
    a = now_mono - recv_mono
    if a >= 0.0:
        return a if math.isfinite(a) else math.inf
    return 0.0 if a >= -RECEIPT_SKEW_S else math.inf


@dataclass(frozen=True, slots=True)
class Level:
    price: float
    qty: int
    orders: int = 0


def _positive(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _clean_book(levels: Iterable[Level] | None, best_is_high: bool) -> tuple[Level, ...]:
    """Valid levels only (finite price > 0 and qty > 0), best first. Accepts Level or (price, qty[, orders])."""
    out: list[Level] = []
    for lv in levels or ():
        if isinstance(lv, (tuple, list)):
            lv = Level(*lv)
        elif not isinstance(lv, Level):
            raise TypeError(f"depth level must be a Level or (price, qty[, orders]), got {type(lv).__name__}")
        if _positive(lv.price) and _positive(lv.qty):
            out.append(lv)
    out.sort(key=lambda lv: lv.price, reverse=best_is_high)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Quote:
    """One depth snapshot. bids/asks are normalized on construction: valid levels only, best first."""

    symbol: str                      # "NFO:RELIANCE26OCT2900CE" | "NSE:RELIANCE" | "NSE:NIFTY 50"
    token: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    ltp: float | None                # logged only; never used for a decision
    volume: int | None
    last_trade_wall: datetime | None
    exch_wall: datetime | None
    recv_mono: float                 # receipt time of THIS snapshot (WS packet or REST response)
    recv_wall: datetime
    source: str                      # "WS" | "REST" | "SIM"

    def __post_init__(self) -> None:
        object.__setattr__(self, "bids", _clean_book(self.bids, best_is_high=True))
        object.__setattr__(self, "asks", _clean_book(self.asks, best_is_high=False))

    @property
    def bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def mid(self) -> float | None:
        """None unless both sides exist and ask >= bid."""
        b, a = self.bid, self.ask
        if b is None or a is None or a < b:
            return None
        return (a + b) / 2.0

    def spread_pct(self) -> float | None:
        m = self.mid()
        if m is None:
            return None
        return (self.ask - self.bid) / m * 100.0

    def age(self, now_mono: float) -> float:
        """Seconds since receipt, as of now_mono. A receipt up to RECEIPT_SKEW_S after now_mono is a tick received
        while the event was handled (age 0); a receipt further in the future (a clock-domain bug) and NaN read as
        infinitely stale."""
        return age_since_receipt(self.recv_mono, now_mono)


@dataclass(frozen=True, slots=True)
class WalkResult:
    avg_px: float | None             # None when nothing is fillable
    fillable_qty: int
    worst_px: float | None           # deepest level touched

    @property
    def fillable(self) -> int:       # spec §5.7/§7.3 spell it w.fillable
        return self.fillable_qty


# Ported from agentic_trader/zerodha_tools.py:4632-4657 @b4694f3 — changes: pure function over Level tuples;
#   adds a price limit and side; returns fillable_qty instead of the 95%-coverage rule
# Ported from agentic_trader/dashboard.py:4255-4269 @b4694f3 — changes: same walk; never falls back to LTP
def walk(levels: Iterable[Level], qty: int, limit: float | None, side: Side) -> WalkResult:
    """Cross the book for qty: BUY walks asks taking price <= limit, SELL walks bids taking price >= limit.

    limit None means unbounded. Levels are taken best first and the walk stops at the first level beyond the
    limit (a trade-through fills nothing more). A NaN limit fills nothing (fails closed).
    """
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise TypeError(f"qty must be int, got {type(qty).__name__}")
    side = Side(side)
    empty = WalkResult(None, 0, None)
    if qty <= 0 or (limit is not None and math.isnan(limit)):
        return empty
    buy = side is Side.BUY
    taken = 0
    value = 0.0
    worst: float | None = None
    for lv in _clean_book(levels, best_is_high=not buy):
        if limit is not None and (lv.price > limit + PRICE_EPS if buy else lv.price < limit - PRICE_EPS):
            break
        take = min(qty - taken, lv.qty)
        value += take * lv.price
        taken += take
        worst = lv.price
        if taken >= qty:
            break
    if taken == 0:
        return empty
    return WalkResult(value / taken, taken, worst)


@dataclass(frozen=True, slots=True)
class UnderlyingTick:
    symbol: str
    token: int
    ltp: float
    cum_volume: int
    day_open: float | None
    prev_close: float | None
    exch_wall: datetime | None
    recv_mono: float
    recv_wall: datetime


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    valid: bool
    source: str                      # "LIVE" | "HIST"


@dataclass(frozen=True, slots=True)
class Underlying:
    symbol: str                      # "NSE:RELIANCE" | "NSE:NIFTY 50"
    token: int
    name: str                        # the NFO name, e.g. "RELIANCE" | "NIFTY"
    kind: str                        # "STOCK" | "INDEX"


@dataclass(frozen=True, slots=True)
class Instrument:
    """One option contract; every field comes from that contract's own instruments-dump row."""

    symbol: str                      # "NFO:<tradingsymbol>"
    token: int
    tradingsymbol: str
    exchange: str
    name: str
    underlying: str                  # underlying symbol, e.g. "NSE:RELIANCE"
    opt_type: str                    # "CE" | "PE"
    strike: float
    expiry: date
    lot_size: int
    tick: float


@dataclass(frozen=True, slots=True)
class Recheck:
    lo: float                        # entry requires lo <= underlying LTP <= hi; +-inf allowed
    hi: float

    def contains(self, px: float | None) -> bool:
        return px is not None and not math.isnan(px) and self.lo <= px <= self.hi


@dataclass(frozen=True, slots=True)
class LegSpec:                       # phase 2 only
    opt_type: str
    strike_rule: str
    side: Side
    ratio: int = 1


@dataclass(frozen=True, slots=True)
class Intent:
    """A strategy's entry request. It has no quantity: size() alone sizes (§8.5)."""

    intent_id: str
    strategy: str
    strategy_version: str
    underlying: str
    direction: Direction
    structure: StructureType
    strike_pref: StrikePref
    expiry_rule: str                 # phase 1: EXPIRY_RULE_NEAREST
    trigger_px: float
    recheck: Recheck
    ttl_s: float                     # 0 < ttl_s <= 10
    created_mono: float
    created_wall: datetime
    reason: Mapping[str, float | str]  # logged only; never drives size or direction
    is_placebo: bool = False
    parent_intent_id: str | None = None
    legs: tuple[LegSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class ExitProfile:                   # per strategy (in PARAMS, versioned); bounds in §7.2
    hard_sl_pct: float = 15.0
    lock_arm_pct: float = 4.0
    lock_floor_pct: float = 3.0
    trail_retain: float = 0.70
    tp_pct: float = 15.0
    time_stop_min: float = 45.0
    time_stop_when_locked: bool = False
    stop_confirm_ms: int = 400
    stop_confirm_max_ms: int = 1500
    stop_confirm_bypass_pct: float = 3.0
    no_bid_exit_s: float = 10.0


@dataclass(frozen=True, slots=True)
class ExitParams:                    # frozen at the entry fill; JSON in structures.exit_params
    profile: ExitProfile
    entry_avg: float
    qty: int
    entry_charges: float
    rates_version: str
    k_exit: float
    be_px: float
    hard_sl_px: float
    arm_px: float
    floor_px: float
    tp_px: float
    entry_fill_wall: datetime
    time_stop_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionQuote:
    bid: float | None
    ask: float | None
    mid: float | None
    age_ms: int
    src: str
    ltp: float | None
    walk_px: float | None
    # The decision snapshot's exchange time (Quote.exch_wall), which the order request carries to the broker
    #   (OrderRequest.decision_exch_wall). RAM only: titan2.db does not keep it, so a reloaded order has None, and it
    #   takes no part in comparisons (a reloaded order equals the one in memory)
    exch_wall: datetime | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """What a broker is asked to do. Implied: NFO, LIMIT, IOC, MIS, variety regular (see order_request_problem)."""

    account: Account
    tag: str                         # == order_pk; make_tag format
    role: Role
    symbol: str
    tradingsymbol: str
    exchange: str
    token: int
    side: Side
    qty: int
    limit_px: float
    lot_size: int
    tick: float
    # The bot book (orders.create_order sets it). Never sent to Kite (broker/kite.place_payload); the paper exchange
    #   funds a SHADOW-account book from its own pool (review F-19)
    book: str | None = None
    # The exchange time of the quote the order was decided on (its DecisionQuote.exch_wall; orders.create_order sets
    #   it). Never sent to Kite either; the paper exchange never fills the order on a book stamped in an earlier
    #   second (paper model 2). None: the decision quote had no exchange stamp
    decision_exch_wall: datetime | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    order_pk: str
    seq_key: str                     # "cum:<cum_qty>" (source CUM) | "trade:<trade_id>" (source TRADE)
    source: str                      # "CUM" | "TRADE"
    qty: int
    px: float
    wall: datetime
    exch_wall: datetime | None


@dataclass(frozen=True, slots=True)
class Charges:
    brokerage: float
    stt: float
    txn: float
    sebi: float
    ipft: float
    gst: float
    stamp: float
    sqoff_fee: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.txn + self.sebi + self.ipft + self.gst + self.stamp
                     + self.sqoff_fee, 2)


@dataclass(frozen=True, slots=True)
class PnL:
    gross: float
    charges: Charges
    net: float
    n_executed_orders: int
    rates_version: str


@dataclass(frozen=True, slots=True)
class Halt:
    """scope: "GLOBAL" | "ACCOUNT:<A>" | "BOOK:<book>" | "STRATEGY:<s>" | "UNDERLYING:<sym>" | "SYMBOL:<sym>"."""

    halt_id: int
    scope: str
    kind: str
    reason: str
    operator: bool
    clears: HaltClears
    created_wall: datetime
    session: date


# ---------------------------------------------------------------------------------------------------------------
# Broker-normalized values (frozen)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BrokerOrder:
    account: Account
    broker_order_id: str
    tag: str | None
    symbol: str
    tradingsymbol: str
    exchange: str
    product: str
    side: Side
    order_type: str
    validity: str
    qty: int
    filled_qty: int
    avg_px: float | None
    status: str                      # normalized: "WORKING" | "COMPLETE" | "CANCELLED" | "REJECTED"
    raw_status: str
    status_msg: str | None
    order_wall: datetime | None
    exch_wall: datetime | None
    placed_by: str | None


@dataclass(frozen=True, slots=True)
class BrokerTrade:
    account: Account
    trade_id: str
    broker_order_id: str
    symbol: str
    side: Side
    qty: int
    px: float
    fill_wall: datetime | None
    exch_wall: datetime | None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    account: Account
    symbol: str
    tradingsymbol: str
    exchange: str
    product: str
    net_qty: int                     # signed: < 0 is a short
    buy_qty: int
    sell_qty: int
    buy_value: float
    sell_value: float


@dataclass(frozen=True, slots=True)
class Margins:
    account: Account
    available_rs: float
    raw: Mapping


@dataclass(frozen=True, slots=True)
class FeedHealth:
    connected: bool
    connected_since_mono: float | None
    last_underlying_tick_mono: float | None
    reconnects: int
    degraded: bool


@dataclass(frozen=True, slots=True)
class Refusal:
    gate: str
    detail: str


@dataclass(frozen=True, slots=True)
class Sized:
    qty: int
    lots: int
    binding: str
    per_lot_risk_rs: float


# ---------------------------------------------------------------------------------------------------------------
# Core-owned mutable state (only the core thread mutates these)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Order:
    order_pk: str                    # bot orders: the tag; EXTERNAL orders: external_order_pk(broker_order_id)
    leg_id: int
    structure_id: int
    account: Account
    book: str
    role: Role
    side: Side
    symbol: str
    qty: int
    limit_px: float | None
    attempt: int
    band_idx: int | None
    state: OrderState = OrderState.NEW
    broker_order_id: str | None = None
    broker_status: str | None = None
    status_msg: str | None = None
    external_kind: str | None = None  # role EXTERNAL: "MANUAL" | "AUTO_SQUAREOFF"
    filled_qty: int = 0
    avg_fill_px: float | None = None
    decision: DecisionQuote | None = None
    created_wall: datetime | None = None
    submitted_mono: float | None = None
    terminal_wall: datetime | None = None
    lookups: int = 0
    stuck: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_ORDER


def leg_fill_totals(orders: Iterable[Order]) -> tuple[int, int]:
    """(entry_filled, exit_filled) of one phase-1 LONG leg, from broker-confirmed filled_qty only.

    Entry = ENTRY and SELFTEST orders (the bot's opening BUYs; a filled self-test must be sold too).
    Exit = EXIT orders plus EXTERNAL SELLs (closes made outside the bot).
    COVER BUYs count in neither: a cover repairs a broker-side short (§9.3) and must never create quantity the
    exit loop could sell again.
    """
    entry = exit_ = 0
    for o in orders:  # == (not `is`) so plain-string roles/sides loaded from rows count the same
        if o.role == Role.ENTRY or o.role == Role.SELFTEST:
            entry += o.filled_qty
        elif o.role == Role.EXIT or (o.role == Role.EXTERNAL and o.side == Side.SELL):
            exit_ += o.filled_qty
    return entry, exit_


class _OrderIndexSlot:
    """One hidden slot for Leg: the core's live order index. Not a dataclass field, so never stored or compared."""

    __slots__ = ("_order_index",)


@dataclass(slots=True)
class Leg(_OrderIndexSlot):
    """A position leg. Its quantities are derived from its orders' fills and are never stored.

    The core binds each Leg to its order map (order_pk -> Order) with bind_orders(); reading entry_filled,
    exit_filled or open_qty on an unbound Leg raises (it never guesses 0). dataclasses.replace() returns an
    unbound copy.
    """

    leg_id: int
    structure_id: int
    leg_no: int
    account: Account
    book: str
    session: date
    seq_no: int
    inst: Instrument
    side: str = "LONG"               # phase 1
    state: LegState = LegState.PLANNED
    orders: list[str] = field(default_factory=list)  # order_pks in creation order
    stop_px: float | None = None
    lock_armed: bool = False
    peak_rb: float | None = None
    peak_wall: datetime | None = None
    trough_rb: float | None = None
    trough_wall: datetime | None = None
    breach_first_mono: float | None = None
    breach_quote_mono: float | None = None
    no_bid_since_mono: float | None = None
    entry_fill_wall: datetime | None = None
    last_fill_mono: float | None = None
    last_fill_wall: datetime | None = None
    exit_reason: str | None = None
    exit_urgency: Urgency | None = None
    exit_band_idx: int = 0
    exiting_since_mono: float | None = None
    broker_residual_qty: int = 0

    def bind_orders(self, index: Mapping[str, Order]) -> Leg:
        self._order_index = index
        return self

    def leg_orders(self) -> list[Order]:
        index = getattr(self, "_order_index", None)
        if index is None:
            raise RuntimeError(f"leg {self.leg_id}: orders not bound; call bind_orders() first")
        out = []
        for pk in self.orders:
            o = index.get(pk)
            if o is None or o.leg_id != self.leg_id:
                raise InvariantViolation("I3", f"order {pk} missing from the index or not on leg {self.leg_id}",
                                         scope=f"LEG:{self.leg_id}")
            out.append(o)
        return out

    @property
    def entry_filled(self) -> int:
        return leg_fill_totals(self.leg_orders())[0]

    @property
    def exit_filled(self) -> int:
        return leg_fill_totals(self.leg_orders())[1]

    @property
    def open_qty(self) -> int:
        """entry_filled - exit_filled. Negative means oversold: reported as is so I3 can catch it."""
        entry, exit_ = leg_fill_totals(self.leg_orders())
        return entry - exit_

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_LEG


@dataclass(slots=True)
class Structure:
    structure_id: int
    session: date
    book: str
    account: Account
    policy: Policy
    strategy: str
    strategy_version: str
    intent: Intent
    type: StructureType
    underlying: str
    state: StructState
    legs: list[int]                  # leg ids
    is_placebo: bool
    parent_structure_id: int | None
    blocked_by: str | None
    decision: Mapping | None         # decision quotes, plan, placebo plan (JSON)
    placebo_status: str | None       # "OPENED" | "SKIPPED:<gate>" | None
    exit_params: ExitParams | None
    risk_basis_rs: float | None
    exit_reason: str | None
    void_reason: str | None
    reserved_wall: datetime
    opened_wall: datetime | None = None
    closed_wall: datetime | None = None
    pnl_status: PnlStatus | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STRUCT


# ---------------------------------------------------------------------------------------------------------------
# Tick math (§4.1). Prices are rounded to 2 dp, so ticks must be >= 0.01 (NSE options: 0.05).
# ---------------------------------------------------------------------------------------------------------------
def _check_tick_args(x: float, t: float) -> None:
    if not (_positive(t) and isinstance(x, (int, float)) and math.isfinite(x)):
        raise ValueError(f"bad tick math input x={x!r} tick={t!r}")


# Ported from agentic_trader/zerodha_tools.py:49-57 @b4694f3 — changes: round-to-nearest 0.05 replaced by
#   floor/ceil to the contract's own tick
def floor_tick(x: float, t: float) -> float:
    _check_tick_args(x, t)
    return round(math.floor(x / t + 1e-9) * t, 2)


def ceil_tick(x: float, t: float) -> float:
    _check_tick_args(x, t)
    return round(math.ceil(x / t - 1e-9) * t, 2)


def is_tick_multiple(x: float, t: float) -> bool:
    """False for any invalid input (a validation predicate fails closed)."""
    if not (_positive(t) and isinstance(x, (int, float)) and math.isfinite(x)):
        return False
    q = x / t
    return abs(q - round(q)) < 1e-6


# ---------------------------------------------------------------------------------------------------------------
# Order tags (§4.1): "T2" + role + account + yymmdd + b36(seq_no, 3) + b36(attempt, 2) = 15 chars of [0-9A-Z]
# ---------------------------------------------------------------------------------------------------------------
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TAG_LEN = 15
SEQ_MAX = 36 ** 3 - 1                # 46655
ATTEMPT_MAX = 36 ** 2 - 1            # 1295
ROLE_LETTER: Mapping[Role, str] = MappingProxyType(
    {Role.ENTRY: "E", Role.EXIT: "X", Role.COVER: "C", Role.SELFTEST: "T"}
)
ACCOUNT_LETTER: Mapping[Account, str] = MappingProxyType({Account.LIVE: "L", Account.PAPER: "P", Account.SHADOW: "S"})
_LETTER_ROLE = {v: k for k, v in ROLE_LETTER.items()}
_LETTER_ACCOUNT = {v: k for k, v in ACCOUNT_LETTER.items()}
_TAG_RE = re.compile(r"T2([EXCT])([LPS])([0-9]{6})([0-9A-Z]{3})([0-9A-Z]{2})")


@dataclass(frozen=True, slots=True)
class ParsedTag:
    role: Role
    account: Account
    session: date
    seq_no: int
    attempt: int

    @property
    def tag(self) -> str:
        return make_tag(self.role, self.account, self.session, self.seq_no, self.attempt)


def _plain_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def b36(n: int, width: int) -> str:
    """n in upper-case base 36, zero-padded to exactly width characters."""
    if not _plain_int(n) or not 0 <= n < 36 ** width:
        raise ValueError(f"{n!r} does not fit in {width} base-36 digits")
    digits = []
    for _ in range(width):
        n, r = divmod(n, 36)
        digits.append(_B36[r])
    return "".join(reversed(digits))


def make_tag(role: Role, account: Account, session: date, seq_no: int, attempt: int) -> str:
    """The order tag (== order_pk). seq_no counts per (account, session, leg); every retry is a new attempt."""
    role, account = Role(role), Account(account)
    if role not in ROLE_LETTER:
        raise ValueError(f"role {role} has no bot tag")
    if not isinstance(session, date) or not 2000 <= session.year <= 2099:
        raise ValueError(f"bad session {session!r}")
    if not _plain_int(seq_no) or not 1 <= seq_no <= SEQ_MAX:
        raise ValueError(f"seq_no {seq_no!r} outside 1..{SEQ_MAX}")
    if not _plain_int(attempt) or not 1 <= attempt <= ATTEMPT_MAX:
        raise ValueError(f"attempt {attempt!r} outside 1..{ATTEMPT_MAX}")
    return (f"T2{ROLE_LETTER[role]}{ACCOUNT_LETTER[account]}"
            f"{session.year % 100:02d}{session.month:02d}{session.day:02d}{b36(seq_no, 3)}{b36(attempt, 2)}")


def parse_tag(tag: object) -> ParsedTag | None:
    """ParsedTag for exactly the make_tag format; None for anything else (foreign, manual, SQOFF, malformed)."""
    if not isinstance(tag, str) or len(tag) != TAG_LEN:
        return None
    m = _TAG_RE.fullmatch(tag)
    if m is None:
        return None
    ymd = m.group(3)
    try:
        session = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    seq_no, attempt = int(m.group(4), 36), int(m.group(5), 36)
    if seq_no < 1 or attempt < 1:
        return None
    return ParsedTag(_LETTER_ROLE[m.group(1)], _LETTER_ACCOUNT[m.group(2)], session, seq_no, attempt)


def is_bot_tag(tag: object, account: Account) -> bool:
    p = parse_tag(tag)
    return p is not None and p.account is Account(account)


def external_order_pk(broker_order_id: str) -> str:
    """order_pk for a closing order the bot did not place (DDL: 'EX' || broker_order_id)."""
    if not isinstance(broker_order_id, str) or not broker_order_id:
        raise ValueError("external order needs a broker_order_id")
    return "EX" + broker_order_id


def order_request_problem(req: OrderRequest) -> str | None:
    """The first reason this request breaks the fixed order rules (I7, I12 lot multiple, I13 tick), else None.

    Shared by every order path (KiteBroker, PaperExchange, invariants) so the rules exist once.
    """
    try:
        role, account, side = Role(req.role), Account(req.account), Side(req.side)
    except ValueError as exc:
        return f"bad enum: {exc}"
    if role not in ROLE_SIDE:
        return f"role {role} cannot be placed by the bot"
    if side is not ROLE_SIDE[role]:
        return f"role {role} must be {ROLE_SIDE[role]}, got {side}"
    p = parse_tag(req.tag)
    if p is None or p.role is not role or p.account is not account:
        return f"tag {req.tag!r} does not match role {role} / account {account}"
    if req.exchange != NFO:
        return f"exchange {req.exchange!r} is not {NFO}"
    if not (_plain_int(req.lot_size) and req.lot_size > 0):
        return f"bad lot_size {req.lot_size!r}"
    if not (_plain_int(req.qty) and req.qty > 0 and req.qty % req.lot_size == 0):
        return f"qty {req.qty!r} is not a positive multiple of lot {req.lot_size}"
    if not _positive(req.tick):
        return f"bad tick {req.tick!r}"
    if not (_positive(req.limit_px) and is_tick_multiple(req.limit_px, req.tick)):
        return f"limit {req.limit_px!r} is not a positive multiple of tick {req.tick}"
    return None
