"""EngineState and EngineContext: what the core thread keeps in RAM (spec §3.3-3.4, §4.5, §9.2; DETAIL §2).

EngineState is a cache of titan2.db rows plus RAM-only memory (order timers, broker snapshots, recon memory,
quote-refresh state, per-leg exit-loop memory). Engine.load() rebuilds it from the DB after every rollback, so it is
never the only copy of a fact that matters after a restart: what must survive lives in rows (halts, modes, the
self-test outcome, EOD steps, handled invariant violations and command acks are all read back from the DB here).

EngineContext is what the runtime hands the engine once per process: settings, calendar, clock, the QuoteCache,
the strategy registry's versions, the StructureHandlers, the gateway and exit-loop modules, callables into the
feed and prep holders, the pub/ and report paths, and a few process-lifetime flags (recovered, prep results) that
must survive an engine reload.

The contract of the two U13 modules the engine calls (core/gateway.py, core/exit_loop.py) is GatewayPort and
ExitLoopPort below; the services they may call on the engine are listed in core/engine.py's docstring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol

from titan2.clock import Clock, SessionCalendar, parse_wall
from titan2.core.events import (
    Command, FetchSquareoffs, MarginsResult, OrdersSnapshot, PositionsSnapshot, QuoteResult,
)
from titan2.core.exits import RefreshState
from titan2.core.orders import OrderTimers
from titan2.core.recon import CoverPlan, ReconMemory, VerifyState
from titan2.market.quotes import QuoteCache
from titan2.model import (
    Account, ExitProfile, FeedHealth, Halt, Instrument, Intent, Leg, LegState, Order, Role, Structure, StructureType,
)
from titan2.settings import Settings

if TYPE_CHECKING:
    from titan2.store import Store

__all__ = [
    "ACCOUNTS", "BrokerView", "EngineContext", "EngineState", "ExitLoopPort", "FlattenTrack", "GatewayPort",
    "RecoveryAcct", "SelfTestRun", "Tail", "eod_phase_rows",
]

log = logging.getLogger(__name__)

ACCOUNTS = (Account.LIVE, Account.PAPER, Account.SHADOW)
SOURCES = ("DASH", "CLI")                       # command DBs (§4.4)
FLATTEN_KINDS = ("FLATTEN_ALL", "KILL")         # commands acked DONE only once flat is verified


def _no_feed() -> FeedHealth:
    return FeedHealth(False, None, None, 0, False)


def _stamp(ts: object) -> datetime | None:
    try:
        return parse_wall(str(ts))
    except (TypeError, ValueError):
        return None


def eod_phase_rows(store: Store, session: date, now_wall: datetime) -> tuple[list[dict], list[dict]]:
    """(kept, ignored): the session's PHASE events that record an EOD step, without the rows stamped later than a
    moment recorded after them: now, or a later start's BOOT event. Such a row was written on a clock running
    ahead of the real one. Before `titan2 laptop --date D` had its own folders (review round 3, N5), a rehearsal
    wrote D's EOD timeline into the real state folder; on the real day D those rows made the morning start
    recovery-only (no session) and the engine skip D's EOD steps (review round 3 follow-up). A row the engine wrote
    on its day is never stamped after a later start, nor after now. A row whose time cannot be read is ignored too.
    EngineState.load (eod_done) and the runtime's recovery-only decision after DONE both read the session's steps
    through this."""
    rows = [e for e in store.events(session=session, kind="PHASE") if (e["payload"] or {}).get("step")]
    if not rows:
        return [], []
    boots = [(int(b["seq"]), _stamp(b["ts"]))
             for b in store.query("SELECT seq, ts FROM events WHERE kind = 'BOOT' AND seq > ? ORDER BY seq",
                                  (rows[0]["seq"],))]
    kept: list[dict] = []
    ignored: list[dict] = []
    for e in rows:
        at = _stamp(e["ts"])
        later = [t for seq, t in boots if seq > e["seq"] and t is not None]
        (ignored if at is None or at > min([now_wall, *later]) else kept).append(e)
    return kept, ignored


# ---------------------------------------------------------------------------------------------------------------
# The contract of U13's modules (the engine calls exactly these; modules satisfy the protocols structurally)
# ---------------------------------------------------------------------------------------------------------------
class GatewayPort(Protocol):
    """core/gateway.py (§5.4, §10.5-10.6). Every function runs on the core thread inside the engine's transaction."""

    def on_intent(self, eng: Any, intent: Intent) -> list[Command]:
        """IntentReceived: validate and insert the intent, route it, run G01-G13 per book (eng.gate_context),
        reserve (eng.reserve) and request the decision quotes (FetchQuotes with request_id = the intent id). Intents
        already waiting (stage WAIT: G09 refused them only because of pending structures) are routed first."""

    def on_quote_result(self, eng: Any, ev: QuoteResult) -> list[Command] | None:
        """A QuoteResult. None when request_id is not one of the gateway's entry requests (the engine then ignores
        it); else run G14-G22 per RESERVED structure, plan and place (eng.begin_entry) or void (eng.void). A
        PAPER:<s> entry opens its placebo in the same transaction (§10.6)."""

    def on_timer(self, eng: Any) -> list[Command]:
        """Every Timer: void RESERVED structures whose intent expired or whose quote request was lost, then route the
        waiting intents again, in arrival order."""


