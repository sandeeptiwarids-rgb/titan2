"""Events into core_q and Commands returned by Engine.handle (spec §4.2).

Plain immutable messages with no logic: adapters normalize what they put in, the engine validates what it reads.
Routing: LIVE commands go to lane-live-orders (orders) or lane-reads (reads); PAPER and SHADOW commands go to
paper-sim, which answers with the same events (source "SIM"). The engine never branches on the broker type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum
from typing import Mapping

from titan2.model import (
    LIMIT, MIS, REGULAR, Account, Bar, BrokerOrder, BrokerPosition, BrokerTrade, Intent, Margins, OrderRequest, Side,
)

PLACE_OUTCOMES = ("OK", "REJECTED", "AMBIGUOUS", "FATAL")
CANCEL_OUTCOMES = ("OK", "BENIGN", "REJECTED", "AMBIGUOUS", "FATAL")
ORDER_STATUSES = ("WORKING", "COMPLETE", "CANCELLED", "REJECTED")      # OrderSnapshot.status, normalized
SNAPSHOT_SOURCES = ("WS", "POLL", "LOOKUP", "SIM", "RECOVERY")
# TOKEN: the §9.7 / §8.7 token re-check on lane-reads while HALT(TOKEN) is active: profile() with the token in the
# session file; ok=True means that token works. Without it the engine has no way to ask for profile().
PREP_KINDS = ("INSTRUMENTS", "BAN_LIST", "PREV_BARS", "TOKEN")
ALERT_SEVERITIES = ("INFO", "WARN", "CRITICAL")
FEED_MODES = ("quote", "full")


class Priority(IntEnum):
    """lane-reads priority; the lowest number is served first (§3.2)."""

    EXIT = 0          # exit quotes; positions/orders for the exit cap and verify-flat
    ENTRY_LIVE = 1    # any decision request that includes a LIVE book
    RECON = 2
    PAPER = 3         # paper decision quotes; paper-fill depth
    SHADOW = 4
    BACKGROUND = 5    # historical, instruments, contract note, ban list


class Event:
    """Marker base for everything posted to core_q."""

    __slots__ = ()


class Command:
    """Marker base for everything Engine.handle returns (dispatched only after the DB commit)."""

    __slots__ = ()


# ---------------------------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Timer(Event):
    now_wall: datetime
    now_mono: float


@dataclass(frozen=True, slots=True)
class QuotesDirty(Event):
    """Held/pending option quotes changed; the core reads the symbols with feed.take_dirty()."""


@dataclass(frozen=True, slots=True)
class FeedState(Event):
    connected: bool
    reconnected: bool
    degraded: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IntentReceived(Event):
    intent: Intent


@dataclass(frozen=True, slots=True)
class BarsClosed(Event):
    bars: tuple[Bar, ...]


@dataclass(frozen=True, slots=True)
class QuoteResult(Event):
    request_id: str
    ok: bool
    symbols: tuple[str, ...]
    missing: tuple[str, ...] = ()
    error: str | None = None          # e.g. "TIMEOUT" after 3 s


@dataclass(frozen=True, slots=True)
class PlaceResult(Event):
    account: Account
    order_pk: str
    outcome: str                      # PLACE_OUTCOMES
    broker_order_id: str | None = None
    error_class: str | None = None
    message: str | None = None
    throttled: bool = False


@dataclass(frozen=True, slots=True)
class CancelResult(Event):
    account: Account
    order_pk: str
    outcome: str                      # CANCEL_OUTCOMES
    message: str | None = None


@dataclass(frozen=True, slots=True)
class OrderSnapshot(Event):
    account: Account
    broker_order_id: str | None
    tag: str | None
    status: str                       # ORDER_STATUSES
    filled_qty: int
    avg_px: float | None
    status_msg: str | None
    exch_wall: datetime | None
    source: str                       # SNAPSHOT_SOURCES
    recv_mono: float


@dataclass(frozen=True, slots=True)
class LookupResult(Event):
    account: Account
    order_pk: str
    ok: bool                          # False = API error, retry later
    found: BrokerOrder | None = None
    req_mono: float | None = None     # when its orders() read was asked (a miss counts from then; None: on arrival)


@dataclass(frozen=True, slots=True)
class TradesResult(Event):
    account: Account
    order_pk: str
    ok: bool
    trades: tuple[BrokerTrade, ...] = ()


@dataclass(frozen=True, slots=True)
class PositionsSnapshot(Event):
    account: Account
    ok: bool
    rows: tuple[BrokerPosition, ...]
    req_mono: float                   # when the request was sent (the post-fill rule, §9.2)
    recv_mono: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OrdersSnapshot(Event):
    account: Account
    ok: bool
    orders: tuple[BrokerOrder, ...]
    req_mono: float
    recv_mono: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TradebookSnapshot(Event):
    account: Account
    ok: bool
    trades: tuple[BrokerTrade, ...]
    req_mono: float


@dataclass(frozen=True, slots=True)
class MarginsResult(Event):
    account: Account
    ok: bool
    margins: Margins | None
    recv_mono: float


@dataclass(frozen=True, slots=True)
class SquareoffsResult(Event):
    """The answer to FetchSquareoffs: the broker's square-off orders of the earlier days asked for, and each symbol's
    net at the start of today."""

    account: Account
    request_id: str
    ok: bool
    orders: tuple[BrokerOrder, ...] = ()
    carried: tuple[tuple[str, int], ...] = ()       # (symbol, net at the start of today)


@dataclass(frozen=True, slots=True)
class HistoricalResult(Event):
    request_id: str
    symbol: str
    ok: bool
    bars: tuple[Bar, ...]


@dataclass(frozen=True, slots=True)
class PrepResult(Event):
    kind: str                         # PREP_KINDS
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ContractNoteRow:
    broker_order_id: str
    total: float
    components: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ContractNoteResult(Event):
    account: Account
    ok: bool
    rows: tuple[ContractNoteRow, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryStart(Event):
    """Posted once at startup (§9.5 step 10): the engine runs recovery (phase RECOVERING) before READY."""


@dataclass(frozen=True, slots=True)
class SelfcheckResult(Event):
    """The answer to RunSelfcheck: the in-bot self-check run after prep (§9.5 step 13, DETAIL §13.7).

    report is the titan2.selfcheck.Report (None when the run itself raised; error then says why). The core raises
    report.halts(), dispatches report.alert_commands() and applies report.demotions(). Each failure only adds a
    halt: report.fatal after READY becomes HALT(INVARIANT) + CRITICAL, never an exit (positions may be open).
    """

    phase: str                        # titan2.selfcheck phase name: "prestart" in the bot
    ok: bool
    report: object = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Shutdown(Event):
    reason: str


# ---------------------------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Place(Command):
    account: Account
    order_pk: str
    req: OrderRequest


@dataclass(frozen=True, slots=True)
class Cancel(Command):
    account: Account
    order_pk: str
    broker_order_id: str | None


@dataclass(frozen=True, slots=True)
class PollOrder(Command):
    account: Account
    order_pk: str
    broker_order_id: str | None


@dataclass(frozen=True, slots=True)
class LookupTag(Command):
    account: Account
    order_pk: str


@dataclass(frozen=True, slots=True)
class FetchTrades(Command):
    account: Account
    order_pk: str
    broker_order_id: str


@dataclass(frozen=True, slots=True)
class FetchPositions(Command):
    account: Account
    priority: int                     # Priority


@dataclass(frozen=True, slots=True)
class FetchOrders(Command):
    account: Account
    priority: int


@dataclass(frozen=True, slots=True)
class FetchTradebook(Command):
    account: Account


@dataclass(frozen=True, slots=True)
class FetchMargins(Command):
    account: Account


@dataclass(frozen=True, slots=True)
class FetchSquareoffs(Command):
    """PAPER and SHADOW only (the §9.6 step 7 backstop, review round 2): the broker's own square-off orders on
    `symbols` placed from session `since` up to yesterday, and each symbol's net at the start of today. The paper
    exchange keeps every day's orders; Kite serves only today's, so a LIVE leftover still closes UNRESOLVED."""

    account: Account
    request_id: str
    symbols: tuple[str, ...]
    since: date


@dataclass(frozen=True, slots=True)
class FetchQuotes(Command):
    request_id: str
    symbols: tuple[str, ...]
    priority: int


@dataclass(frozen=True, slots=True)
class FetchHistorical(Command):
    request_id: str
    symbol: str
    token: int
    frm: datetime
    to: datetime
    interval: str = "5minute"


@dataclass(frozen=True, slots=True)
class Prep(Command):
    kind: str                         # PREP_KINDS


@dataclass(frozen=True, slots=True)
class ContractNoteOrder:
    """One order for kite.get_virtual_contract_note (§8.1)."""

    broker_order_id: str
    exchange: str
    tradingsymbol: str
    side: Side
    qty: int
    avg_px: float
    product: str = MIS
    order_type: str = LIMIT
    variety: str = REGULAR


@dataclass(frozen=True, slots=True)
class ContractNote(Command):
    account: Account
    orders: tuple[ContractNoteOrder, ...]


@dataclass(frozen=True, slots=True)
class Subscribe(Command):
    tokens: tuple[int, ...]
    mode: str                         # FEED_MODES


@dataclass(frozen=True, slots=True)
class Unsubscribe(Command):
    tokens: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Alert(Command):
    severity: str                     # ALERT_SEVERITIES
    code: str
    text: str
    dedup_key: str | None = None


@dataclass(frozen=True, slots=True)
class StartSignals(Command):
    """Start the signals thread once prep is done (§9.5 step 14)."""


@dataclass(frozen=True, slots=True)
class RunSelfcheck(Command):
    """Run a titan2.selfcheck phase once prep is done (§9.5 step 13); answered by SelfcheckResult.

    The runtime runs it on a thread of its own, never on lane-reads: the checks run subprocesses and a fresh
    interpreter for up to minutes, and EXIT-priority quotes on lane-reads must never wait behind them.
    """

    phase: str = "prestart"


@dataclass(frozen=True, slots=True)
class Exit(Command):
    """Process exit with this code (not an exit-engine decision)."""

    code: int


# Broker commands that PAPER and SHADOW accounts accept exactly like LIVE (served by paper-sim).
BROKER_COMMANDS = (Place, Cancel, PollOrder, LookupTag, FetchTrades, FetchPositions, FetchOrders, FetchTradebook,
                   FetchMargins)