class ExitLoopPort(Protocol):
    """core/exit_loop.py (§5.5, §9.4). Every function runs on the core thread inside the engine's transaction."""

    def step(self, eng: Any, leg: Leg) -> list[Command]:
        """One exit-loop pass for an EXITING or VERIFY_FLAT leg (every Timer and every relevant event): broker cap,
        exit attempts (eng.create_order), verify-flat and the close (eng.close), STOP_SENDING, the backstop band.
        The Timer also calls it for any other leg that has exit-loop memory (eng.state.exit_memory): a cover hangs
        off any leg (§9.3) and runs on the same cadence; with nothing to do it returns []."""

    def on_order_terminal(self, eng: Any, order: Order, leg: Leg) -> list[Command]:
        """An EXIT or COVER order of the leg just became terminal: band ladder, band-reject re-price, EXIT_STUCK."""

    def cover(self, eng: Any, plan: CoverPlan) -> list[Command]:
        """Recon found a bot-explained short (§9.3): BUY back exactly plan.qty through the cover loop."""


# ---------------------------------------------------------------------------------------------------------------
# The runtime's hand-over
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class EngineContext:
    """Built once by the runtime; the same object is passed to every Engine.load of the process."""

    settings: Settings
    calendar: SessionCalendar
    clock: Clock
    quotes: QuoteCache
    versions: Mapping[str, str] = field(default_factory=dict)          # registered strategy -> running version
    version_inputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)   # g1_trials.version_inputs
    exit_profiles: Mapping[str, ExitProfile] = field(default_factory=dict)          # strategy -> EXIT_PROFILE
    disabled: frozenset[str] = frozenset()                             # refused at registry load (G01)
    handlers: Mapping[StructureType, Any] = field(default_factory=dict)  # plugins.load_handlers()
    gateway: Any = None                                                # default: titan2.core.gateway
    exit_loop: Any = None                                              # default: titan2.core.exit_loop
    allow_live: bool = False                                           # env ALLOW_LIVE == "1" (key 1)
    # False in the laptop profile (PAPER only), whose KiteBroker refuses every LIVE place and cancel: recovery then
    #   never imports today's titan2 LIVE orders that titan2.db lacks (another titan2, e.g. EC2, placed them); it
    #   halts ACCOUNT:LIVE (UNKNOWN_POSITION) with a CRITICAL alert instead
    live_orders: bool = True
    # "<source>:<requested_by>" of a FLATTEN_ALL that is part of a stop (the laptop runner's first Ctrl+C,
    #   "CLI:laptop-ctrl-c"); None elsewhere. That command expires like a non-operator one (it never runs at a later
    #   start), and the FLATTEN halt it raised is cleared, once flat, as the run ends (operator.release_stop_flatten)
    stop_flatten_by: str | None = None
    safe_mode: bool = False                                            # bot --safe / TITAN2_SAFE=1
    settings_errors: tuple[str, ...] = ()                              # non-empty: the settings file was invalid
    instruments: Callable[[], Any] = lambda: None                      # today's InstrumentStore (prep fills it)
    ban_list: Callable[[], frozenset[str] | None] = lambda: None       # today's F&O ban list (None = missing)
    feed_health: Callable[[], FeedHealth] = _no_feed
    take_dirty: Callable[[], frozenset[str]] = frozenset
    signals_degraded: Callable[[], bool] = lambda: False
    unacked_critical: Callable[[], int] = lambda: 0
    # information only (2026-10-14 night), never an input to a decision: the quote lane's measured calls since the
    #   last take (ReadsLane.take_quote_calls; the engine writes them into its once-a-minute LATENCY event), the
    #   alerter's counters, and one-line notes from outside the engine for the 15:38 phone report (the laptop's
    #   paper condor)
    take_quote_calls: Callable[[], Any] = list
    alert_counts: Callable[[], Mapping[str, int]] = dict
    report_notes: Callable[[], Mapping[str, str]] = dict
    pub_dir: Path | None = None
    reports_dir: Path | None = None
    exports_dir: Path | None = None
    command_dbs: Mapping[str, Path] = field(default_factory=dict)      # "DASH" | "CLI" -> commands DB file
    start_mono: float | None = None                                    # process start (§9.2 after a restart)
    # process-lifetime flags: they outlive an engine reload
    engine_loads: int = 0                                              # Engine instances so far: request-id prefix
    recovered: bool = False                                            # recovery done: the runtime sends READY
    recovery_started: bool = False
    prep: dict[str, bool] = field(default_factory=dict)                # Prep kind -> last answer ok
    signals_started: bool = False
    stop_mono: float | None = None                                     # SIGTERM seen (Engine._on_shutdown)
    # READY came at or after BROKER_SQUAREOFF_AT on a trading day: until then recon keeps 5 s rounds while a leg is
    #   held and the broker differs, and the EOD steps from TRUTH on wait (Engine._late_hold, review round 2).
    #   late_recon_from is that READY's monotonic time: the hold's grace counts from it, not from start_mono
    late_recon_until: datetime | None = None
    late_recon_from: float | None = None
    # FetchSquareoffs in flight (the leftover backstop): request_id -> (the read, how many were sent for it). Here,
    #   not in EngineState, so a failed answer that comes after a rollback reload is still asked again or alerted
    squareoff_asks: dict[str, tuple[FetchSquareoffs, int]] = field(default_factory=dict)
    # order_pk -> the late-accept deadline its timers last held (orders.OrderTimers.late_until: LATE_ACCEPT_S after
    #   an AMBIGUOUS answer, a lost answer's lookups or the restart). Here, not in EngineState, so a rollback reload
    #   ends the window where the answer put it (review round 3: rebuilt from the submit, it ended early after a late
    #   answer, and a second SELL could go out while the first could still be accepted)
    late_until: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------------------
# RAM-only parts of the state
# ---------------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class BrokerView:
    """One account's latest broker reads (§9.2) and recon memory. RAM only: a reload starts afresh."""

    positions: PositionsSnapshot | None = None       # newest OK positions read (for caps and verify-flat)
    orders: OrdersSnapshot | None = None             # newest OK orders read
    pos_pending: int = 0                             # FetchPositions sent and not answered yet (== len(pos_sent))
    ords_pending: int = 0
    pos_sent: list[float] = field(default_factory=list)    # send times of the unanswered reads, oldest first: a
    ords_sent: list[float] = field(default_factory=list)   # read lost for READ_LOST_S stops counting as in flight
    pos_failed: float | None = None                  # req_mono of the newest failed positions read (the exit cap's
    ords_failed: float | None = None                 # fallback and read pacing, recon.broker_cap); orders likewise
    margins_sent: float | None = None
    recon: ReconMemory = field(default_factory=ReconMemory)
    round_start: float | None = None                 # a reconcile waits for both reads requested after this
    round_pos: PositionsSnapshot | None = None
    round_ords: OrdersSnapshot | None = None
    last_round_mono: float | None = None             # recon cadence (recon.recon_due)
    last_recon_mono: float | None = None             # when reconcile() last ran
    recon_req: float | None = None                   # req_mono of the positions read behind that reconcile
    extra_round_at: float | None = None              # a second reconcile >= 3 s later (after recovery)
    margins: MarginsResult | None = None
    margins_pending: bool = False
    audit_mono: dict[str, float] = field(default_factory=dict)   # broker_snapshots: last OK row per kind


@dataclass(slots=True)
class RecoveryAcct:
    """§9.6 step-1 reads of one account (3 tries, 2 s apart)."""

    tries: int = 1
    orders: OrdersSnapshot | None = None
    positions: PositionsSnapshot | None = None
    tradebook_ok: bool | None = None
    retry_at: float | None = None
    done: bool = False

    def answered(self) -> bool:
        return self.orders is not None and self.positions is not None and self.tradebook_ok is not None

    def all_ok(self) -> bool:
        return self.answered() and self.orders.ok and self.positions.ok and self.tradebook_ok is True


@dataclass(slots=True)
class SelfTestRun:
    """The daily LIVE order-path self-test in progress (§10.8)."""

    attempt: int
    stage: str                       # SPOT (waiting for NIFTY spot) | QUOTE (option quote) | ORDER (sent)
    started_mono: float
    request_id: str | None = None
    inst: Instrument | None = None
    order_pk: str | None = None
    structure_id: int | None = None


@dataclass(slots=True)
class FlattenTrack:
    """A FLATTEN_ALL or KILL command acked ACCEPTED: DONE once flat is verified, FAILED after 60 s (§8.9)."""

    kind: str
    started_mono: float
    failed: bool = False


@dataclass(slots=True)
class Tail:
    """The 45-min post-exit tail of a closed leg (§7.5): POST quote_checks rows every 5 s, then Unsubscribe."""

    symbol: str
    token: int
    until_mono: float
    last_row_mono: float | None = None


# ---------------------------------------------------------------------------------------------------------------
# EngineState
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class EngineState:
    session: date
    structures: dict[int, Structure]                 # every non-terminal structure plus all of this session's
    legs: dict[int, Leg]                             # their legs, bound to `orders`
    orders: dict[str, Order]                         # their orders (bot and EXTERNAL)
    timers: dict[str, OrderTimers] = field(default_factory=dict)
    halts: tuple[Halt, ...] = ()                     # active (uncleared) halt rows
    modes: dict[str, dict[str, Any]] = field(default_factory=dict)   # strategy_modes rows by strategy
    broker: dict[Account, BrokerView] = field(default_factory=lambda: {a: BrokerView() for a in ACCOUNTS})
    refresh: dict[str, RefreshState] = field(default_factory=dict)   # REST refresh state per option symbol
    quote_requests: dict[str, tuple[tuple[str, ...], float]] = field(default_factory=dict)  # request_id -> (syms, sent)
    last_rb: dict[int, float] = field(default_factory=dict)          # leg_id -> last realizable bid (recon pricing)
    # exit-loop and gateway memory (owned by those modules; RAM only)
    cap_wait: dict[int, float] = field(default_factory=dict)         # leg_id -> broker_cap wait start
    verify: dict[int, VerifyState] = field(default_factory=dict)     # leg_id -> verify-flat memory
    exit_memory: dict[int, Any] = field(default_factory=dict)        # leg_id -> the exit loop's per-leg memory
    # intent_id -> the gateway's memory: the fetch it waits for, or its WAIT (arrival order)
    entry_memory: dict[str, Any] = field(default_factory=dict)
    gate_errors: tuple[float, ...] = ()                              # gateway: risk.note_gate_error window
    # evaluation bookkeeping
    dirty_marks: set[int] = field(default_factory=set)               # legs with peak/trough/stop not yet flushed
    last_mark_flush: float | None = None
    hold_rows: dict[int, tuple[float, str]] = field(default_factory=dict)   # leg_id -> (last HOLD row, decision)
    notes_seen: dict[int, frozenset[str]] = field(default_factory=dict)    # leg_id -> decide() notes last time
    stop_event_mono: dict[int, float] = field(default_factory=dict)        # STOP_MOVED events <= 1/s per leg
    tails: dict[int, Tail] = field(default_factory=dict)
    # facts read back from the DB at load
    known_violations: set[tuple[str, str, str]] = field(default_factory=set)   # (code, scope, detail) handled today
    selftest_passed: bool = False
    selftest_attempts: int = 0                                             # finished attempts today
    selftest_run: SelfTestRun | None = None
    eod_done: set[str] = field(default_factory=set)                        # EOD timeline steps done today
    eod_ran: dict[str, tuple[float, datetime]] = field(default_factory=dict)   # step -> (mono, wall) it ran (RAM)
    cmd_last_seen: dict[str, int] = field(default_factory=dict)            # source -> highest cmd_id picked
    flatten: dict[tuple[str, int], FlattenTrack] = field(default_factory=dict)
    last_heartbeat_wall: datetime | None = None
    last_heartbeat_mono: float | None = None
    # process duties
    recovery: dict[Account, RecoveryAcct] = field(default_factory=dict)
    recovery_started_mono: float | None = None
    entries_resumed: bool = False                                          # §9.6 latch (2 CLEAN recons, warm feed)
    stopping: bool = False
    stop_mono: float | None = None
    exit_sent: bool = False
    last_timer: tuple[float, datetime] | None = None                       # I15
    last_poll_mono: float | None = None                                    # command DBs, 1 Hz
    last_risk_mono: float | None = None                                    # daily stop, 1 Hz
    prep_pending: set[str] = field(default_factory=set)
    prep_retry_at: dict[str, float] = field(default_factory=dict)
    token_due: float | None = None
    token_pending: bool = False
    feed_dead_since: float | None = None                                   # NORECONNECT
    feed_healthy_since: float | None = None                                # STALE_FEED auto clear
    tradebook_purpose: dict[Account, str] = field(default_factory=dict)    # RECOVERY | TRUTH
    bar_requests: dict[str, str] = field(default_factory=dict)             # request_id -> symbol (15:36)
    vcn_pending: set[Account] = field(default_factory=set)                 # 15:33 contract notes not answered yet
    vcn_sample_unsupported: bool = False

    # -- loading ------------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, store: Store, now_mono: float, now_wall: datetime, session: date) -> EngineState:
        """Rebuild from titan2.db: structures/legs/orders (store.load_state re-anchors monotonic times), halts,
        modes, and the facts of this session the engine must not repeat or forget."""
        ls = store.load_state(now_mono, now_wall, session=session)
        st = cls(session=session, structures=ls.structures, legs=ls.legs, orders=ls.orders)
        st.halts = tuple(store.active_halts())
        st.modes = {r["strategy"]: r for r in store.strategy_modes()}
        for e in store.events(session=session, kind="SELFTEST"):
            outcome = e["payload"].get("outcome")
            if outcome == "PASSED":
                st.selftest_passed = True
            elif outcome == "FAILED":
                st.selftest_attempts += 1
            elif outcome == "FILLED":
                st.selftest_attempts = max(st.selftest_attempts + 1, 2)     # a filling self-test is never retried
        for e in store.events(session=session, kind="INVARIANT_VIOLATION"):
            p = e["payload"]
            if p.get("handled") and p.get("code") and p.get("scope"):
                st.known_violations.add((str(p["code"]), str(p["scope"]), str(p.get("detail", ""))))
        kept, ignored = eod_phase_rows(store, session, now_wall)
        if ignored:
            log.warning("%s: ignoring %d EOD step row(s) stamped later than now or than a later start (%s): not "
                        "written by a run on that day (an old `titan2 laptop --date` rehearsal in the real state "
                        "folder?); those steps run as usual", session, len(ignored),
                        ", ".join(f"{e['payload'].get('step')} at {e['ts']} (seq {e['seq']})" for e in ignored))
        for e in kept:
            st.eod_done.add(str(e["payload"]["step"]))
        st.cmd_last_seen = {s: store.last_command_id(s) for s in SOURCES}
        rows = store.query("SELECT source, cmd_id, kind, status FROM command_acks WHERE kind IN (?, ?) "
                           "AND status IN ('ACCEPTED', 'FAILED')", FLATTEN_KINDS)
        st.flatten = {(r["source"], r["cmd_id"]): FlattenTrack(r["kind"], now_mono, r["status"] == "FAILED")
                      for r in rows}
        ts = store.scalar("SELECT ts FROM events WHERE kind = 'HEARTBEAT' ORDER BY seq DESC LIMIT 1")
        st.last_heartbeat_wall = None if ts is None else parse_wall(ts)
        return st

    # -- queries ------------------------------------------------------------------------------------------------
    def legs_of(self, st: Structure) -> list[Leg]:
        return [self.legs[i] for i in st.legs if i in self.legs]

    def structure_of(self, leg: Leg) -> Structure:
        return self.structures[leg.structure_id]

    def live_structures(self) -> list[Structure]:
        """Non-terminal structures, oldest first."""
        return [s for _, s in sorted(self.structures.items()) if not s.is_terminal]

    def legs_in(self, *states: LegState) -> list[Leg]:
        """Legs in one of `states` whose structure is not terminal, by leg_id."""
        out = []
        for lid in sorted(self.legs):
            leg = self.legs[lid]
            st = self.structures.get(leg.structure_id)
            if leg.state in states and st is not None and not st.is_terminal:
                out.append(leg)
        return out

    def busy(self, leg: Leg) -> list[Order]:
        """The leg's bot orders that are not terminal (I4 allows at most one)."""
        return [self.orders[pk] for pk in leg.orders
                if pk in self.orders and self.orders[pk].role != Role.EXTERNAL and not self.orders[pk].is_terminal]

    def working_orders(self) -> list[Order]:
        """Every bot order that is not terminal."""
        return [o for o in self.orders.values() if o.role != Role.EXTERNAL and not o.is_terminal]

    def held_tokens(self, *, except_leg: int | None = None) -> set[int]:
        """Feed tokens a non-terminal leg or a post-exit tail still needs."""
        out = {t.token for lid, t in self.tails.items() if lid != except_leg}
        for leg in self.legs.values():
            if leg.leg_id != except_leg and not leg.is_terminal:
                st = self.structures.get(leg.structure_id)
                if st is not None and not st.is_terminal:
                    out.add(leg.inst.token)
        return out
