"""The engine: Engine.handle(event) -> list[Command] on the single core thread (spec §1, §3.3, §8.9, §9, §10.8,
§12.5; DETAIL §2 core/engine.py).

The runtime's core loop (§3.3) calls, inside one store.tx() per event:
    cmds = engine.handle(ev); engine.check_invariants()
and dispatches cmds only after the commit. On InvariantViolation it rolls back, reloads (Engine.load) and calls
engine.on_invariant_violation(iv) in a new transaction; on any other exception it reloads and calls
engine.raise_halt("EXCEPTION", "GLOBAL", "ACK", repr(exc)). After each commit it may read engine.recovery_done
(send READY=1 once true) and engine.phase.

What handle() does
- Order events go through core/orders.py; a terminal ENTRY/SELFTEST order opens or voids its structure
  (positions.on_entry_terminal), a terminal EXIT/COVER order goes to the exit loop.
- OPEN legs are evaluated through their StructureHandler (§14; phase 1: exits.decide(), also the fallback when no
  handler is registered) on every Timer, on QuotesDirty for their symbol and after a REST refresh (§7.4-7.5): marks
  are applied, LOCK_ARMED and exits persisted at once, peak/trough/stop at most 1/s, quote_checks HOLD rows at most
  1/s per leg plus every decision change. EXITING/VERIFY_FLAT legs get exit_loop.step() on the same cadence, in
  their handler's exit_sequence, and again when a positions or orders read of their account lands (the exit cap
  and verify-flat wait for one); on each Timer so does any other leg holding exit-loop memory (a cover hangs off
  any leg, §9.3). Forced exits come from the KILL/FLATTEN/DAILY_STOP halts and anomalies.
- Broker reads in flight are counted per account; one unanswered for READ_LOST_S stops counting (WARN). A token or
  permission error on any read raises HALT(TOKEN) like one on an order (§9.7).
- Timer duties: order timers, recon cadence (§9.3), margins, prep and the prestart self-check, the token re-check,
  feed health, AUTO halt clears, the daily stop (1 Hz), the LIVE self-test (§10.8), command polling (1 Hz, §8.9),
  the EOD timeline (§12.5), the 60 s heartbeat and the heartbeat-gap IMPAIRED mark, post-exit tails, I15.
- Recovery (§9.6) on RecoveryStart, recon application (§9.3), the 15:33 truth and contract-note checks, the 15:36
  bar check, the 15:38 exports / report / nightly G1 / live review / weekly prune, Exit(0) at 15:40. A start after
  BROKER_SQUAREOFF_AT gets the late recon (_late_hold), and a paper leftover the paper exchange closed on an earlier
  day is booked from its record (FetchSquareoffs, _on_squareoffs) (review round 2). EOD steps that fall due together
  (a start after 15:40, a late hold that ends after their times) wait for the earlier ones' answers and files
  (_eod_waits), so the Exit never comes first.
- pub/ files are written at the start of the next handle() (after the previous transaction committed), so pub/
  never shows a rolled-back row; a failed write is a WARN, never a halt.

Contract with the gateway and the exit loop (U13; their side is GatewayPort / ExitLoopPort in core/state.py).
They run inside the engine's transaction and may use, on the Engine `eng` they are handed:
  data      eng.store eng.ctx eng.settings eng.calendar eng.quotes eng.state eng.session eng.now_mono eng.now_wall
            eng.rates eng.eod eng.instruments eng.ban_list eng.handlers
  gates     eng.gate_context(book) -> risk.GateContext; eng.book_state(book); eng.live_preconditions(strategy)
  entries   eng.reserve(intent, book=, account=, policy=, ...) -> Structure; eng.void(st, reason);
            eng.begin_entry(st, inst=, qty=, limit_px=, decision_quote=, decision=, role=) -> (leg, order, cmds)
            eng.flag_impaired(strategy, detail)
  exits     eng.create_order(leg, role, qty, limit_px, decision=, band_idx=) -> (order, cmds) (I14 handled here);
            eng.close(st, residual_qty=0) -> cmds; eng.close_external(st, leg, ext) -> cmds;
            eng.symbol_view(account, symbol); eng.bot_view(account); eng.forced_reason(leg); eng.profile(strategy)
  reads     eng.request_quotes(symbols, priority) -> cmds (refresh state kept per symbol);
            eng.request_positions(account, priority) / eng.request_orders(account, priority) -> cmds (deduped);
            eng.state.broker[account] holds the latest snapshots and the in-flight counts
  halts     eng.add_halt(kind, scope, reason, ...); eng.apply_halt(HaltRequest, alert=); eng.event(kind, payload, ...)
  memory    eng.state.cap_wait / verify / exit_memory (exit loop), eng.state.entry_memory and gate_errors
            (gateway); the writers among these services are @core_only
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import math
import uuid
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Iterable, Mapping

from titan2 import g1, ledger
from titan2.broker.port import FATAL, error_outcome, snapshot_of
from titan2.charges import OrderFills, order_charges, rates_for
from titan2.clock import ist_datetime, iso, session_of, to_ist
# exit_loop and gateway are imported with the engine, never at first use: titan2/ is the `current` release link,
#   so a module first imported after a release switch without a restart would come from the NEW release while
#   this process runs the old one (DETAIL §13.1: a switch takes effect only on restart)
from titan2.core import exit_loop as exit_loop_module
from titan2.core import exits, invariants, operator, orders, positions, recon, risk, sizing
from titan2.core import gateway as gateway_module
from titan2.core.events import (
    Alert, BarsClosed, CancelResult, Command, ContractNote, ContractNoteOrder, ContractNoteResult, Event, Exit,
    FeedState, FetchHistorical, FetchMargins, FetchOrders, FetchPositions, FetchQuotes, FetchSquareoffs,
    FetchTradebook, HistoricalResult, IntentReceived, LookupResult, LookupTag, MarginsResult, OrderSnapshot,
    OrdersSnapshot, PlaceResult, PositionsSnapshot, Prep, PrepResult, Priority, QuoteResult, QuotesDirty,
    RecoveryStart, RunSelfcheck, SelfcheckResult, Shutdown, SquareoffsResult, StartSignals, Subscribe, Timer,
    TradebookSnapshot, TradesResult, Unsubscribe,
)
from titan2.core.orders import OrderTimers, whole_ms
from titan2.core.recon import ExternalClose, ExternalFill, HaltRequest, ReconClass, StaleStructure, TagImport
from titan2.core.risk import BookState, GateContext, LivePreconditions
from titan2.core.single_leg import HandlerCtx
from titan2.core.state import ACCOUNTS, EngineContext, EngineState, RecoveryAcct, SelfTestRun, Tail
from titan2.market.bars import bar_check_symbols, check_bar, find_bar
from titan2.model import (
    EXPIRY_RULE_NEAREST, ROLE_SIDE, Account, DecisionQuote, Direction, ExitProfile, Halt, HaltClears, Instrument,
    Intent, InvariantViolation, Leg, LegState, Mode, Order, OrderState, PnlStatus, Policy, Quote, Recheck, Refusal,
    Role, Side, StrikePref, Structure, StructState, StructureType, Urgency, ceil_tick, floor_tick, make_tag,
    parse_tag,
)
from titan2.settings import (
    FRESH_FILL_GRACE_S, G1_IMPAIRED_GAP_S, HEARTBEAT_EVERY_S, POST_EXIT_TAIL_MIN, QUOTE_REQUEST_TIMEOUT_S,
    RECON_CONFIRM_GAP_S,
)
from titan2.store import core_only

__all__ = ["Engine", "EngineContext", "EngineState", "NIFTY_SPOT", "SELFTEST_BOOK", "SELFTEST_STRATEGY"]

log = logging.getLogger(__name__)

NIFTY_SPOT = "NSE:NIFTY 50"
SELFTEST_BOOK = risk.SELFTEST_BOOK                    # "LIVE:_selftest"
SELFTEST_STRATEGY = "_selftest"
SELFTEST_TIMES = (dtime(9, 15, 30), dtime(9, 17, 0))  # first attempt, retry (earliest times: a late start runs them)
SELFTEST_EXPECT_S = 5.0                               # CANCELLED with 0 filled within 5 s of the send
SELFTEST_STAGE_S = QUOTE_REQUEST_TIMEOUT_S + 2.0      # a quote step that waits longer fails the attempt
# §10.8: the self-test proves the tag round trip and WS order updates (DETAIL §10.8; the [V] §17.2 #4 drift check).
# After the CANCELLED the order must also have shown its tag in a WS order update and in orders() (a LookupTag,
# asked every SELFTEST_LOOKUP_EVERY_S) within SELFTEST_VERIFY_S, else the attempt fails.
SELFTEST_VERIFY_S = 5.0
SELFTEST_LOOKUP_EVERY_S = 1.5
LATE_FILL_OF = "late_fill_of"     # intent reason key of a RECOVERED structure holding a NOT_PLACED order's late fill
# EOD timeline (§12.5). The post-square-off steps are offsets from BROKER_SQUAREOFF_AT so they move with that [V]
# setting; at its default 15:26 they are the spec's 15:27, 15:31, 15:33, 15:36, 15:38 and 15:40.
EOD_AFTER_SQUAREOFF_S = (("RECON_1", 60), ("RECON_2", 300), ("TRUTH", 420), ("BAR_CHECK", 600),
                         ("EOD_TASKS", 720), ("DONE", 840))
# A start at or after BROKER_SQUAREOFF_AT (review round 2): the square-off may have run while the bot was down (the
#   paper exchange runs it at its first step), after the periodic recon window, or too close to its end for recon to
#   confirm it. While a leg is held and the broker differs, recon keeps its 5 s rounds and the EOD steps from TRUTH
#   on (and a recovery-only run's exit) wait, so the square-off is booked today from today's orders; until
#   LATE_RECON_UNTIL, or LATE_RECON_MIN_S after READY when that is later
LATE_RECON_UNTIL = dtime(16, 0)
LATE_RECON_MIN_S = 60.0
LATE_HELD_STEPS = frozenset({"TRUTH", "BAR_CHECK", "EOD_TASKS", "DONE"})
# EOD steps that fall due together (a start after 15:40, a late start's hold that ends after their times): each one
#   waits for what the earlier steps it reads sent, so the nightly G1 (EOD_TASKS) sees the day's truth check and
#   bar checks, and DONE's Exit, which ends the core loop, comes after the answers and the day's files (review round
#   2 follow-up). Each wait lasts at most the normal timeline's gap between the two steps (EOD_AFTER_SQUAREOFF_S)
EOD_WAITS_FOR = {"EOD_TASKS": ("TRUTH", "BAR_CHECK"), "DONE": ("TRUTH", "BAR_CHECK", "EOD_TASKS")}
EOD_FILES = frozenset({"trades_90d", "exports", "report"})   # EOD_TASKS' marks (_publish): no later load writes them
SQUAREOFF_ASK_TRIES = 3                               # FetchSquareoffs failing this often: left to recon
MARGINS_EVERY_S = 30.0                                # G21 needs a margins read <= 60 s old
MARGINS_RETRY_S = 5.0                                 # after a failed margins read
MARGINS_FROM = dtime(9, 0)
RISK_EVERY_S = 1.0                                    # daily-stop check cadence
STATUS_EVERY_S = 1.0
EVENTS_TAIL_EVERY_S = 2.0
PUB_RETRY_S = 5.0                                     # after a failed pub/ write, every pub/ write waits 5 s ...
PUB_MAX_TRIES = 3                                     # ... and a marked file is retried until 3 failures in a row
MARK_FLUSH_S = 1.0                                    # peak/trough/stop persisted at most once a second
HOLD_ROW_EVERY_S = 1.0
STOP_EVENT_EVERY_S = 1.0
POST_ROW_EVERY_S = 5.0
HEARTBEAT_GAP_SCAN_DAYS = 400                         # a heartbeat gap marks at most this many days' sessions
FEED_NORECONNECT_EXIT_S = 60.0
STALE_FEED_HEALTHY_S = 60.0
TOKEN_RECHECK_S = 30.0
PREP_RETRY_S = 60.0
DAILY_PREP = ("INSTRUMENTS", "BAN_LIST", "PREV_BARS")
SHUTDOWN_WAIT_S = 20.0                                # SIGTERM: entries cancelled (<= 10 s), exits finish (<= 10 s)
RECOVERY_MAX_S = 30.0                                 # recovery reads that never complete are planned anyway
QUOTE_REQUEST_STALE_S = QUOTE_REQUEST_TIMEOUT_S + 2.0
# The lanes answer every positions / orders / margins read (KiteConnect timeout 5 s, EXIT reads first). One still
# unanswered after this long was lost: it stops counting as in flight, else request_positions/request_orders would
# dedupe for the rest of the day and the exit cap and verify-flat would lose their EXIT-priority reads.
READ_LOST_S = 15.0
EXIT_CONTROLLED_RESTART = 75
VCN_SAMPLE_MAX = 50
VCN_TOLERANCE_RS = 1.0
CRITICAL_HALTS = frozenset({"KILL", "INVARIANT", "EXCEPTION", "GATE_ERRORS", "SHORT_DETECTED", "MISMATCH",
                            "UNKNOWN_POSITION", "ORDER_STUCK", "EXIT_DIFFICULTY", "TOKEN", "LIVE_DISABLED_TODAY",
                            "V1_ACTIVE", "NO_INSTRUMENTS", "SETTINGS_INVALID"})
_ORDER_EVENTS = (PlaceResult, CancelResult, OrderSnapshot, LookupResult, TradesResult)
_HELD = (LegState.ENTERING, LegState.OPEN, LegState.EXITING, LegState.VERIFY_FLAT)


def _pos(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _num(x: object) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _seq_flat(imp: TagImport) -> bool:
    """A lost seq whose own broker orders bought and sold the same qty (or filled nothing): it holds no position."""
    return sum(b.filled_qty if Side(b.side) is Side.BUY else -b.filled_qty for b in imp.orders) == 0


@dataclasses.dataclass(slots=True)
class _SelfTestProof:
    """What the running self-test order has shown (RAM, like SelfTestRun): its tag in a WS order update and in an
    orders() read. LookupTag after an AMBIGUOUS place, recovery's tag import and recon's bot-tag test all rely on it."""

    order_pk: str
    ws_tag: bool = False                 # a WS OrderSnapshot carried tag == order_pk (nothing filled)
    lookup_tag: bool = False             # a LookupTag or an orders() read found the order by its tag (nothing filled)
    lookup_sent: float | None = None     # the last LookupTag sent for it
    latency_ms: int | None = None        # send -> CANCELLED


class Engine:
    """The functional core. One instance per load; Engine.load() after every rollback."""

    def __init__(self, store: Any, ctx: EngineContext, state: EngineState) -> None:
        self.store = store
        self.ctx = ctx
        self.state = state
        self.settings = ctx.settings
        self.calendar = ctx.calendar
        self.quotes = ctx.quotes
        self.handlers = ctx.handlers
        self.now_mono = ctx.clock.mono()
        self.now_wall = ctx.clock.now_wall()
        # SIGTERM is process state: an engine loaded after a rollback inside the shutdown window keeps stopping, with
        #   the original deadline (§9.5 step 17). EngineContext outlives every load; EngineState is rebuilt from rows.
        if ctx.stop_mono is not None and not state.stopping:
            state.stopping, state.stop_mono = True, ctx.stop_mono
        self._set_session(state.session)
        self._gateway = ctx.gateway
        self._exit_loop = ctx.exit_loop
        self._pending: list[Command] = []          # re-polls and a recon after a load, emitted by the first entry
        self._started = False
        # request ids "<kind>:e<n>:<seq>": a reloaded engine never reuses an earlier one's ids (their late answers are
        # ignored), and they are deterministic, so the §16.1 PAPER and LIVE runs issue identical command streams
        ctx.engine_loads += 1
        self._uid = f"e{ctx.engine_loads}"
        self._seq = itertools.count(1)
        self._counters: dict[str, Any] = {}        # book counters, per event
        self._orders_before: frozenset[str] = frozenset(state.orders)
        self._imported: set[str] = set()           # broker orders imported by recovery (not created by this event)
        self._asked: set[tuple[str, Account]] = set()   # FetchPositions/FetchOrders asked during this event
        self._pub: set[str] = {"acks", "g1", "trades_today"}   # a load rebuilds the pub/ files from the DB
        self._pub_trades: set[int] = set()
        self._last_status: float | None = None
        self._last_events: float | None = None
        self._events_seq: int | None = None
        self._pub_retry_at: float | None = None    # a pub/ write failed: the next writes wait until then
        self._pub_fails: dict[str, int] = {}       # consecutive failures per pub/ file
        self._selftest_proof: _SelfTestProof | None = None
        self._late_blocked: set[str] = set()       # NOT_PLACED orders whose late fill could not be imported (alerted)

    # ------------------------------------------------------------------------------------------------------------
    # Loading and the entry points the runtime calls
    # ------------------------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, store: Any, ctx: EngineContext) -> Engine:
        """Rebuild from titan2.db (read only: runs outside a transaction). Every non-terminal order of this session
        gets fresh timers and its §3.3 re-poll, and every account an immediate recon; the first entry point emits
        those commands."""
        now_mono, now_wall = ctx.clock.mono(), ctx.clock.now_wall()
        state = EngineState.load(store, now_mono, now_wall, session_of(now_wall))
        eng = cls(store, ctx, state)
        for o in state.orders.values():
            # an earlier session's open order is never polled or cancelled: the broker cannot answer for it, and
            #   recovery concludes it (_apply_stale -> orders.session_ended)
            if o.role != Role.EXTERNAL and not o.is_terminal and not eng._earlier_session(o):
                # engine_loads == 1: this process's first load (a restart); later ones are rollback reloads, which
                #   end a late-accept window where the process last had it (EngineContext.late_until)
                timers, cmds = orders.timers_after_reload(o, now_mono, restart=ctx.engine_loads == 1,
                                                          late_until=ctx.late_until.get(o.order_pk))
                state.timers[o.order_pk] = timers
                eng._keep_late_window(o)
                eng._pending.extend(cmds)
        for acct in ACCOUNTS:
            eng._pending.extend(eng.start_recon(acct))
        return eng

    @core_only
    def handle(self, ev: Event) -> list[Command]:
        self._begin(ev)
        out = self._publish() + self._take_pending()
        if not self._started:
            self._started = True
            out += self._startup()
        out += self._dispatch(ev)
        return self._track(out)

    def check_invariants(self) -> None:
        invariants.check(self)

    @core_only
    def on_invariant_violation(self, iv: InvariantViolation) -> list[Command]:
        """After the rollback and reload: record the violation, then HALT(INVARIANT, ACK) for a global or account
        scope, or EXITING(ANOMALY_EXIT) for a leg scope; CRITICAL either way (§3.6). Exits continue."""
        self._begin(None)
        out = self._take_pending() + self._abort_recovery(f"invariant {getattr(iv, 'code', '?')}")
        code, scope, detail = invariants.known_key(iv)
        self.state.known_violations.add((code, scope, detail))
        try:
            leg_id = iv.leg_id if isinstance(iv, InvariantViolation) else None
        except ValueError:                       # a malformed LEG: scope halts globally (below)
            leg_id = None
        self.event("INVARIANT_VIOLATION", {"code": code, "scope": scope, "detail": detail, "handled": True},
                   "CRITICAL", leg_id=leg_id if leg_id in self.state.legs else None)
        text = f"invariant {code} [{scope}]: {detail[:400]}"
        if leg_id is not None and leg_id in self.state.legs:
            out += self._anomaly_exit(self.state.legs[leg_id])
            out.append(Alert("CRITICAL", "INVARIANT", text, f"invariant:{code}:{scope}"))
        else:
            kind, _, value = scope.partition(":")
            halt_scope = scope if kind == "ACCOUNT" and value in {a.value for a in Account} else "GLOBAL"
            out += self.add_halt("INVARIANT", halt_scope, text)
        return self._track(out)

    @core_only
    def raise_halt(self, kind: str, scope: str, clears: HaltClears | str, reason: str, *,
                   operator: bool = False) -> list[Command]:
        """The runtime's call after an exception (§3.3), on the freshly loaded engine. Also usable as a service."""
        out: list[Command] = []
        if not self._started:
            self._begin(None)
            out += self._take_pending() + self._abort_recovery(f"{kind}: {reason[:120]}")
            if kind == "EXCEPTION":
                self.event("EXCEPTION", {"reason": reason[:500]}, "CRITICAL")
        out += self.add_halt(kind, scope, reason, clears=HaltClears(clears), operator=operator)
        return self._track(out)

    @property
    def recovery_done(self) -> bool:
        return self.ctx.recovered

    @property
    def late_recon_holding(self) -> bool:
        """A late start still waits for a square-off to be booked (_late_hold): a recovery-only run does not exit."""
        return self._late_hold()

    @property
    def eod_finishing(self) -> bool:
        """Today's EOD steps from TRUTH on are due and not all done (held by a late start, or waiting for what the
        earlier ones sent, _eod_waits): a recovery-only run does not exit before the engine's own DONE, or it would
        lose the truth check's answers and the day's files (review round 2 follow-up)."""
        if not self.ctx.recovered or not self.calendar.is_trading_day(self.session):
            return False
        return any(step in LATE_HELD_STEPS and step not in self.state.eod_done and self.now_wall >= at
                   for step, at in self.eod_steps())

    @property
    def phase(self) -> str:
        """PREOPEN | RECOVERING | TRADING | EOD | DONE (status.json)."""
        if "DONE" in self.state.eod_done:
            return "DONE"
        if self.ctx.recovery_started and not self.ctx.recovered:
            return "RECOVERING"
        if not self.ctx.recovered:
            return "PREOPEN"
        if "FLATTEN" in self.state.eod_done:
            return "EOD"
        bounds = self.calendar.session_bounds(self.session)
        if bounds is None or self.now_wall < bounds[0]:
            return "PREOPEN"
        return "TRADING"

    # ------------------------------------------------------------------------------------------------------------
    # Per-event plumbing
    # ------------------------------------------------------------------------------------------------------------
    def _begin(self, ev: Event | None) -> None:
        if isinstance(ev, Timer):
            self.now_mono, self.now_wall = ev.now_mono, ev.now_wall
        else:
            self.now_mono, self.now_wall = self.ctx.clock.mono(), self.ctx.clock.now_wall()
        self._counters.clear()
        self._orders_before = frozenset(self.state.orders)
        self._imported.clear()
        self._asked.clear()

    def _take_pending(self) -> list[Command]:
        out, self._pending = self._pending, []
        return out

    def _track(self, cmds: list[Command]) -> list[Command]:
        """Count the broker reads going out, so in-flight checks (recon, verify-flat, the exit cap) stay right."""
        for c in cmds:
            if isinstance(c, (FetchPositions, FetchOrders, FetchMargins)):
                bv = self.state.broker[Account(c.account)]
                if isinstance(c, FetchPositions):
                    bv.pos_sent.append(self.now_mono)
                    bv.pos_pending = len(bv.pos_sent)
                elif isinstance(c, FetchOrders):
                    bv.ords_sent.append(self.now_mono)
                    bv.ords_pending = len(bv.ords_sent)
                else:
                    bv.margins_pending, bv.margins_sent = True, self.now_mono
        return cmds

    def new_order_pks(self) -> list[str]:
        """Orders created by the current event (invariants I6, I12-I14); recovery imports excluded."""
        return [pk for pk in self.state.orders if pk not in self._orders_before and pk not in self._imported]

    def _set_session(self, session: date) -> None:
        self.session = session
        self.state.session = session
        self.rates = rates_for(session)
        self.eod = exits.EodTimes.for_session(session, self.settings)

    def _rid(self, prefix: str) -> str:
        return f"{prefix}:{self._uid}:{next(self._seq)}"

    def _dispatch(self, ev: Event) -> list[Command]:
        if isinstance(ev, Timer):
            return self._on_timer(ev)
        if isinstance(ev, QuotesDirty):
            return self._on_quotes_dirty()
        if isinstance(ev, _ORDER_EVENTS):
            return self._on_order_event(ev)
        if isinstance(ev, PositionsSnapshot):
            return self._on_positions(ev)
        if isinstance(ev, OrdersSnapshot):
            return self._on_orders(ev)
        if isinstance(ev, TradebookSnapshot):
            return self._on_tradebook(ev)
        if isinstance(ev, MarginsResult):
            return self._on_margins(ev)
        if isinstance(ev, SquareoffsResult):
            return self._on_squareoffs(ev)
        if isinstance(ev, QuoteResult):
            return self._on_quote_result(ev)
        if isinstance(ev, IntentReceived):
            return self._on_intent(ev.intent)
        if isinstance(ev, BarsClosed):
            for b in ev.bars:
                self.store.put_bar(b)
            return []
        if isinstance(ev, FeedState):
            return self._on_feed_state(ev)
        if isinstance(ev, PrepResult):
            return self._on_prep(ev)
        if isinstance(ev, HistoricalResult):
            return self._on_historical(ev)
        if isinstance(ev, ContractNoteResult):
            return self._on_contract_note(ev)
        if isinstance(ev, RecoveryStart):
            return self._on_recovery_start()
        if isinstance(ev, SelfcheckResult):
            return self._on_selfcheck(ev)
        if isinstance(ev, Shutdown):
            return self._on_shutdown(ev)
        raise TypeError(f"the engine has no handler for {type(ev).__name__}")

    # ------------------------------------------------------------------------------------------------------------
    # Small services
    # ------------------------------------------------------------------------------------------------------------
    @property
    def gateway(self) -> Any:
        """The context's gateway (a test double), else titan2.core.gateway as imported with this module."""
        return gateway_module if self._gateway is None else self._gateway

    @property
    def exit_loop(self) -> Any:
        """The context's exit loop (a test double), else titan2.core.exit_loop as imported with this module."""
        return exit_loop_module if self._exit_loop is None else self._exit_loop

    @property
    def instruments(self) -> Any:
        """Today's InstrumentStore, or None (never a previous day's)."""
        inst = self.ctx.instruments()
        return inst if inst is not None and getattr(inst, "session", None) == self.session else None

    @property
    def instruments_loaded(self) -> bool:
        return self.ctx.prep.get("INSTRUMENTS") is True and self.instruments is not None

    @property
    def ban_list(self) -> frozenset[str] | None:
        return self.ctx.ban_list()

    @property
    def token_ok(self) -> bool:
        return not self.halted("TOKEN")

    @core_only
    def event(self, kind: str, payload: dict | None = None, severity: str = "INFO", **refs: Any) -> int:
        """One events row in the current transaction (§4.6)."""
        return self.store.append_event(kind, wall=self.now_wall, mono=self.now_mono, payload=payload or {},
                                       severity=severity, session=self.session, **refs)

    def halted(self, kind: str, scope: str = "GLOBAL") -> bool:
        return any(h.kind == kind and h.scope == scope for h in self.state.halts)

    def active_halts(self, kind: str | None = None, scope: str | None = None) -> list[Halt]:
        return [h for h in self.state.halts if (kind is None or h.kind == kind) and (scope is None or h.scope == scope)]

    @core_only
    def add_halt(self, kind: str, scope: str, reason: str, *, clears: HaltClears | str | None = None,
                 operator: bool = False, alert: bool = True) -> list[Command]:
        """Raise a sticky halt (one active row per (scope, kind)); the HALT event and, with alert, an Alert."""
        rule = risk.HALT_RULES.get(kind)
        clears = HaltClears(clears) if clears is not None else (rule.clears if rule is not None else HaltClears.ACK)
        h, new = self.store.add_halt(scope, kind, str(reason)[:500], clears, self.now_wall, session=self.session,
                                     operator=operator)
        if not new:
            return []
        self.state.halts = tuple(self.state.halts) + (h,)
        severity = "CRITICAL" if kind in CRITICAL_HALTS else "WARN"
        self.event("HALT", {"halt_id": h.halt_id, "kind": kind, "scope": scope, "reason": h.reason,
                            "clears": str(clears), "operator": operator}, severity)
        if operator:
            self._operator_impaired()
        if not alert:
            return []
        return [Alert(severity, f"HALT_{kind}", f"HALT {kind} [{scope}]: {h.reason}"[:900], f"halt:{kind}:{scope}")]

    @core_only
    def apply_halt(self, req: HaltRequest, *, alert: bool = True) -> list[Command]:
        return self.add_halt(req.kind, req.scope, req.reason, clears=req.clears, alert=alert)

    @core_only
    def clear_halt(self, h: Halt, by: str, note: str | None = None) -> list[Command]:
        if self.store.clear_halt(h.halt_id, self.now_wall, by, note):
            self.state.halts = tuple(x for x in self.state.halts if x.halt_id != h.halt_id)
            self.event("HALT_CLEARED", {"halt_id": h.halt_id, "kind": h.kind, "scope": h.scope, "by": by,
                                        "note": note})
        return []

    def _clear_auto(self, kind: str, scope: str, why: str) -> None:
        for h in self.active_halts(kind, scope):
            if risk.can_clear(h, "AUTO", today=self.session, condition_met=True)[0]:
                self.clear_halt(h, "AUTO", why)

    def profile(self, strategy: str) -> ExitProfile:
        return self.ctx.exit_profiles.get(strategy) or ExitProfile()

    @core_only
    def flag_impaired(self, strategy: str, detail: str) -> None:
        self._flag_session(self.session, strategy, detail)

    def _flag_session(self, day: date, strategy: str, detail: str) -> None:
        """Mark one of the strategy's sessions IMPAIRED for G1 (§10.7); idempotent (the first detail is kept)."""
        if self.store.flag_session(day, strategy, detail[:300]):
            payload = {"strategy": strategy, "detail": detail[:300]}
            if day != self.session:
                payload["session"] = day.isoformat()
            self.event("SESSION_IMPAIRED", payload, "WARN", strategy=strategy)

    def reload_modes(self) -> None:
        self.state.modes = {r["strategy"]: r for r in self.store.strategy_modes()}

    def _g1_strategies(self) -> list[str]:
        """Strategies whose sessions count for G1 (mode PAPER or LIVE)."""
        return sorted(s for s, r in self.state.modes.items() if r["mode"] in ("PAPER", "LIVE"))

    def _operator_impaired(self) -> None:
        """§8.9/§10.7: an operator halt during the session marks the G1 strategies' session IMPAIRED. The laptop's
        Ctrl+C FLATTEN halt (operator.is_stop_flatten) counts only while something is open: with nothing open it only
        bridges the second until the process stops, and a stop is the heartbeat gap's to judge (G1_IMPAIRED_GAP_S)."""
        flat = not self.state.live_structures()
        kinds = sorted({h.kind for h in self.state.halts
                        if risk.is_operator_halt(h) and not (flat and operator.is_stop_flatten(self, h))})
        if not kinds or not self.calendar.is_trading_day(self.session) or self.now_wall >= self.eod.flatten:
            return
        for s in self._g1_strategies():
            self.flag_impaired(s, f"operator halt {','.join(kinds)}")

    # ------------------------------------------------------------------------------------------------------------
    # Services for the gateway, the exit loop and the operator
    # ------------------------------------------------------------------------------------------------------------
    @core_only
    def reserve(self, intent: Intent, *, book: str, account: Account | str, policy: Policy | str,
                is_placebo: bool | None = None, parent_structure_id: int | None = None,
                blocked_by: str | None = None) -> Structure:
        st = positions.reserve(self.store, intent, book=book, account=account, policy=policy,
                               now_mono=self.now_mono, now_wall=self.now_wall, is_placebo=is_placebo,
                               parent_structure_id=parent_structure_id, blocked_by=blocked_by)
        self.state.structures[st.structure_id] = st
        return st

    @core_only
    def void(self, st: Structure, reason: str) -> list[Command]:
        legs = self.state.legs_of(st)
        positions.void(self.store, st, legs, reason, now_mono=self.now_mono, now_wall=self.now_wall)
        out: list[Command] = []
        for leg in legs:
            out += self._release_token(leg)
        return out

    @core_only
    def begin_entry(self, st: Structure, *, inst: Instrument, qty: int, limit_px: float,
                    decision_quote: DecisionQuote, decision: dict | None,
                    role: Role | str = Role.ENTRY) -> tuple[Leg, Order, list[Command]]:
        leg, o, fx = positions.begin_entry(self.store, self.state.orders, st, inst=inst, qty=qty, limit_px=limit_px,
                                           decision_quote=decision_quote, decision=decision, now_mono=self.now_mono,
                                           now_wall=self.now_wall, role=role,
                                           live_allowed=self.live_allowed(st, Role(role)))
        self.state.legs[leg.leg_id] = leg
        self.state.timers[o.order_pk] = OrderTimers()
        return leg, o, list(fx.commands)

    @core_only
    def create_order(self, leg: Leg, role: Role | str, qty: int, limit_px: float, *,
                     decision: DecisionQuote | None = None, band_idx: int | None = None) -> tuple[Order, list[Command]]:
        st = self.state.structures[leg.structure_id]
        role = Role(role)
        o, fx = orders.create_order(self.store, self.state.orders, leg, role=role, qty=qty, limit_px=limit_px,
                                    now_mono=self.now_mono, now_wall=self.now_wall, decision=decision,
                                    band_idx=band_idx, live_allowed=self.live_allowed(st, role))
        self.state.timers[o.order_pk] = OrderTimers()
        return o, list(fx.commands)

    @core_only
    def request_cancel(self, o: Order, reason: str) -> list[Command]:
        leg = self.state.legs.get(o.leg_id)
        if leg is None or o.is_terminal:
            return []
        fx = orders.request_cancel(self.store, o, self._timers(o), self.now_mono, self.now_wall, reason=reason)
        return self._order_effects(o, leg, fx)

    def live_allowed(self, st: Structure, role: Role) -> bool:
        """I14: a LIVE ENTRY needs ALLOW_LIVE, mode LIVE with a valid unlock, today's self-test and no demotion; a
        LIVE SELFTEST needs ALLOW_LIVE and the self-test book. Other orders are not restricted here."""
        role = Role(role)
        if Account(st.account) is not Account.LIVE or role not in (Role.ENTRY, Role.SELFTEST):
            return True
        if self.ctx.allow_live is not True:
            return False
        if role is Role.SELFTEST:
            return st.book == SELFTEST_BOOK
        lp = self.live_preconditions(st.strategy)
        return (st.book == f"LIVE:{st.strategy}" and lp.unlocked and lp.selftest_passed and not lp.demoted)

    def live_preconditions(self, strategy: str) -> LivePreconditions:
        """The two keys and the daily self-test for one strategy (§10.8): G13's input."""
        row = self.state.modes.get(strategy)
        trial = self.store.trial(strategy, row["version"]) if row is not None else None
        unlocked = (row is not None and row["mode"] == "LIVE" and row["live_unlock_report_id"] is not None
                    and trial is not None and trial["state"] == "CERTIFIED")
        demoted = row is None or (trial is not None and trial["state"] == "DEMOTED")
        n = self.store.live_sessions(strategy)
        today = any(s.book == f"LIVE:{strategy}" and s.session == self.session for s in self.state.structures.values())
        first_week = n + (0 if today else 1) <= self.settings.LIVE_FIRST_WEEK_SESSIONS
        return LivePreconditions(allow_live=self.ctx.allow_live is True, unlocked=bool(unlocked),
                                 selftest_passed=self.state.selftest_passed, demoted=bool(demoted),
                                 first_week=first_week)

    def forced_reason(self, leg: Leg) -> str | None:
        """decide() rule 0: a pre-set reason (an anomaly found while the leg was entering) or a KILL / FLATTEN /
        DAILY_STOP halt that covers the leg."""
        if leg.exit_reason and leg.state in (LegState.ENTERING, LegState.OPEN):
            return leg.exit_reason
        kinds = {h.kind for h in self.state.halts if h.scope == "GLOBAL"}
        if "KILL" in kinds:
            return "KILL_SWITCH"
        if "FLATTEN" in kinds:
            return "OPERATOR_FLATTEN"
        live = Account(leg.account) is Account.LIVE
        for h in self.state.halts:
            if h.kind == "DAILY_STOP" and (h.scope == f"BOOK:{leg.book}" or (live and h.scope == "ACCOUNT:LIVE")):
                return "DAILY_STOP"
        return None

    @core_only
    def force_exit(self, leg: Leg, reason: str) -> list[Command]:
        """Move an OPEN leg to EXITING(reason) (an EXITING one only escalates to PROTECTIVE) and step its exit."""
        st = self.state.structures.get(leg.structure_id)
        if st is None or leg.state not in (LegState.OPEN, LegState.EXITING):
            return []
        positions.begin_exit(self.store, st, leg, reason, Urgency.PROTECTIVE, now_mono=self.now_mono,
                             now_wall=self.now_wall)
        return self._exit_step(leg)

    @core_only
    def close(self, st: Structure, residual_qty: int = 0) -> list[Command]:
        """VERIFY_FLAT -> CLOSED (positions.close books the trade), then the post-close duties."""
        row, fx = positions.close(self.store, st, self.state.legs_of(st), now_mono=self.now_mono,
                                  now_wall=self.now_wall, residual_qty=residual_qty, mode=self._trade_mode(st))
        return list(fx.commands) + self._after_close(st, row)

    @core_only
    def close_external(self, st: Structure, leg: Leg, ext: ExternalClose) -> list[Command]:
        row, fx = positions.close_external(self.store, self.state.orders, st, leg, ext, now_mono=self.now_mono,
                                           now_wall=self.now_wall, mode=self._trade_mode(st))
        out = list(fx.commands)
        for h in fx.halts:
            out += self.apply_halt(h, alert=False)       # recon already raised and alerted the MISMATCH
        return out + self._after_close(st, row)

    def request_quotes(self, symbols: Iterable[str], priority: int) -> list[Command]:
        """One FetchQuotes for the symbols without a refresh in flight (their RefreshState marks it)."""
        new = [s for s in dict.fromkeys(symbols) if not self.state.refresh.get(s, exits.NO_REFRESH).in_flight]
        if not new:
            return []
        rid = self._rid("q")
        for s in new:
            prev = self.state.refresh.get(s, exits.NO_REFRESH)
            self.state.refresh[s] = exits.RefreshState(True, prev.last_ok_mono, prev.last_fail_mono)
        self.state.quote_requests[rid] = (tuple(new), self.now_mono)
        return [FetchQuotes(rid, tuple(new), int(priority))]

    def request_positions(self, account: Account, priority: int = Priority.EXIT) -> list[Command]:
        acct = Account(account)
        if self.state.broker[acct].pos_pending or ("P", acct) in self._asked:
            return []
        self._asked.add(("P", acct))
        return [FetchPositions(acct, int(priority))]

    def request_orders(self, account: Account, priority: int = Priority.EXIT) -> list[Command]:
        acct = Account(account)
        if self.state.broker[acct].ords_pending or ("O", acct) in self._asked:
            return []
        self._asked.add(("O", acct))
        return [FetchOrders(acct, int(priority))]

    def start_recon(self, account: Account, priority: int = Priority.RECON) -> list[Command]:
        """A reconcile round: both reads, requested now; reconcile() runs when both answers are in."""
        acct = Account(account)
        bv = self.state.broker[acct]
        bv.round_start, bv.round_pos, bv.round_ords = self.now_mono, None, None
        bv.last_round_mono = self.now_mono
        return [FetchPositions(acct, int(priority)), FetchOrders(acct, int(priority))]

    def bot_view(self, account: Account) -> recon.BotView:
        acct = Account(account)
        legs = tuple(lg for _, lg in sorted(self.state.legs.items())
                     if Account(lg.account) is acct and (not lg.is_terminal or lg.session == self.session))
        acks = {(r["symbol"], r["product"]): int(r["qty"]) for r in self.store.foreign_acks(self.session, acct)}
        return recon.BotView(acct, self.session, self.settings.BROKER_SQUAREOFF_AT, legs, acks,
                             dict(self.state.last_rb), start_mono=self.ctx.start_mono)

    def symbol_view(self, account: Account, symbol: str) -> recon.SymbolView:
        return recon.symbol_view(self.bot_view(account), symbol)

    def recon_clean(self, scope: str) -> bool:
        """MISMATCH / UNKNOWN_POSITION / SHORT_DETECTED clear only while recon is CLEAN (recently) for the scope."""
        kind, _, value = scope.partition(":")
        if kind == "ACCOUNT":
            accts = [Account(value)] if value in {a.value for a in Account} else []
        elif kind == "GLOBAL":
            accts = list(ACCOUNTS)
        else:
            return True
        max_age = self.settings.RECON_MAX_AGE_ENTRY_S
        return bool(accts) and all(self.state.broker[a].recon.status == ReconClass.CLEAN.value
                                   and self.state.broker[a].recon.clean_within(self.now_mono, max_age)
                                   for a in accts)

    def mark_pub(self, name: str) -> None:
        self._pub.add(name)

    def _timers(self, o: Order) -> OrderTimers:
        return self.state.timers.setdefault(o.order_pk, OrderTimers())

    def _trade_mode(self, st: Structure) -> str | None:
        row = self.state.modes.get(st.strategy)
        return row["mode"] if row is not None else None

    # ------------------------------------------------------------------------------------------------------------
    # Books, MTM and the gate context
    # ------------------------------------------------------------------------------------------------------------
    def leg_mtm(self, leg: Leg) -> float | None:
        """mtm_net at the realizable bid of a quote <= STALE_REFRESH_S old; None when unknown (§8.2)."""
        done = []
        for o in leg.leg_orders():
            if o.filled_qty <= 0:
                continue
            if not _pos(o.avg_fill_px):
                return None
            done.append(OrderFills(Side(o.side), o.filled_qty, o.filled_qty * o.avg_fill_px, o.external_kind))
        open_qty = leg.open_qty
        if open_qty < 0:
            return None
        rb = age = None
        q = self.quotes.last(leg.inst.symbol)
        if open_qty > 0 and q is not None:
            rb, _ = exits.realizable_bid(q, open_qty)
            age = exits.receipt_age(q, self.now_mono)
        return risk.leg_mtm(done, open_qty, rb, age, self.rates, self.settings.STALE_REFRESH_S)

    def _exposure(self, st: Structure) -> float | None:
        """G20: premium + entry charges of one structure (a working entry counts at qty x limit); None: no plan."""
        value = charges = 0.0
        planned = False
        for leg in self.state.legs_of(st):
            for o in leg.leg_orders():
                if o.role not in (Role.ENTRY, Role.SELFTEST):
                    continue
                if not o.is_terminal and _pos(o.limit_px):
                    qty, v = o.qty, o.qty * o.limit_px
                elif o.filled_qty > 0:
                    px = o.avg_fill_px if _pos(o.avg_fill_px) else o.limit_px
                    if not _pos(px):
                        continue
                    qty, v = o.filled_qty, o.filled_qty * px
                else:
                    continue
                planned = True
                value += v
                charges += order_charges(OrderFills(Side.BUY, qty, v), self.rates).total
        return round(value + charges, 2) if planned else None

    def _open_risk(self, sts: list[Structure], mtm_of: dict[int, float | None]) -> float | None:
        """size()'s open_risk_rs for one book (§8.5 DAY_BUDGET; review round 3, it was never passed): the stop-outs of
        its structures past RESERVED measured from their entries, each sizing.remaining_stop_risk(its plan's per-lot
        risk x lots, its legs' MTM now, what its stop has been raised since: _stop_raise). An open gain is neither
        credited nor counted as still to lose (review round 3 follow-up: it was counted twice). An entry still working
        counts in full. None when one has no sizing plan (a recovered position, the self-test) or an unknown MTM:
        size() then refuses the entry (fails closed)."""
        total = 0.0
        for x in sts:
            if x.state is StructState.RESERVED:
                continue                          # not planned yet: the one being sized now, or one sized after it
            plan = x.decision.get("plan") if isinstance(x.decision, Mapping) else None
            try:
                at_entry = float(plan["per_lot_risk_rs"]) * int(plan["lots"])
            except (KeyError, TypeError, ValueError):
                return None
            vals = [mtm_of[leg.leg_id] for leg in self.state.legs_of(x) if leg.leg_id in mtm_of]
            part = sizing.remaining_stop_risk(at_entry, None if None in vals else math.fsum(vals), self._stop_raise(x))
            if part is None:
                return None
            total += part
        return round(total, 2)

    def _stop_raise(self, st: Structure) -> float:
        """How far a lock or a trail has raised the structure's stop above its hard stop, in rupees over the open qty:
        Σ open qty x (stop_px - hard_sl_px). 0 before the entry fill (no frozen levels yet) or while no stop moved."""
        p = st.exit_params
        if p is None or not _pos(p.hard_sl_px):
            return 0.0
        total = 0.0
        for leg in self.state.legs_of(st):
            stop, qty = leg.stop_px, leg.open_qty
            if _pos(stop) and stop > p.hard_sl_px and qty > 0:
                total += qty * (stop - p.hard_sl_px)
        return total

    def book_state(self, book: str) -> BookState:
        c = self._counters.get(book)
        if c is None:
            c = self._counters[book] = self.store.book_counters(book, self.session, self.now_wall)
        s = self.settings
        sts = [x for x in self.state.live_structures() if x.book == book]
        mtm_of = {leg.leg_id: self.leg_mtm(leg) for x in sts for leg in self.state.legs_of(x) if leg.entry_filled > 0}
        mtms = list(mtm_of.values())
        return BookState(
            book=book, policy=risk.book_policy(book) or Policy.FULL,
            open=sum(1 for x in sts if x.state is not StructState.RESERVED),
            reserved=sum(1 for x in sts if x.state is StructState.RESERVED),
            entries=c.entries, attempts=c.attempts, orders_today=c.orders_today,
            entry_orders_60s=c.entry_orders_60s, realized_net=c.realized_net, mtm_net=risk.book_mtm(mtms),
            first_loss_today=c.first_loss_today,
            cooldown_until=self.store.cooldowns(book, self.session, reentry_min=s.REENTRY_COOLDOWN_MIN,
                                                reject_min=s.REJECT_COOLDOWN_MIN, void_min=s.VOID_COOLDOWN_MIN),
            active_underlyings=frozenset(x.underlying for x in sts),
            exposures_rs=tuple(e for x in sts if (e := self._exposure(x)) is not None),
            pending=sum(1 for x in sts if x.session == self.session and x.opened_wall is None
                        and x.state in (StructState.RESERVED, StructState.ENTERING)),
            open_risk_rs=self._open_risk(sts, mtm_of))

    def _live_books(self, extra: str | None = None) -> list[str]:
        books = {x.book for x in self.state.live_structures() if Account(x.account) is Account.LIVE}
        books |= {x.book for x in self.state.structures.values()
                  if Account(x.account) is Account.LIVE and x.session == self.session}
        if extra is not None:
            books.add(extra)
        return sorted(books)

    def gate_context(self, book: str) -> GateContext:
        """The frozen snapshot G01-G22 read for one book (§8.6). Until recovery and two CLEAN recons per account
        are done (§9.6) the recon map is empty, so G07 refuses every entry."""
        acct = risk.book_account(book)
        parsed = risk.parse_book(book)
        live = acct is Account.LIVE
        recon_map = {a: bv.recon for a, bv in self.state.broker.items()} if self.state.entries_resumed else {}
        return GateContext(
            now_mono=self.now_mono, now_wall=self.now_wall, session=self.session, settings=self.settings,
            calendar=self.calendar, clock_synced=not self.halted("CLOCK_UNSYNCED"),
            instruments_loaded=self.instruments_loaded, rates=self.rates, instruments=self.instruments,
            ban_list=self.ban_list, blocked_symbols=frozenset(h.scope[7:] for h in self.state.halts
                                                              if h.kind == "CONTRACT_BLOCKED"
                                                              and h.scope.startswith("SYMBOL:")),
            feed=self.ctx.feed_health(), signals_degraded=self.ctx.signals_degraded(), recon=recon_map,
            margins={a: bv.margins for a, bv in self.state.broker.items() if bv.margins is not None},
            halts=tuple(self.state.halts),
            strategy_modes={s: Mode(r["mode"]) for s, r in self.state.modes.items()},
            running_versions=dict(self.ctx.versions), disabled_strategies=frozenset(self.ctx.disabled),
            structure_types=frozenset(self.handlers), book=self.book_state(book),
            live_account=(risk.live_account_state([self.book_state(b) for b in self._live_books(book)])
                          if live else None),
            live=self.live_preconditions(parsed[1]) if live and parsed is not None else None)

    # ------------------------------------------------------------------------------------------------------------
    # Startup and the session rollover
    # ------------------------------------------------------------------------------------------------------------
    def _startup(self) -> list[Command]:
        """First event of this engine instance (idempotent: it also runs after every reload)."""
        out: list[Command] = []
        problems = invariants.settings_problems(self.settings)                       # I16
        if problems:
            out += self.add_halt("INVARIANT", "GLOBAL", f"I16 settings outside LIMITS: {'; '.join(problems[:3])}")
        if self.ctx.settings_errors:
            out += self.add_halt("SETTINGS_INVALID", "GLOBAL",
                                 "settings file invalid, running on code defaults: "
                                 + "; ".join(self.ctx.settings_errors[:3]))
        else:
            self._clear_auto("SETTINGS_INVALID", "GLOBAL", "valid settings at start")
        if self.ctx.safe_mode:
            out += self.add_halt("SAFE_FLAG", "GLOBAL", "bot --safe / TITAN2_SAFE=1: exits only", operator=True)
        if self.store.get_meta("last_session") != self.session.isoformat():
            out += self._rollover()
        out += self._ensure_modes()
        self._operator_impaired()
        return out

    def _new_session(self) -> list[Command]:
        """The first Timer of a later session while the process runs."""
        self._set_session(session_of(self.now_wall))
        st = self.state
        st.selftest_passed, st.selftest_attempts, st.selftest_run = False, 0, None
        st.eod_done, st.eod_ran, st.known_violations = set(), {}, set()
        st.entries_resumed = False
        self.ctx.prep.clear()
        return self._rollover() + self._ensure_modes()

    def _rollover(self) -> list[Command]:
        """Clear the NEXT_SESSION halts of earlier sessions and apply pending mode changes (§8.7, §10.5)."""
        for h in risk.rollover_clears(self.state.halts, self.session):
            self.clear_halt(h, "NEXT_SESSION", f"session {self.session}")
        for name, row in sorted(self.state.modes.items()):
            new = row["pending_mode"]
            if not new:
                continue
            since = row["live_since_session"]
            if new == "LIVE" and row["mode"] != "LIVE":
                since = self.session
            self.store.update_row("strategy_modes", {"strategy": name},
                                  {"mode": new, "pending_mode": None, "live_since_session": since,
                                   "changed_ts": self.now_wall, "changed_by": "session"})
            self.event("MODE_CHANGED", {"version": row["version"], "from": row["mode"], "to": new, "by": "session"},
                       strategy=name)
        self.reload_modes()
        self.store.set_meta("last_session", self.session.isoformat())
        return []

    def _ensure_modes(self) -> list[Command]:
        """Every registered strategy has a strategy_modes row (both start in PAPER) carrying the running version;
        a new version of a LIVE strategy drops to PAPER (LIVE needs a fresh G1); PAPER/LIVE versions get a trial."""
        out: list[Command] = []
        for name, version in sorted(self.ctx.versions.items()):
            row = self.state.modes.get(name)
            if row is None:
                self.store.put_strategy_mode(name, version=version, mode="PAPER", changed_wall=self.now_wall,
                                             changed_by="engine")
                self.event("MODE_CHANGED", {"version": version, "from": None, "to": "PAPER", "by": "engine"},
                           strategy=name)
            elif row["version"] != version:
                live = row["mode"] == "LIVE" or row["pending_mode"] == "LIVE"
                values: dict[str, Any] = {"version": version, "changed_ts": self.now_wall, "changed_by": "engine"}
                if live:
                    values.update(mode="PAPER" if row["mode"] == "LIVE" else row["mode"], pending_mode=None,
                                  demoted_reason="VERSION_CHANGED")
                    out.append(Alert("CRITICAL", "LIVE_DEMOTED", f"{name}: new version {version} replaces "
                                     f"{row['version']}; LIVE needs a fresh G1 on it: mode PAPER",
                                     f"version_live:{name}"))
                self.store.update_row("strategy_modes", {"strategy": name}, values)
                self.event("MODE_CHANGED", {"from_version": row["version"], "version": version,
                                            "mode": values.get("mode", row["mode"]), "by": "engine"},
                           "CRITICAL" if live else "INFO", strategy=name)
        self.reload_modes()
        for name in self._g1_strategies():
            row = self.state.modes[name]
            g1.ensure_trial(self.store, name, row["version"], dict(self.ctx.version_inputs.get(name, {})),
                            self.now_wall, self.now_mono)
        return out

    # ------------------------------------------------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------------------------------------------------
    def _on_order_event(self, ev: Event) -> list[Command]:
        if isinstance(ev, OrderSnapshot):
            # a report of a NOT_PLACED attempt whose late fill was adopted goes to the adopted row (orders.find_order)
            o = orders.find_order(self.state.orders, ev.account, tag=ev.tag, broker_order_id=ev.broker_order_id)
        else:
            o = self.state.orders.get(ev.order_pk)
            if o is not None and Account(o.account) is not Account(ev.account):
                o = None
        if o is None or o.leg_id not in self.state.legs:
            # not a bot order (recon decides what it is) or an answer to a request this engine never made (§3.3)
            log.debug("order event for an unknown order: %r", ev)
            return []
        leg = self.state.legs[o.leg_id]
        self._selftest_note(o, ev)
        fx = orders.handle(self.store, o, leg, ev, self._timers(o), self.now_mono, self.now_wall,
                           token_ok=self.token_ok)
        out = self._order_effects(o, leg, fx)
        out += self._adopt_late_fill(o, self._reported(o, ev))
        return out + self._selftest_verify()

    # -- a bot order given up as NOT_PLACED that the broker took after all -------------------------------------------
    def _reported(self, o: Order, ev: Event) -> OrderSnapshot | None:
        """What the broker says about `o` in this event: a snapshot, a lookup's find, or a PlaceResult OK."""
        if isinstance(ev, OrderSnapshot):
            return ev
        if isinstance(ev, LookupResult) and ev.ok and ev.found is not None:
            return snapshot_of(ev.found, "LOOKUP", self.now_mono)
        if isinstance(ev, PlaceResult) and ev.outcome == "OK" and ev.broker_order_id:
            return OrderSnapshot(ev.account, ev.broker_order_id, o.order_pk, "WORKING", 0, None, None, None, "POLL",
                                 self.now_mono)
        return None

    @core_only
    def _adopt_late_fill(self, o: Order, snap: OrderSnapshot | None) -> list[Command]:
        """A bot BUY (ENTRY/SELFTEST) the order machine concluded NOT_PLACED that the broker reports taken after all:
        a timed-out place Kite's OMS registered after the last tag lookup (§5.2). Its row is immutable (I9) and its
        structure VOID, so the broker order is adopted, under a new tag-shaped order_pk holding its broker id (later
        reports reach it through orders.find_order / adopted_report), as the entry of a new RECOVERED:<account>
        structure (strategy _recovered) whose leg exits as soon as it opens (ANOMALY_EXIT, protective): never an
        unmanaged long. CRITICAL LATE_FILL (the alert and a LATE_FILL event). A late EXIT SELL is not adopted: recon
        books its real fills (a lost order closing qty, §9.3) and covers a bot-explained short."""
        if (snap is None or not self.ctx.recovered or o.state is not OrderState.NOT_PLACED
                or Role(o.role) not in (Role.ENTRY, Role.SELFTEST) or not snap.broker_order_id
                or snap.tag != o.order_pk or o.leg_id not in self.state.legs):
            return []
        bid = snap.broker_order_id
        taken = snap.status == "WORKING" or (snap.status in ("COMPLETE", "CANCELLED") and snap.filled_qty > 0)
        if not taken or any(x.broker_order_id == bid for x in self.state.orders.values()):
            return []
        acct, inst = Account(o.account), self.state.legs[o.leg_id].inst
        book = f"RECOVERED:{acct.value}"
        text = (f"{o.book} {inst.symbol} {o.role} {o.order_pk} was given up as NOT_PLACED but the broker took it "
                f"({bid}: {snap.status}, filled {snap.filled_qty})")
        refs = {"account": acct, "book": o.book, "structure_id": o.structure_id, "leg_id": o.leg_id,
                "order_pk": o.order_pk}
        payload = {"broker_order_id": bid, "status": snap.status, "filled": snap.filled_qty, "source": snap.source}
        blocker = next((s for s in self.state.live_structures() if s.underlying == inst.underlying
                        and (s.book == book or (acct is Account.LIVE and Account(s.account) is Account.LIVE))), None)
        if blocker is not None:                          # I5: one structure per (book | LIVE account, underlying)
            if o.order_pk in self._late_blocked:
                return []
            self._late_blocked.add(o.order_pk)
            self.event("LATE_FILL", {**payload, "adopted": None, "blocked_by": blocker.structure_id}, "CRITICAL",
                       **refs)
            return [Alert("CRITICAL", "LATE_FILL", f"{text}; structure {blocker.structure_id} holds "
                          f"{inst.underlying}, so it cannot be adopted: close it in Kite", f"late_fill:{o.order_pk}")]
        ce = inst.opt_type == "CE"
        intent = Intent(intent_id=f"rec{uuid.uuid4().hex[:9]}", strategy=recon.RECOVERED_STRATEGY,
                        strategy_version="0", underlying=inst.underlying,
                        direction=Direction.UP if ce else Direction.DOWN,
                        structure=StructureType.LONG_CE if ce else StructureType.LONG_PE,
                        strike_pref=StrikePref.ATM, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=inst.strike,
                        recheck=Recheck(-math.inf, math.inf), ttl_s=10.0, created_mono=self.now_mono,
                        created_wall=self.now_wall, reason={LATE_FILL_OF: o.order_pk, "broker_order_id": bid})
        self.store.insert_intent(intent, self.session)
        st = self.reserve(intent, book=book, account=acct, policy=Policy.FULL)
        role = Role(o.role)
        # decide() rule 0 exits an ENTRY as soon as it opens; a filled SELFTEST exits as SELFTEST_FILLED by itself
        leg = Leg(leg_id=0, structure_id=st.structure_id, leg_no=1, account=acct, book=book, session=self.session,
                  seq_no=self.store.next_leg_seq(acct, self.session), inst=inst, side="LONG",
                  state=LegState.ENTERING, exit_reason="ANOMALY_EXIT" if role is Role.ENTRY else None)
        self.store.insert_leg(leg)
        leg.bind_orders(self.state.orders)
        st.legs.append(leg.leg_id)
        st.state = StructState.ENTERING
        self.store.update_structure(st)
        self.state.legs[leg.leg_id] = leg
        x = Order(order_pk=make_tag(role, acct, self.session, leg.seq_no, 1), leg_id=leg.leg_id,
                  structure_id=st.structure_id, account=acct, book=book, role=role, side=ROLE_SIDE[role],
                  symbol=inst.symbol, qty=o.qty, limit_px=o.limit_px, attempt=1, band_idx=None,
                  state=OrderState.SUBMITTING, decision=o.decision, created_wall=whole_ms(self.now_wall),
                  submitted_mono=self.now_mono)
        self.store.insert_order(x, submitted_wall=self.now_wall)
        self.state.orders[x.order_pk] = x
        self._imported.add(x.order_pk)
        leg.orders.append(x.order_pk)
        self.event("LATE_FILL", {**payload, "adopted": x.order_pk, "into_structure": st.structure_id}, "CRITICAL",
                   **refs)
        self.event("ORDER_CREATED", {LATE_FILL_OF: o.order_pk, "broker_order_id": bid, "status": snap.status,
                                     "qty": x.qty, "filled": snap.filled_qty}, "CRITICAL", account=acct, book=book,
                   structure_id=st.structure_id, leg_id=leg.leg_id, order_pk=x.order_pk)
        out: list[Command] = [Alert("CRITICAL", "LATE_FILL", f"{text}: adopted as {x.order_pk} into {book} "
                                    f"structure {st.structure_id}, which exits at once", f"late_fill:{o.order_pk}")]
        # the given-up structure's VOID released the token (_release_token): the adopted leg needs it back in full
        #   mode, as begin_entry asks for an entry, or it exits on REST quotes only and its post-exit tail stays empty
        out.append(Subscribe((inst.token,), "full"))
        fx = orders.on_snapshot(self.store, x, leg, dataclasses.replace(snap, tag=x.order_pk, source="RECOVERY"),
                                self._timers(x), self.now_mono, self.now_wall)
        return out + self._order_effects(x, leg, fx)

    def _keep_late_window(self, o: Order) -> None:
        """Remember the order's late-accept deadline for the rest of the process (EngineContext.late_until): a
        rollback reload ends the window there, not at one rebuilt from the submit (review round 3). It is kept
        once the order resolves: a reload after a rolled-back resolution still needs it."""
        t = self.state.timers.get(o.order_pk)
        if t is not None and t.late_until is not None:
            self.ctx.late_until[o.order_pk] = t.late_until

    def _order_effects(self, o: Order, leg: Leg, fx: orders.Effects) -> list[Command]:
        self._keep_late_window(o)
        out = list(fx.commands)
        for h in fx.halts:
            out += self.apply_halt(h, alert=False)       # the order machine sends its own alerts
        st = self.state.structures[leg.structure_id]
        if fx.unresolved:
            positions.mark_unresolved(self.store, st, now_mono=self.now_mono, now_wall=self.now_wall,
                                      detail=f"order {o.order_pk}")
        if fx.became_terminal:
            out += self._order_terminal(o, leg, st)
        return out

    def _order_terminal(self, o: Order, leg: Leg, st: Structure) -> list[Command]:
        role = Role(o.role)
        out: list[Command] = []
        if role in (Role.ENTRY, Role.SELFTEST):
            if (leg.state is LegState.ENTERING and st.state is StructState.ENTERING
                    and not self.state.busy(leg)):
                out += self._entry_done(leg, st)
            if role is Role.SELFTEST:
                out += self._selftest_order_done(o)
            return out
        if role in (Role.EXIT, Role.COVER):
            return list(self.exit_loop.on_order_terminal(self, o, leg))
        return out

    def _entry_done(self, leg: Leg, st: Structure) -> list[Command]:
        """ENTERING -> OPEN or VOID from the entry fills (§5.3); a new OPEN leg gets its first evaluation at once (a
        forced flag exits it immediately), and one whose levels fail I8 goes straight to its exit. (A PAPER:<s>
        entry's placebo was opened with it, at the decision: gateway._open_placebo.)"""
        fx = positions.on_entry_terminal(self.store, st, leg, profile=self.profile(st.strategy),
                                         now_mono=self.now_mono, now_wall=self.now_wall)
        out = list(fx.commands)
        for h in fx.halts:
            out += self.apply_halt(h)
        if st.state is StructState.VOID:
            out += self._release_token(leg)
        elif st.state is StructState.OPEN:
            out += self._evaluate(leg)
        elif st.state is StructState.EXITING:
            out += self._exit_step(leg)
        return out

    def _release_token(self, leg: Leg) -> list[Command]:
        if leg.inst.token in self.state.held_tokens(except_leg=leg.leg_id):
            return []
        return [Unsubscribe((leg.inst.token,))]

    def _anomaly_exit(self, leg: Leg) -> list[Command]:
        """Leg-scope invariant: OPEN -> EXITING(ANOMALY_EXIT); EXITING -> PROTECTIVE; ENTERING -> the reason is
        pre-set on the leg (persisted) and decide() rule 0 exits it as soon as it opens."""
        st = self.state.structures.get(leg.structure_id)
        if st is None:
            return []
        if leg.state in (LegState.OPEN, LegState.EXITING):
            return self.force_exit(leg, "ANOMALY_EXIT")
        if leg.state is LegState.ENTERING and not leg.exit_reason:
            leg.exit_reason = "ANOMALY_EXIT"
            self.store.update_leg(leg)
        return []

    # ------------------------------------------------------------------------------------------------------------
    # Exit evaluation of OPEN legs (§7.4-7.5) and the exit loop
    # ------------------------------------------------------------------------------------------------------------
    def _evaluate(self, leg: Leg) -> list[Command]:
        st = self.state.structures.get(leg.structure_id)
        if st is None or leg.state is not LegState.OPEN or st.state is not StructState.OPEN or self.state.stopping:
            return []
        sym = leg.inst.symbol
        q = self.quotes.last(sym)
        before = (leg.stop_px, leg.lock_armed, leg.peak_rb, leg.trough_rb)
        d = self._decide(st, leg, q)
        d.marks.apply_to(leg)
        if d.rb is not None:
            self.state.last_rb[leg.leg_id] = d.rb
        self._evaluation_events(st, leg, d)
        kind = d.reason if isinstance(d, exits.Exit) else type(d).__name__
        self._hold_row(leg, q, d.rb, kind)
        out: list[Command] = []
        if isinstance(d, exits.Exit):
            positions.begin_exit(self.store, st, leg, d.reason, d.urgency, now_mono=self.now_mono,
                                 now_wall=self.now_wall, quote=self._quote_doc(q, d.rb))
            self.state.dirty_marks.discard(leg.leg_id)
            return self._exit_step(leg)
        if isinstance(d, exits.RefreshQuote):
            out += self.request_quotes((sym,), Priority.EXIT)
        if exits.LOCK_ARMED in d.notes:
            self.store.update_leg(leg)
            self.state.dirty_marks.discard(leg.leg_id)
        elif (leg.stop_px, leg.lock_armed, leg.peak_rb, leg.trough_rb) != before:
            self.state.dirty_marks.add(leg.leg_id)
        return out

    def _handler(self, st: Structure | None) -> Any:
        """The structure type's StructureHandler (plugins.load_handlers), or None."""
        if st is None:
            return None
        try:
            return self.handlers.get(StructureType(st.type))
        except ValueError:
            return None

    def _decide(self, st: Structure, leg: Leg, q: Quote | None) -> Any:
        """§14: exit evaluation goes through the structure's handler (phase 1: SingleLegHandler.evaluate is
        exits.decide on the leg's newest quote). Without a usable handler exits.decide runs directly: a missing
        plugin never blocks an exit."""
        forced = self.forced_reason(leg)
        evaluate = getattr(self._handler(st), "evaluate", None)
        if callable(evaluate):
            ctx = HandlerCtx(now_mono=self.now_mono, now_wall=self.now_wall, settings=self.settings,
                             instruments=self.instruments, quotes=self.quotes, legs=self.state.legs,
                             refresh=self.state.refresh, eod=self.eod)
            return evaluate(st, self.quotes, self.now_mono, self.now_wall, forced, ctx)
        return exits.decide(leg, q, self.state.refresh.get(leg.inst.symbol), self.now_mono, self.now_wall,
                            st.exit_params, self.eod, forced)

    def _exit_sequence(self, legs: list[Leg]) -> list[Leg]:
        """§14: a structure's exiting legs are stepped in its handler's exit_sequence (phase 2: shorts first). Only
        the order changes: a leg the sequence leaves out is stepped after the ones it names, never skipped."""
        first: dict[int, int] = {}
        for leg in legs:
            first.setdefault(leg.structure_id, leg.leg_id)
        rank: dict[int, int] = {}
        for sid in first:
            st = self.state.structures.get(sid)
            seq = getattr(self._handler(st), "exit_sequence", None)
            if callable(seq):
                for i, lid in enumerate(seq(st)):
                    rank.setdefault(lid, i)
        return sorted(legs, key=lambda lg: (first[lg.structure_id], rank.get(lg.leg_id, math.inf), lg.leg_id))

    def _evaluation_events(self, st: Structure, leg: Leg, d: Any) -> None:
        notes = frozenset(d.notes)
        seen = self.state.notes_seen.get(leg.leg_id, frozenset())
        self.state.notes_seen[leg.leg_id] = notes
        refs = {"account": leg.account, "book": leg.book, "strategy": st.strategy, "structure_id": st.structure_id,
                "leg_id": leg.leg_id}
        if exits.LOCK_ARMED in notes:
            self.event("LOCK_ARMED", {"rb": _num(d.rb), "stop": _num(leg.stop_px), "peak": _num(leg.peak_rb)},
                       **refs)
        if exits.STOP_MOVED in notes:
            last = self.state.stop_event_mono.get(leg.leg_id)
            if last is None or self.now_mono - last >= STOP_EVENT_EVERY_S:
                self.state.stop_event_mono[leg.leg_id] = self.now_mono
                self.event("STOP_MOVED", {"stop": _num(leg.stop_px), "peak": _num(leg.peak_rb),
                                          "lock_armed": leg.lock_armed}, **refs)
        for note, kind, severity in ((exits.STOP_BREACH_PENDING, "STOP_BREACH_PENDING", "INFO"),
                                     (exits.NO_BID, "NO_BID", "WARN"), (exits.QUOTE_STALE, "QUOTE_STALE", "WARN")):
            if note in notes and note not in seen:
                self.event(kind, {"rb": _num(d.rb), "stop": _num(leg.stop_px), "decision": type(d).__name__},
                           severity, **refs)

    def _hold_row(self, leg: Leg, q: Quote | None, rb: float | None, kind: str) -> None:
        last = self.state.hold_rows.get(leg.leg_id)
        if last is not None and last[1] == kind and self.now_mono - last[0] < HOLD_ROW_EVERY_S:
            return
        self.state.hold_rows[leg.leg_id] = (self.now_mono, kind)
        self.store.add_quote_check(leg.leg_id, self.now_wall, "HOLD", **self._quote_fields(q, rb),
                                   stop_px=_num(leg.stop_px), lock_armed=leg.lock_armed, decision=kind)

    def _quote_fields(self, q: Quote | None, rb: float | None) -> dict[str, Any]:
        if q is None:
            return {"rb": _num(rb)}
        age = exits.receipt_age(q, self.now_mono)
        return {"bid": q.bid, "ask": q.ask, "bid_qty": q.bids[0].qty if q.bids else None,
                "ask_qty": q.asks[0].qty if q.asks else None, "ltp": _num(q.ltp), "rb": _num(rb),
                "age_ms": int(age * 1000) if math.isfinite(age) else None, "src": q.source}

    def _quote_doc(self, q: Quote | None, rb: float | None) -> dict[str, Any]:
        return {k: v for k, v in self._quote_fields(q, rb).items() if k in ("bid", "ask", "rb", "age_ms", "src")}

    def _exit_step(self, leg: Leg) -> list[Command]:
        st = self.state.structures.get(leg.structure_id)
        if (self.state.stopping or st is None or st.is_terminal
                or leg.state not in (LegState.EXITING, LegState.VERIFY_FLAT)):
            return []
        return list(self.exit_loop.step(self, leg))

    def _other_exit_memory(self, stepped: set[int]) -> list[Command]:
        """A cover hangs off any leg (§9.3), also an OPEN one or one whose structure already closed, and runs on the
        exit loop's cadence (DETAIL §5.7 'same loop'): step every other leg that holds exit-loop memory."""
        if self.state.stopping:
            return []
        out: list[Command] = []
        for lid in sorted(self.state.exit_memory):
            leg = self.state.legs.get(lid)
            if lid not in stepped and leg is not None:
                out += list(self.exit_loop.step(self, leg))
        return out

    def _flush_marks(self) -> None:
        if self.state.last_mark_flush is not None and self.now_mono - self.state.last_mark_flush < MARK_FLUSH_S:
            return
        self.state.last_mark_flush = self.now_mono
        for lid in sorted(self.state.dirty_marks):
            leg = self.state.legs.get(lid)
            if leg is not None:
                self.store.update_leg(leg)
        self.state.dirty_marks.clear()

    def _after_close(self, st: Structure, row: dict) -> list[Command]:
        """Post-close duties: pub, the loss-streak halt, the 45-min post-exit tail, the LIVE review (§10.9)."""
        out: list[Command] = []
        self._pub_trades.add(st.structure_id)
        h = risk.loss_streak_halt(st.book, float(row["net"]), self.settings)
        if h is not None:
            out += self.apply_halt(h)
        for leg in self.state.legs_of(st):
            self.state.tails[leg.leg_id] = Tail(leg.inst.symbol, leg.inst.token,
                                                self.now_mono + POST_EXIT_TAIL_MIN * 60.0)
            for d in (self.state.cap_wait, self.state.verify, self.state.exit_memory, self.state.hold_rows,
                      self.state.notes_seen, self.state.stop_event_mono):
                d.pop(leg.leg_id, None)
        if Account(st.account) is Account.LIVE and st.book != SELFTEST_BOOK:
            out += self._live_review()
        return out

    def _live_review(self) -> list[Command]:
        review = g1.live_review(self.store, self.now_wall, self.now_mono, self.settings)
        if review.demotions or review.reports:
            self.reload_modes()
            self._pub.add("g1")
        return list(review.alerts)

    # ------------------------------------------------------------------------------------------------------------
    # Market data, intents, feed
    # ------------------------------------------------------------------------------------------------------------
    def _on_quotes_dirty(self) -> list[Command]:
        return self._on_symbols(self.ctx.take_dirty())

    def _on_symbols(self, symbols: Iterable[str]) -> list[Command]:
        syms = set(symbols)
        out: list[Command] = []
        for leg in self.state.legs_in(LegState.OPEN, LegState.EXITING, LegState.VERIFY_FLAT):
            if leg.inst.symbol not in syms:
                continue
            out += self._evaluate(leg) if leg.state is LegState.OPEN else self._exit_step(leg)
        return out

    def _on_quote_result(self, ev: QuoteResult) -> list[Command]:
        fatal = self._read_fatal("a quote read", ev.error) if not ev.ok else []
        if ev.request_id in self.state.quote_requests:
            return fatal + self._refresh_done(ev.request_id, set(ev.symbols) if ev.ok else set())
        run = self.state.selftest_run
        if run is not None and ev.request_id == run.request_id:
            return fatal + self._selftest_quote(ev)
        out = self.gateway.on_quote_result(self, ev)
        return fatal + ([] if out is None else list(out))

    def _refresh_done(self, rid: str, got: set[str]) -> list[Command]:
        syms, _ = self.state.quote_requests.pop(rid)
        for s in syms:
            prev = self.state.refresh.get(s, exits.NO_REFRESH)
            q = self.quotes.last(s)
            if s in got and q is not None:
                self.state.refresh[s] = exits.RefreshState(False, max(q.recv_mono, prev.last_ok_mono or q.recv_mono),
                                                           prev.last_fail_mono)
            else:
                self.state.refresh[s] = exits.RefreshState(False, prev.last_ok_mono, self.now_mono)
        return self._on_symbols(syms)

    def _expire_quote_requests(self) -> list[Command]:
        out: list[Command] = []
        for rid, (_, sent) in list(self.state.quote_requests.items()):
            if self.now_mono - sent >= QUOTE_REQUEST_STALE_S:     # the lane answers in 3 s; this one was lost
                out += self._refresh_done(rid, set())
        return out

    def _expire_reads(self) -> list[Command]:
        """Positions / orders / margins reads unanswered for READ_LOST_S stop counting as in flight (WARN)."""
        out: list[Command] = []
        for acct, bv in self.state.broker.items():
            lost = []
            for kind, sent in (("POSITIONS", bv.pos_sent), ("ORDERS", bv.ords_sent)):
                n = 0
                while sent and self.now_mono - sent[0] >= READ_LOST_S:
                    del sent[0]
                    n += 1
                if n:
                    lost.append((kind, n))
            bv.pos_pending, bv.ords_pending = len(bv.pos_sent), len(bv.ords_sent)
            if bv.margins_pending and bv.margins_sent is not None and self.now_mono - bv.margins_sent >= READ_LOST_S:
                bv.margins_pending, bv.margins_sent = False, None
                lost.append(("MARGINS", 1))
            for kind, n in lost:
                self.event("READ_LOST", {"account": str(acct), "kind": kind, "lost": n, "after_s": READ_LOST_S},
                           "WARN", account=acct)
                out.append(Alert("WARN", "READ_LOST", f"{acct}: {n} {kind} read(s) unanswered for "
                                 f"{READ_LOST_S:.0f} s; asking again", f"read_lost:{acct}:{kind}"))
        return out

    def _on_intent(self, intent: Intent) -> list[Command]:
        if self.state.stopping:
            self.event("INTENT_RECEIVED", {"strategy": intent.strategy, "underlying": intent.underlying,
                                           "refused": "STOPPING"}, "WARN", intent_id=intent.intent_id,
                       strategy=intent.strategy)
            return []
        return list(self.gateway.on_intent(self, intent))

    def _on_feed_state(self, ev: FeedState) -> list[Command]:
        st = self.state
        out: list[Command] = []
        bad = not ev.connected or ev.degraded
        self.event("FEED_STATE", {"connected": ev.connected, "reconnected": ev.reconnected, "degraded": ev.degraded,
                                  "reason": ev.reason}, "WARN" if bad else "INFO")
        if not ev.connected:
            if ev.reason == "NORECONNECT" and st.feed_dead_since is None:
                st.feed_dead_since = self.now_mono
                out.append(Alert("CRITICAL", "FEED_DEAD", "KiteTicker gave up reconnecting: exits use REST quotes; "
                                 "controlled restart in 60 s if still down", "feed:noreconnect"))
        else:
            st.feed_dead_since = None
            if ev.reconnected:
                out += self.request_orders(Account.LIVE, Priority.EXIT)      # back-fill missed order updates (§9.7)
                out.append(Alert("WARN", "FEED_RECONNECT", "WS reconnected; order updates back-filled",
                                 "feed:reconnect"))
        if ev.degraded:
            out += self.add_halt("STALE_FEED", "GLOBAL", f"feed degraded: {ev.reason}", alert=True)
        return out

    def _feed_tick(self) -> list[Command]:
        st = self.state
        out: list[Command] = []
        h = self.ctx.feed_health()
        if st.feed_dead_since is not None and not st.exit_sent:
            if h.connected:
                st.feed_dead_since = None
            elif self.now_mono - st.feed_dead_since >= FEED_NORECONNECT_EXIT_S:
                st.exit_sent = True
                self.event("SHUTDOWN", {"reason": "WS_NORECONNECT", "exit": EXIT_CONTROLLED_RESTART}, "CRITICAL")
                out += [Alert("CRITICAL", "FEED_DEAD", "WS still down 60 s after giving up: controlled restart",
                              "feed:restart"), Exit(EXIT_CONTROLLED_RESTART)]
        if h.connected and not h.degraded:
            if st.feed_healthy_since is None:
                st.feed_healthy_since = self.now_mono
            elif self.now_mono - st.feed_healthy_since >= STALE_FEED_HEALTHY_S:
                self._clear_auto("STALE_FEED", "GLOBAL", f"feed healthy for {STALE_FEED_HEALTHY_S:.0f} s")
        else:
            st.feed_healthy_since = None
        return out

    # ------------------------------------------------------------------------------------------------------------
    # Broker reads: recon rounds, recovery, margins, audit copies
    # ------------------------------------------------------------------------------------------------------------
    def _audit(self, acct: Account, kind: str, ok: bool, payload: Callable[[], Any], *, force: bool = False) -> None:
        """broker_snapshots: every failed read, every read behind a recon finding (force), and otherwise an OK one
        at most once a minute per (account, kind). Rows are keyed by receipt time; the same read twice is one row."""
        bv = self.state.broker[acct]
        if ok and not force:
            last = bv.audit_mono.get(kind)
            if last is not None and self.now_mono - last < 60.0:
                return
        if ok:
            bv.audit_mono[kind] = self.now_mono
        self.store.add_broker_snapshot(acct, kind, self.now_wall, ok, payload())

    def _on_positions(self, ev: PositionsSnapshot) -> list[Command]:
        acct = Account(ev.account)
        bv = self.state.broker[acct]
        del bv.pos_sent[:1]                                  # the oldest read in flight is answered
        bv.pos_pending = len(bv.pos_sent)
        self._audit(acct, "POSITIONS", ev.ok, lambda: list(ev.rows) if ev.ok else {"error": ev.error})
        out: list[Command] = []
        if ev.ok:
            bv.positions = ev
        else:
            bv.pos_failed = ev.req_mono if bv.pos_failed is None else max(bv.pos_failed, ev.req_mono)
            out += self._read_fatal(f"the {acct} positions read", ev.error)
        ra = self.state.recovery.get(acct)
        if ra is not None and not ra.done and ra.positions is None:
            ra.positions = ev
            out += self._recovery_progress(acct)
        if bv.round_start is not None and bv.round_pos is None and ev.req_mono >= bv.round_start:
            bv.round_pos = ev
        out += self._maybe_reconcile(acct)
        return out + (self._exits_after_read(acct) if ev.ok else [])

    def _on_orders(self, ev: OrdersSnapshot) -> list[Command]:
        acct = Account(ev.account)
        bv = self.state.broker[acct]
        del bv.ords_sent[:1]
        bv.ords_pending = len(bv.ords_sent)
        self._audit(acct, "ORDERS", ev.ok, lambda: list(ev.orders) if ev.ok else {"error": ev.error})
        out: list[Command] = []
        if ev.ok:
            bv.orders = ev
            self._selftest_seen_in(ev)
            out += self._backfill(ev)
        else:
            bv.ords_failed = ev.req_mono if bv.ords_failed is None else max(bv.ords_failed, ev.req_mono)
            out += self._read_fatal(f"the {acct} orders read", ev.error)
        ra = self.state.recovery.get(acct)
        if ra is not None and not ra.done and ra.orders is None:
            ra.orders = ev
            out += self._recovery_progress(acct)
        if bv.round_start is not None and bv.round_ords is None and ev.req_mono >= bv.round_start:
            bv.round_ords = ev
        out += self._maybe_reconcile(acct)
        return out + (self._exits_after_read(acct) if ev.ok else []) + self._selftest_verify()

    def _read_fatal(self, what: str, error: str | None) -> list[Command]:
        """§9.7: a token or permission error on a broker read is the same dead token as on an order: HALT(TOKEN,
        GLOBAL, AUTO) + CRITICAL once, which starts the 30 s session-file re-check (and Exit(75) on a newer token)."""
        if error_outcome(error) != FATAL or self.halted("TOKEN"):
            return []
        text = (f"token or permission failure on {what}: exits are impossible until the token is restored "
                f"({error})")[:500]
        self.add_halt("TOKEN", "GLOBAL", text, alert=False)
        return [Alert("CRITICAL", "TOKEN", text, "token:fatal")]

    def _exits_after_read(self, acct: Account) -> list[Command]:
        """DETAIL §5.5: the exit loop runs on every relevant event. A positions or orders read is one for every leg
        of its account that is exiting (the exit cap waits for a read made after the exit decision, verify-flat for a
        post-fill read) or covering, so the exit moves on as soon as the read lands instead of at the next Timer."""
        if self.state.stopping:
            return []
        exiting = self._exit_sequence([lg for lg in self.state.legs_in(LegState.EXITING, LegState.VERIFY_FLAT)
                                       if Account(lg.account) is acct])
        out: list[Command] = []
        for leg in exiting:
            out += self._exit_step(leg)
        stepped = {lg.leg_id for lg in exiting}
        for lid in sorted(self.state.exit_memory):
            leg = self.state.legs.get(lid)
            if lid not in stepped and leg is not None and Account(leg.account) is acct:
                out += list(self.exit_loop.step(self, leg))
        return out

    def _backfill(self, ev: OrdersSnapshot) -> list[Command]:
        """Apply an orders read to every non-terminal bot order it shows (§3.3, §9.7 WS back-fill); a NOT_PLACED
        entry it shows taken is adopted (_adopt_late_fill)."""
        out: list[Command] = []
        source = "RECOVERY" if not self.ctx.recovered else "POLL"
        for b in ev.orders:
            o = orders.find_order(self.state.orders, ev.account, tag=b.tag, broker_order_id=b.broker_order_id)
            if o is None or o.role == Role.EXTERNAL or o.leg_id not in self.state.legs:
                continue
            snap = snapshot_of(b, source, ev.recv_mono)
            if o.is_terminal:
                out += self._adopt_late_fill(o, snap)
                continue
            leg = self.state.legs[o.leg_id]
            fx = orders.on_snapshot(self.store, o, leg, snap, self._timers(o), self.now_mono, self.now_wall)
            out += self._order_effects(o, leg, fx)
        return out

    def _maybe_reconcile(self, acct: Account) -> list[Command]:
        bv = self.state.broker[acct]
        if bv.round_pos is None or bv.round_ords is None:
            return []
        pos, ords = bv.round_pos, bv.round_ords
        bv.round_start, bv.round_pos, bv.round_ords = None, None, None
        ra = self.state.recovery.get(acct)
        if not self.ctx.recovered or (ra is not None and not ra.done):
            return []                    # recon starts once recovery applied its plan (which then reconciles twice)
        res = recon.reconcile(self.bot_view(acct), pos, ords, bv.recon, self.now_mono)
        bv.last_recon_mono, bv.recon_req = self.now_mono, pos.req_mono
        if pos.ok and any(f.cls not in (ReconClass.CLEAN, ReconClass.PENDING) for f in res.findings):
            self._audit(acct, "POSITIONS", True, lambda: list(pos.rows), force=True)   # the read behind a finding
        return self._apply_recon(acct, res)

    def _apply_recon(self, acct: Account, res: recon.ReconResult) -> list[Command]:
        bv = self.state.broker[acct]
        bv.recon = res.memory
        out: list[Command] = list(res.alerts) + list(res.commands)
        for e in res.events:
            self.event(e.kind, dict(e.payload), e.severity, account=acct)
        for h in res.halts:
            out += self.apply_halt(h, alert=False)
        for h in res.clear_halts:
            self._clear_auto(h.kind, h.scope, h.reason)
        for f in res.findings:
            if f.cls is ReconClass.REPAIRED:
                for pk, b in f.repairs:
                    o = self.state.orders.get(pk)
                    if o is not None and not o.is_terminal and o.leg_id in self.state.legs:
                        leg = self.state.legs[o.leg_id]
                        fx = orders.on_snapshot(self.store, o, leg, snapshot_of(b, "RECOVERY", self.now_mono),
                                                self._timers(o), self.now_mono, self.now_wall)
                        out += self._order_effects(o, leg, fx)
            elif f.cls in (ReconClass.EXTERNAL_CLOSE, ReconClass.BROKER_SQUAREOFF):
                for c in f.closes:
                    leg = self.state.legs.get(c.leg_id)
                    st = self.state.structures.get(leg.structure_id) if leg is not None else None
                    if st is not None and not st.is_terminal:
                        out += self.close_external(st, leg, c)
            elif f.cls is ReconClass.PARTIAL_EXTERNAL:
                for lid in f.legs:
                    if lid in self.state.legs:
                        out += self.force_exit(self.state.legs[lid], "ANOMALY_EXIT")
            elif f.cls is ReconClass.SHORT and f.cover is not None:
                out += list(self.exit_loop.cover(self, f.cover))
        if res.live_demote:
            classes = ", ".join(sorted({f"{f.cls} {f.symbol}" for f in res.findings
                                        if f.cls not in (ReconClass.CLEAN, ReconClass.PENDING)}))
            out += self.add_halt("INVARIANT", "GLOBAL", f"LIVE reconciliation problem (§10.9): {classes}")
            for d in g1.demote_all_live(self.store, f"LIVE_RECON: {classes}"[:300], self.now_wall, self.now_mono,
                                        changed_by="recon"):
                out.append(d.alert())
            self.reload_modes()
        return out

    def _recon_tick(self) -> list[Command]:
        out: list[Command] = []
        for acct in ACCOUNTS:
            bv = self.state.broker[acct]
            if bv.extra_round_at is not None and self.now_mono >= bv.extra_round_at:
                bv.extra_round_at = None
                out += self.start_recon(acct)
                continue
            held = any(Account(lg.account) is acct and lg.state in _HELD for lg in self.state.legs.values())
            if recon.recon_due(self.now_mono, self.now_wall, bv.last_round_mono, held,
                               self.settings.RECON_INTERVAL_S, window_end=self._recon_window_end()):
                out += self.start_recon(acct)
            elif self._late_hold_account(acct) and (bv.last_round_mono is None or self.now_mono - bv.last_round_mono
                                                    >= recon.RECON_OPEN_LEGS_INTERVAL_S - 1e-9):
                out += self.start_recon(acct)            # a late start: past the window, until it is booked
        return out

    def _recon_window_end(self) -> dtime:
        """The periodic recons run until the last EOD recon (RECON_2), which moves with BROKER_SQUAREOFF_AT [V]."""
        sq = ist_datetime(self.session, self.settings.BROKER_SQUAREOFF_AT)
        return to_ist(sq + timedelta(seconds=dict(EOD_AFTER_SQUAREOFF_S)["RECON_2"])).time()

    def _mark_late_start(self) -> None:
        """At READY: a start at or after today's BROKER_SQUAREOFF_AT on a trading day may find a square-off made while
        the bot was down; it gets the late recon until LATE_RECON_UNTIL, or LATE_RECON_MIN_S from now if later, and
        its grace counts from now (_late_hold_account)."""
        if self.ctx.late_recon_until is not None or not self.calendar.is_trading_day(self.session):
            return
        if self.now_wall < ist_datetime(self.session, self.settings.BROKER_SQUAREOFF_AT):
            return
        until = max(ist_datetime(self.session, LATE_RECON_UNTIL), self.now_wall + timedelta(seconds=LATE_RECON_MIN_S))
        self.ctx.late_recon_until, self.ctx.late_recon_from = until, self.now_mono
        self.event("LATE_START", {"squareoff_at": self.settings.BROKER_SQUAREOFF_AT.strftime("%H:%M"),
                                  "recon_until": iso(until)})

    def _late_hold(self) -> bool:
        """A late start (_mark_late_start) holds while any account holds a bot leg that its recon has not shown the
        broker agreeing with (_late_hold_account): recon keeps its 5 s rounds past the window, and the EOD steps from
        TRUTH on (and a recovery-only run's exit) wait, so the square-off is booked today, before the truth check
        and G1 read the day."""
        return any(self._late_hold_account(a) for a in ACCOUNTS)

    def _late_hold_account(self, acct: Account) -> bool:
        """The account holds a bot leg, and no recon round requested more than FRESH_FILL_GRACE_S after READY has
        come back CLEAN: the broker may differ. The paper exchange squares off at its first steps, before READY, and
        may wait 1.5 s for depth; the grace counts from READY, not from the process start, which comes before the
        store open, the backup and the token check (review round 2 follow-up): after a slow start a READY read made
        before that square-off filled was already past it. CLEAN after the grace means the broker still holds what
        the bot holds, so there is nothing to wait for."""
        until = self.ctx.late_recon_until
        if until is None or self.now_wall >= until:
            return False
        if not any(Account(lg.account) is acct for lg in self.state.legs_in(*_HELD)):
            return False
        bv = self.state.broker[acct]
        ready = self.ctx.late_recon_from if self.ctx.late_recon_from is not None else -math.inf
        return not (bv.recon.status == ReconClass.CLEAN.value and bv.recon_req is not None
                    and bv.recon_req > ready + FRESH_FILL_GRACE_S)

    def _entries_resume_tick(self) -> None:
        """§9.6: entries resume after recovery, two CLEAN recons per account in use, a warm feed and the dump."""
        st = self.state
        if st.entries_resumed or not self.ctx.recovered:
            return
        h = self.ctx.feed_health()
        warm = bool(h.connected and h.connected_since_mono is not None
                    and self.now_mono - h.connected_since_mono >= self.settings.FEED_WARM_S)
        accounts = [Account.PAPER, Account.SHADOW]
        if any(r["mode"] == "LIVE" for r in st.modes.values()):
            accounts.append(Account.LIVE)
        if recon.entries_may_resume({a: bv.recon for a, bv in st.broker.items()}, accounts, feed_warm=warm,
                                    prep_done=self.instruments_loaded, entry_halted=False):
            st.entries_resumed = True
            self.event("PHASE", {"phase": "TRADING", "entries": "RESUMED"})

    def _on_margins(self, ev: MarginsResult) -> list[Command]:
        acct = Account(ev.account)
        bv = self.state.broker[acct]
        bv.margins_pending, bv.margins_sent = False, None
        bv.margins = ev
        self._audit(acct, "MARGINS", ev.ok,
                    lambda: {"available_rs": ev.margins.available_rs} if ev.ok and ev.margins else {"error": "read"})
        return []

    def _margins_tick(self) -> list[Command]:
        if not self.ctx.recovered or not self.calendar.is_trading_day(self.session):
            return []
        t = to_ist(self.now_wall).time()
        if not MARGINS_FROM <= t < self.settings.NO_NEW_AFTER:
            return []
        accounts = [Account.PAPER]
        if any(r["mode"] == "LIVE" for r in self.state.modes.values()):
            accounts.append(Account.LIVE)
        out: list[Command] = []
        for acct in accounts:
            bv = self.state.broker[acct]
            if bv.margins_pending:
                continue
            every = MARGINS_EVERY_S if bv.margins is not None and bv.margins.ok else MARGINS_RETRY_S
            if bv.margins is None or self.now_mono - bv.margins.recv_mono >= every:
                out.append(FetchMargins(acct))
        return out

    def _on_tradebook(self, ev: TradebookSnapshot) -> list[Command]:
        acct = Account(ev.account)
        self._audit(acct, "TRADES", ev.ok, lambda: list(ev.trades) if ev.ok else {"error": "read"})
        ra = self.state.recovery.get(acct)
        if ra is not None and not ra.done and ra.tradebook_ok is None:
            ra.tradebook_ok = bool(ev.ok)
            return self._recovery_progress(acct)
        if self.state.tradebook_purpose.pop(acct, None) == "TRUTH":
            return self._truth_check(acct, ev)
        return []

    # ------------------------------------------------------------------------------------------------------------
    # Recovery (§9.6)
    # ------------------------------------------------------------------------------------------------------------
    def _on_recovery_start(self) -> list[Command]:
        if self.ctx.recovery_started or self.ctx.recovered:
            return []
        self.ctx.recovery_started = True
        self.event("PHASE", {"phase": "RECOVERING"})
        out: list[Command] = []
        for acct in ACCOUNTS:
            self.state.recovery[acct] = RecoveryAcct(retry_at=None)
            out += list(recon.recovery_fetches(acct))
        self.state.recovery_started_mono = self.now_mono
        return out

    def _abort_recovery(self, why: str) -> list[Command]:
        """A reload while recovery ran: its progress is lost. READY anyway (exits must run); the halt the caller
        raises keeps entries off until an operator ACK."""
        if not self.ctx.recovery_started or self.ctx.recovered:
            return []
        self.ctx.recovered = True
        self.event("READY", {"recovery": "ABORTED", "why": why[:300]}, "CRITICAL")
        self._mark_late_start()
        return [Alert("CRITICAL", "RECOVERY_ABORTED", f"recovery did not complete ({why}); exits run on recon caps",
                      "recovery:aborted")]

    def _recovery_progress(self, acct: Account) -> list[Command]:
        ra = self.state.recovery[acct]
        if not ra.answered():
            return []
        if not ra.all_ok() and ra.tries < recon.RECOVERY_FETCH_TRIES:
            if ra.retry_at is None:
                ra.retry_at = self.now_mono + recon.RECOVERY_FETCH_GAP_S
            return []
        return self._recovery_apply(acct)

    def _recovery_tick(self) -> list[Command]:
        out: list[Command] = []
        started = self.state.recovery_started_mono
        for acct, ra in self.state.recovery.items():
            if ra.done:
                continue
            if ra.retry_at is not None and self.now_mono >= ra.retry_at:
                ra.tries += 1
                ra.retry_at = None
                ra.orders, ra.positions, ra.tradebook_ok = None, None, None
                out += list(recon.recovery_fetches(acct))
            elif started is not None and self.now_mono - started >= RECOVERY_MAX_S:
                ra.tries = recon.RECOVERY_FETCH_TRIES
                out += self._recovery_apply(acct)
        return out

    def _trade_qty(self, acct: Account) -> dict[str, int]:
        pks = [o.order_pk for o in self.state.orders.values() if Account(o.account) is acct]
        rows = ledger.query_in(self.store, "SELECT order_pk, SUM(qty) AS q FROM fills WHERE source = 'TRADE' AND "
                                           "order_pk IN ({}) GROUP BY order_pk", pks)
        return {r["order_pk"]: int(r["q"]) for r in rows}

    def _recovery_apply(self, acct: Account) -> list[Command]:
        ra = self.state.recovery[acct]
        if ra.done:
            return []
        ra.done = True
        plan = recon.plan_recovery(
            account=acct, session=self.session, now_wall=self.now_wall, eod_flatten_at=self.eod.flatten,
            structures=[s for s in self.state.structures.values() if Account(s.account) is acct],
            legs=[lg for lg in self.state.legs.values() if Account(lg.account) is acct],
            trade_qty=self._trade_qty(acct), orders=ra.orders, positions=ra.positions,
            tradebook_ok=bool(ra.tradebook_ok), import_tags=self.ctx.live_orders or acct is not Account.LIVE)
        out = self._apply_plan(plan)
        self.event("RECOVERY", {"account": str(acct), "applied": len(plan.apply), "lookups": len(plan.lookups),
                                "vanished": list(plan.vanished), "imports": len(plan.imports),
                                "opened": list(plan.open_legs), "voided": [x[0] for x in plan.void_legs],
                                "flatten": list(plan.flatten_legs), "stale": [s.structure_id for s in plan.stale]},
                   account=acct)
        if all(r.done for r in self.state.recovery.values()):
            out += self._recovery_finish()
        return out

    def _apply_plan(self, plan: recon.RecoveryPlan) -> list[Command]:
        acct = plan.account
        out: list[Command] = list(plan.alerts) + list(plan.commands)
        for e in plan.events:
            self.event(e.kind, dict(e.payload), e.severity, account=acct)
        for h in plan.halts:
            out += self.apply_halt(h, alert=False)
        for pk, b in plan.apply:
            o = self.state.orders.get(pk)
            if o is None or o.is_terminal or o.leg_id not in self.state.legs:
                continue
            leg = self.state.legs[o.leg_id]
            fx = orders.on_snapshot(self.store, o, leg, snapshot_of(b, "RECOVERY", self.now_mono), self._timers(o),
                                    self.now_mono, self.now_wall)
            out += self._order_effects(o, leg, fx)
        out += self._import_tags(acct, plan.imports)
        for lid in plan.open_legs:
            leg = self.state.legs.get(lid)
            st = self.state.structures.get(leg.structure_id) if leg is not None else None
            if st is not None and leg.state is LegState.ENTERING and not self.state.busy(leg):
                out += self._entry_done(leg, st)
        for lid, reason in plan.void_legs:
            leg = self.state.legs.get(lid)
            st = self.state.structures.get(leg.structure_id) if leg is not None else None
            if st is not None and st.state is StructState.ENTERING and not self.state.busy(leg):
                out += self.void(st, reason)
        for sid, reason in plan.void_structures:
            st = self.state.structures.get(sid)
            if st is not None and st.state is StructState.RESERVED:
                out += self.void(st, reason)
        for lid in plan.flatten_legs:
            leg = self.state.legs.get(lid)
            if leg is not None and leg.state is LegState.OPEN:
                out += self.force_exit(leg, "RECOVERY_FLATTEN")
        for s in plan.stale:
            out += self._apply_stale(s)
        return out

    def _apply_stale(self, s: StaleStructure) -> list[Command]:
        """§9.6 step 7: a structure of an earlier session is never traded again. An order of it the DB still holds
        open can never be resolved through the broker (Kite and the paper exchange serve only today's orders): it is
        concluded here (orders.session_ended, P&L UNRESOLVED) and the action is re-planned from what the structure
        then holds (recon.stale_action), so the leftover settles and frees its leg, symbol and account today."""
        st = self.state.structures.get(s.structure_id)
        if st is None or st.is_terminal:
            return []
        legs = self.state.legs_of(st)
        out: list[Command] = []
        action = s.action
        busy = [o for leg in legs for o in self.state.busy(leg)]
        if busy:
            for o in busy:
                out += list(orders.session_ended(self.store, o, self._timers(o), self.now_mono, self.now_wall,
                                                 session=s.session).commands)
            positions.mark_unresolved(self.store, st, now_mono=self.now_mono, now_wall=self.now_wall,
                                      detail=f"orders {[o.order_pk for o in busy]} of {s.session} never resolved")
            action = recon.stale_action(Account(st.account), legs)
            self.event("STALE_ORDERS_ENDED", {"session": s.session.isoformat(), "orders": [o.order_pk for o in busy],
                                              "planned": s.action, "action": action}, "CRITICAL",
                       account=st.account, book=st.book, structure_id=st.structure_id)
        if action == "AWAIT_PAPER_SQUAREOFF":
            # the simulator's startup square-off, then recon, closes it; a close the paper exchange made on an earlier
            #   day is in no order of today: its own record books it (_on_squareoffs)
            return out + self._ask_squareoffs(st)
        if action == "VOID":
            if st.state in (StructState.RESERVED, StructState.ENTERING) and all(lg.entry_filled == 0 for lg in legs):
                return out + self.void(st, "STALE_SESSION")
            return out
        if action == "CLOSE":
            for leg in legs:
                if leg.state is LegState.OPEN:
                    positions.begin_exit(self.store, st, leg, "RECOVERY_FLATTEN", Urgency.PROTECTIVE,
                                         now_mono=self.now_mono, now_wall=self.now_wall)
                if leg.state is LegState.EXITING and leg.open_qty <= 0:
                    positions.to_verify_flat(self.store, st, leg, now_mono=self.now_mono, now_wall=self.now_wall)
            return out + (self.close(st) if st.state is StructState.VERIFY_FLAT else [])
        return out + self._close_unresolved(st, legs)   # CLOSE_UNRESOLVED: LIVE (earlier days' orders are gone)

    def _close_unresolved(self, st: Structure, legs: Iterable[Leg]) -> list[Command]:
        """§9.6 CLOSE_UNRESOLVED: every leg still holding qty is CLOSED_EXTERNAL at the last realizable bid (unpriced:
        booked at 0), pnl UNRESOLVED; the MANUAL close raises HALT(MISMATCH, ACCOUNT) (close_external)."""
        out: list[Command] = []
        for leg in legs:
            held = leg.open_qty
            if held > 0 and leg.state in (LegState.ENTERING, LegState.OPEN, LegState.EXITING):
                ext = ExternalClose(leg.leg_id, held, ReconClass.EXTERNAL_CLOSE, PnlStatus.UNRESOLVED,
                                    (ExternalFill(None, held, self.state.last_rb.get(leg.leg_id), None),), ())
                out += self.close_external(st, leg, ext)
        return out

    def _ask_squareoffs(self, st: Structure, tries: int = 1) -> list[Command]:
        """FetchSquareoffs for a PAPER/SHADOW leftover's symbols since its session (review round 2): the answer says
        whether the paper exchange still carried it into today, and how it closed it if not."""
        if Account(st.account) is Account.LIVE:
            return []
        symbols = tuple(sorted({lg.inst.symbol for lg in self.state.legs_of(st)}))
        if not symbols:
            return []
        cmd = FetchSquareoffs(Account(st.account), self._rid("sqoff"), symbols, st.session)
        self.ctx.squareoff_asks[cmd.request_id] = (cmd, tries)
        return [cmd]

    def _on_squareoffs(self, ev: SquareoffsResult) -> list[Command]:
        """The paper exchange's record for leftovers of earlier sessions (§9.6 step 7 backstop, review round 2). A
        symbol it held nothing of at the start of today was closed on an earlier day: a square-off or settlement the
        bot never booked (it was down, or stopped before recon could confirm it), which no order of today can show,
        so recon would hold it as a CONTRADICTION for good. Its legs are booked from that record, as recon books a
        square-off (recon.carried_closes: FINAL at the record's prices); with no record at all they close UNRESOLVED
        like a LIVE leftover (CRITICAL MISMATCH, HALT(MISMATCH), acked once recon is CLEAN). A symbol still carried
        into today is the startup settlement's, which recon books from today's orders."""
        acct = Account(ev.account)
        asked = self.ctx.squareoff_asks.pop(ev.request_id, None)
        if not ev.ok:
            if asked is None:
                return []
            cmd, tries = asked
            if tries < SQUAREOFF_ASK_TRIES:
                retry = dataclasses.replace(cmd, request_id=self._rid("sqoff"))
                self.ctx.squareoff_asks[retry.request_id] = (retry, tries + 1)
                return [retry]
            text = (f"{acct}: the paper exchange's square-off record for {', '.join(cmd.symbols)} could not be read "
                    f"({tries} tries): a leftover of {cmd.since} it closed on an earlier day stays unbooked (recon "
                    "holds it as a contradiction); check paper_exchange.db and restart")
            self.event("SQUAREOFF_RECORD", {"account": str(acct), "symbols": list(cmd.symbols), "ok": False},
                       "CRITICAL", account=acct)
            return [Alert("CRITICAL", "STALE_STRUCTURE", text, f"squareoffs:{acct}:{','.join(cmd.symbols)}")]
        out: list[Command] = []
        bot = self.bot_view(acct)
        for symbol, net in ev.carried:
            if net != 0:
                continue                 # carried into today: the startup settlement's (today's orders show it)
            legs = [lg for lg in bot.legs if lg.inst.symbol == symbol and lg.session < self.session
                    and not lg.is_terminal and lg.open_qty > 0]
            if not legs:
                continue
            record = tuple(b for b in ev.orders if b.symbol == symbol)
            closes = recon.carried_closes(bot, symbol, record)
            sids = sorted({lg.structure_id for lg in legs})
            payload = {"account": str(acct), "symbol": symbol, "legs": [lg.leg_id for lg in legs],
                       "orders": [b.broker_order_id for b in record], "booked": bool(closes),
                       "pnl_status": str(closes[0].pnl_status) if closes else str(PnlStatus.UNRESOLVED)}
            for sid in sids:
                st = self.state.structures[sid]
                self.event("SQUAREOFF_RECORD", payload, "WARN", account=acct, book=st.book,
                           structure_id=sid)
            if closes:
                for c in closes:
                    leg = self.state.legs.get(c.leg_id)
                    st = self.state.structures.get(leg.structure_id) if leg is not None else None
                    if st is not None and not st.is_terminal:
                        out += self.close_external(st, leg, c)
                when = ", ".join(sorted({session_of(b.order_wall or self.now_wall).isoformat() for b in record}))
                out.append(Alert("CRITICAL", "BROKER_SQUAREOFF",
                                 f"{acct} {symbol}: closed by the paper exchange on {when} while titan2 was down; "
                                 f"booked now from its own record ({closes[0].pnl_status})",
                                 f"squareoff_record:{acct}:{symbol}"))
                continue
            for sid in sids:
                st = self.state.structures[sid]
                text = (f"{acct} structure {sid} of {st.session} still held, but the paper exchange holds none of "
                        f"{symbol} and has no square-off record for it: closed as UNRESOLVED")
                self.event("MISMATCH", {"account": str(acct), "structure_id": sid, "session": st.session.isoformat(),
                                        "symbol": symbol, "broker_qty": 0}, "CRITICAL", account=acct, book=st.book)
                out.append(Alert("CRITICAL", "MISMATCH", text, f"recovery:{acct}:stale:{sid}"))
                out += self._close_unresolved(st, self.state.legs_of(st))
        return out

    def _import_tags(self, acct: Account, imports: Iterable[TagImport]) -> list[Command]:
        """§9.6 step 3: today's bot-tagged broker orders the DB lost; attached to their leg, or to a new
        RECOVERED:<account> structure (strategy _recovered, default exit profile). CRITICAL either way.

        Several lost seqs can share an underlying (two PAPER books on one stock; a LIVE trade and a later one on the
        same stock), and only one non-terminal structure may hold (book, underlying) and (LIVE, underlying) (I5):
        a seq its own orders show flat is imported first and closed at once, which frees the slot; a seq still
        holding qty next to another gets a RECOVERED book of its own. One that still cannot be rebuilt is alerted
        and left to recon; it never aborts the recovery of the others."""
        out: list[Command] = []
        for imp in sorted(imports, key=lambda i: (not _seq_flat(i), i.seq_no)):
            leg = self.state.legs.get(imp.leg_id) if imp.leg_id is not None else None
            if leg is None:
                made = self._recovered_leg(acct, imp)
                if isinstance(made, str):
                    out.append(Alert("CRITICAL", "RECOVERY_IMPORT", f"{acct}: bot orders of seq {imp.seq_no} "
                                     f"({[b.broker_order_id for b in imp.orders]}) cannot be rebuilt ({made}): "
                                     "settle them in Kite", f"recovery:{acct}:noimport:{imp.seq_no}"))
                    continue
                leg = made
            st = self.state.structures[leg.structure_id]
            for b in imp.orders:
                out += self._import_order(acct, st, leg, b)
            if (imp.leg_id is None and leg.open_qty == 0 and not self.state.busy(leg)
                    and st.state in (StructState.OPEN, StructState.EXITING, StructState.VERIFY_FLAT)):
                out += self._close_imported(st, leg)
            elif st.state is StructState.OPEN and leg.state is LegState.OPEN and leg.open_qty <= 0:
                out += self.force_exit(leg, "RECOVERY_FLATTEN")
        return out

    def _close_imported(self, st: Structure, leg: Leg) -> list[Command]:
        """A RECOVERED leg whose imported orders bought and sold the same qty, none still working: booked from those
        fills and closed now (EXITING(RECOVERY_FLATTEN) -> VERIFY_FLAT -> CLOSED). Its own broker orders are the
        evidence it is flat; anything else on the symbol is recon's (§9.3)."""
        if leg.state is LegState.OPEN:
            positions.begin_exit(self.store, st, leg, "RECOVERY_FLATTEN", Urgency.PROTECTIVE, now_mono=self.now_mono,
                                 now_wall=self.now_wall)
        if leg.state is LegState.EXITING:
            positions.to_verify_flat(self.store, st, leg, now_mono=self.now_mono, now_wall=self.now_wall)
        return self.close(st) if st.state is StructState.VERIFY_FLAT else []

    def _recovered_book(self, acct: Account, underlying: str, imp: TagImport) -> str | None:
        """The RECOVERED book a lost seq goes to: imp.book unless a non-terminal structure there already holds the
        underlying, then '<imp.book>:<seq>'. None on LIVE while any LIVE structure holds the underlying (one per
        underlying, whatever the book)."""
        live = self.state.live_structures()
        if Account(acct) is Account.LIVE and any(Account(s.account) is Account.LIVE and s.underlying == underlying
                                                 for s in live):
            return None
        taken = {s.book for s in live if s.underlying == underlying}
        return next((b for b in (imp.book, f"{imp.book}:{imp.seq_no}") if b not in taken), None)

    def _recovered_leg(self, acct: Account, imp: TagImport) -> Leg | str:
        """A new ENTERING structure and leg for a lost seq (§9.6 step 3), or why there can be none."""
        store = self.instruments
        inst = store.by_symbol(imp.orders[0].symbol) if store is not None else None
        if inst is None:
            return "no contract row"
        book = self._recovered_book(acct, inst.underlying, imp)
        if book is None:
            return f"another non-terminal structure holds {inst.underlying} (I5)"
        ce = inst.opt_type == "CE"
        intent = Intent(intent_id=f"rec{uuid.uuid4().hex[:9]}", strategy=recon.RECOVERED_STRATEGY,
                        strategy_version="0", underlying=inst.underlying,
                        direction=Direction.UP if ce else Direction.DOWN,
                        structure=StructureType.LONG_CE if ce else StructureType.LONG_PE,
                        strike_pref=StrikePref.ATM, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=inst.strike,
                        recheck=Recheck(-math.inf, math.inf), ttl_s=10.0, created_mono=self.now_mono,
                        created_wall=self.now_wall, reason={"recovered_seq": imp.seq_no})
        self.store.insert_intent(intent, self.session)
        try:
            st = self.reserve(intent, book=book, account=acct, policy=Policy.FULL)
        except InvariantViolation as iv:       # the DB's I5 index (its savepoint is rolled back): never abort recovery
            if getattr(iv, "code", None) != "I5":
                raise
            return f"I5: {getattr(iv, 'detail', iv)}"
        leg = Leg(leg_id=0, structure_id=st.structure_id, leg_no=1, account=acct, book=book,
                  session=self.session, seq_no=imp.seq_no, inst=inst, side="LONG", state=LegState.ENTERING)
        self.store.insert_leg(leg)
        leg.bind_orders(self.state.orders)
        st.legs.append(leg.leg_id)
        st.state = StructState.ENTERING
        self.store.update_structure(st)
        self.state.legs[leg.leg_id] = leg
        return leg

    def _import_order(self, acct: Account, st: Structure, leg: Leg, b: Any) -> list[Command]:
        p = parse_tag(b.tag)
        if p is None or p.role not in ROLE_SIDE:
            return []
        tick = leg.inst.tick
        px = b.avg_px if _pos(b.avg_px) else None
        if px is None:
            limit = tick
        else:
            limit = max(tick, floor_tick(px, tick) if ROLE_SIDE[p.role] is Side.SELL else ceil_tick(px, tick))
        o = Order(order_pk=b.tag, leg_id=leg.leg_id, structure_id=st.structure_id, account=acct, book=leg.book,
                  role=p.role, side=ROLE_SIDE[p.role], symbol=leg.inst.symbol, qty=b.qty, limit_px=limit,
                  attempt=p.attempt, band_idx=None, state=OrderState.SUBMITTING,
                  created_wall=whole_ms(b.order_wall or self.now_wall), submitted_mono=self.now_mono)
        self.store.insert_order(o, submitted_wall=b.order_wall or self.now_wall)
        self.state.orders[o.order_pk] = o
        self._imported.add(o.order_pk)
        leg.orders.append(o.order_pk)
        self.event("ORDER_CREATED", {"recovered": True, "broker_order_id": b.broker_order_id, "status": b.status,
                                     "qty": b.qty, "filled": b.filled_qty, "limit_unknown": True}, "CRITICAL",
                   account=acct, book=leg.book, structure_id=st.structure_id, leg_id=leg.leg_id,
                   order_pk=o.order_pk)
        fx = orders.on_snapshot(self.store, o, leg, snapshot_of(b, "RECOVERY", self.now_mono), self._timers(o),
                                self.now_mono, self.now_wall)
        return self._order_effects(o, leg, fx)

    def _recovery_finish(self) -> list[Command]:
        """READY (§9.5 step 11), then reconcile twice >= 3 s apart (§9.6 step 6)."""
        self.ctx.recovered = True
        self.event("READY", {"phase": self.phase})
        self._mark_late_start()
        out: list[Command] = []
        for acct in ACCOUNTS:
            out += self.start_recon(acct)
            self.state.broker[acct].extra_round_at = self.now_mono + RECON_CONFIRM_GAP_S
        return out

    # ------------------------------------------------------------------------------------------------------------
    # Prep, prestart self-check, token
    # ------------------------------------------------------------------------------------------------------------
    def _prep_tick(self) -> list[Command]:
        if not self.ctx.recovered or not self.calendar.is_trading_day(self.session):
            return []
        out: list[Command] = []
        for kind in DAILY_PREP:
            if self.ctx.prep.get(kind) is True or kind in self.state.prep_pending:
                continue
            due = self.state.prep_retry_at.get(kind)
            if due is not None and self.now_mono < due:
                continue
            self.state.prep_pending.add(kind)
            out.append(Prep(kind))
        return out

    def _on_prep(self, ev: PrepResult) -> list[Command]:
        if ev.kind == "TOKEN":
            return self._on_token_check(ev)
        self.state.prep_pending.discard(ev.kind)
        self.ctx.prep[ev.kind] = ev.ok is True
        self.event("PREP", {"kind": ev.kind, "ok": ev.ok, "detail": str(ev.detail)[:300]},
                   "INFO" if ev.ok else "WARN")
        out: list[Command] = []
        if not ev.ok:
            self.state.prep_retry_at[ev.kind] = self.now_mono + PREP_RETRY_S
        if ev.kind == "INSTRUMENTS":
            if ev.ok and self.instruments is not None:
                self._clear_auto("NO_INSTRUMENTS", "GLOBAL", "today's dump loaded")
            else:
                out += self.add_halt("NO_INSTRUMENTS", "GLOBAL", f"instruments prep failed: {ev.detail}"[:300])
        elif ev.kind == "BAN_LIST" and not ev.ok:
            out.append(Alert("WARN", "BAN_LIST", f"F&O ban list unavailable ({ev.detail}): no stock is eligible "
                             "today", "prep:ban_list"))
        if not self.ctx.signals_started and all(k in self.ctx.prep for k in DAILY_PREP):
            self.ctx.signals_started = True
            out += [RunSelfcheck("prestart"), StartSignals()]
        return out

    def _on_selfcheck(self, ev: SelfcheckResult) -> list[Command]:
        report = ev.report
        if report is None:
            self.event("SELFCHECK", {"phase": ev.phase, "ok": False, "error": ev.error}, "WARN")
            return [Alert("WARN", "SELFCHECK", f"prestart self-check did not run: {ev.error}", "selfcheck:error")]
        out: list[Command] = []
        raised = set()
        for h in report.halts():
            raised.add(h.kind)
            out += self.add_halt(h.kind, h.scope, h.reason, clears=h.clears, alert=False)
        out += list(report.alert_commands())
        for s in report.demotions():
            d = g1.demote(self.store, s, "SELFCHECK: LIVE without a CERTIFIED trial", self.now_wall, self.now_mono,
                          changed_by="selfcheck")
            if d is not None:
                out.append(d.alert())
        self.reload_modes()
        if report.fatal:
            out += self.add_halt("INVARIANT", "GLOBAL", "prestart self-check found unsafe code or a damaged DB")
        for kind in ("CLOCK_UNSYNCED", "V1_ACTIVE"):
            if kind not in raised:
                self._clear_auto(kind, "GLOBAL", "prestart self-check passed")
        self.event("SELFCHECK", {"phase": ev.phase, "ok": bool(report.ok), "fatal": bool(report.fatal),
                                 "halts": sorted(raised)}, "INFO" if report.ok else "WARN")
        return out

    def _token_tick(self) -> list[Command]:
        st = self.state
        if self.token_ok or st.token_pending or (st.token_due is not None and self.now_mono < st.token_due):
            return []
        st.token_pending = True
        st.token_due = self.now_mono + TOKEN_RECHECK_S
        return [Prep("TOKEN")]

    def _on_token_check(self, ev: PrepResult) -> list[Command]:
        """§9.7: a token that works clears HALT(TOKEN); a NEWER one that works means a controlled restart."""
        self.state.token_pending = False
        self.event("PREP", {"kind": "TOKEN", "ok": ev.ok, "detail": str(ev.detail)[:200]}, "INFO" if ev.ok else "WARN")
        if not ev.ok:
            return []
        if str(ev.detail).upper().startswith("NEW") and not self.state.exit_sent:
            self.state.exit_sent = True
            self.event("SHUTDOWN", {"reason": "TOKEN_ROTATED", "exit": EXIT_CONTROLLED_RESTART}, "WARN")
            return [Alert("WARN", "TOKEN", "a new session token works: controlled restart", "token:restart"),
                    Exit(EXIT_CONTROLLED_RESTART)]
        self._clear_auto("TOKEN", "GLOBAL", "profile() succeeds")
        return []

    # ------------------------------------------------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------------------------------------------------
    def _on_timer(self, ev: Timer) -> list[Command]:
        out = self._clock_check(ev)
        if session_of(self.now_wall) != self.session:
            out += self._new_session()
        out += self._order_timers()
        out += self._expire_reads()
        for leg in self.state.legs_in(LegState.OPEN):
            out += self._evaluate(leg)
        exiting = self._exit_sequence(self.state.legs_in(LegState.EXITING, LegState.VERIFY_FLAT))
        for leg in exiting:
            out += self._exit_step(leg)
        out += self._other_exit_memory({lg.leg_id for lg in exiting})
        if not self.state.stopping:
            # the gateway re-runs G09 for waiting intents: this Timer's order timers, exits and closes may have changed
            #   the book counters the event started with (orders, entries, realized net)
            self._counters.clear()
            out += list(self.gateway.on_timer(self))
        out += self._expire_quote_requests()
        out += self._recovery_tick()
        out += self._recon_tick()
        self._entries_resume_tick()
        out += self._margins_tick()
        out += self._prep_tick()
        out += self._token_tick()
        out += self._feed_tick()
        self._auto_clears()
        out += self._risk_tick()
        out += self._selftest_tick()
        out += operator.poll(self)
        out += operator.flatten_tick(self)
        out += self._eod_tick()
        self._heartbeat_tick()
        out += self._tails_tick()
        self._flush_marks()
        out += self._shutdown_tick()
        return out

    def _clock_check(self, ev: Timer) -> list[Command]:
        """I15: a wall-clock jump of more than 2 s against monotonic is a WARN (never a violation)."""
        prev = self.state.last_timer
        self.state.last_timer = (ev.now_mono, ev.now_wall)
        jump = invariants.clock_jump(prev, (ev.now_mono, ev.now_wall)) if prev is not None else None
        if jump is None:
            return []
        self.event("CLOCK_JUMP", {"jump_s": round(jump, 3)}, "WARN")
        return [Alert("WARN", "CLOCK_JUMP", f"wall clock moved {jump:+.1f} s against monotonic (I15)", "clock_jump")]

    def _earlier_session(self, o: Order) -> bool:
        """The order belongs to a leg of an earlier session (only a recovery leftover, §9.6 step 7)."""
        leg = self.state.legs.get(o.leg_id)
        return leg is not None and leg.session < self.session

    def _order_timers(self) -> list[Command]:
        out: list[Command] = []
        for o in list(self.state.orders.values()):
            if o.role == Role.EXTERNAL or o.leg_id not in self.state.legs or self._earlier_session(o):
                continue
            t = self._timers(o)
            if not orders.needs_timer(o, t):
                continue
            fx = orders.on_timer(self.store, o, t, self.now_mono, self.now_wall, token_ok=self.token_ok)
            out += self._order_effects(o, self.state.legs[o.leg_id], fx)
        return out

    def _auto_clears(self) -> None:
        """EXIT_DIFFICULTY clears when its book has no leg left exiting (§8.7)."""
        exiting = {lg.book for lg in self.state.legs_in(LegState.EXITING, LegState.VERIFY_FLAT)}
        for h in self.active_halts("EXIT_DIFFICULTY"):
            if h.scope.startswith("BOOK:") and h.scope[5:] not in exiting:
                self._clear_auto("EXIT_DIFFICULTY", h.scope, "the book's legs are flat")

    def _risk_tick(self) -> list[Command]:
        """The daily stop on realized + bid MTM, once a second (§8.8); an unknown MTM decides nothing unless the
        realized loss alone is past the stop (risk.check_daily_stops)."""
        st = self.state
        if st.last_risk_mono is not None and self.now_mono - st.last_risk_mono < RISK_EVERY_S:
            return []
        st.last_risk_mono = self.now_mono
        books = sorted({x.book for x in st.structures.values() if risk.book_policy(x.book) is Policy.FULL})
        if not books:
            return []
        chk = risk.check_daily_stops([self.book_state(b) for b in books], st.halts, self.settings, self.session)
        out: list[Command] = []
        for trip in chk.trips:
            out += self.apply_halt(trip.halt, alert=False)
            out.append(trip.alert)
        for leg in st.legs_in(LegState.OPEN):
            if leg.book in chk.force_exit:
                out += self.force_exit(leg, "DAILY_STOP")
        return out

    def _heartbeat_tick(self) -> None:
        """HEARTBEAT every 60 s. §10.7: a heartbeat gap covering more than G1_IMPAIRED_GAP_S of a session's entry
        window [ENTRY_START, NO_NEW_AFTER) marks that session IMPAIRED for the G1 strategies: every session the gap
        spans, one the bot never ran in included (a bot left stopped on a day expected to be bad must not drop out
        of G1's sample unseen). So does an ACK halt that keeps a G1 strategy's PAPER book from entering that long."""
        st = self.state
        if st.last_heartbeat_mono is not None and self.now_mono - st.last_heartbeat_mono < HEARTBEAT_EVERY_S:
            return
        prev = st.last_heartbeat_wall
        if prev is not None:
            self._impaired_gap(prev, self.now_wall)
        self._impaired_ack_halts()
        st.last_heartbeat_mono, st.last_heartbeat_wall = self.now_mono, self.now_wall
        self.event("HEARTBEAT", {"phase": self.phase}, "DEBUG")

    def _entry_window_s(self, session: date, start: datetime, end: datetime) -> float:
        """Seconds of the session's entry window [ENTRY_START, NO_NEW_AFTER) inside [start, end]."""
        w0 = ist_datetime(session, self.settings.ENTRY_START)
        w1 = ist_datetime(session, self.settings.NO_NEW_AFTER)
        return max(0.0, (min(end, w1) - max(start, w0)).total_seconds())

    def _impaired_gap(self, prev: datetime, now: datetime) -> None:
        gap = (now - prev).total_seconds()
        strategies = self._g1_strategies()
        if gap <= G1_IMPAIRED_GAP_S or not strategies:
            return
        last = session_of(now)
        day = max(session_of(prev), last - timedelta(days=HEARTBEAT_GAP_SCAN_DAYS))
        while day <= last:
            if self.calendar.is_trading_day(day) and self._entry_window_s(day, prev, now) > G1_IMPAIRED_GAP_S:
                for s in strategies:
                    self._flag_session(day, s, f"heartbeat gap {gap:.0f} s ({iso(prev)} -> {iso(now)})")
            day += timedelta(days=1)

    def _impaired_ack_halts(self) -> None:
        """An ACK halt (operator halts divert to SHADOW and mark the session at once) blocking a G1 strategy's PAPER
        book for more than G1_IMPAIRED_GAP_S of today's entry window censors the sample like a stopped bot. So does
        the laptop's Ctrl+C FLATTEN halt, which _operator_impaired leaves out while flat, when it outlives its stop."""
        if not self.calendar.is_trading_day(self.session):
            return
        for s in self._g1_strategies():
            for h in risk.entry_halts(self.state.halts, account=Account.PAPER, book=f"PAPER:{s}", strategy=s,
                                      underlying="", policy=Policy.FULL):
                if not operator.is_stop_flatten(self, h) and (HaltClears(h.clears) is not HaltClears.ACK
                                                              or risk.is_operator_halt(h)):
                    continue
                blocked = self._entry_window_s(self.session, h.created_wall, self.now_wall)
                if blocked > G1_IMPAIRED_GAP_S:
                    self.flag_impaired(s, f"{h.kind} [{h.scope}] ({h.clears}) blocked PAPER entries for "
                                          f"{blocked:.0f} s")
                    break

    def _tails_tick(self) -> list[Command]:
        out: list[Command] = []
        for lid, tail in list(self.state.tails.items()):
            if self.now_mono >= tail.until_mono:
                del self.state.tails[lid]
                if tail.token not in self.state.held_tokens():
                    out.append(Unsubscribe((tail.token,)))
                continue
            if tail.last_row_mono is not None and self.now_mono - tail.last_row_mono < POST_ROW_EVERY_S:
                continue
            q = self.quotes.fresh(tail.symbol, POST_ROW_EVERY_S, self.now_mono)
            if q is None:
                continue
            tail.last_row_mono = self.now_mono
            leg = self.state.legs.get(lid)
            rb = exits.realizable_bid(q, leg.inst.lot_size)[0] if leg is not None else None
            self.store.add_quote_check(lid, self.now_wall, "POST", **self._quote_fields(q, rb))
        return out

    # ------------------------------------------------------------------------------------------------------------
    # The LIVE self-test (§10.8)
    # ------------------------------------------------------------------------------------------------------------
    def _selftest_due(self) -> bool:
        st = self.state
        if st.selftest_passed or st.selftest_run is not None or st.selftest_attempts >= len(SELFTEST_TIMES):
            return False
        if not (self.ctx.recovered and self.ctx.allow_live and self.calendar.is_trading_day(self.session)):
            return False
        if not any(r["mode"] == "LIVE" for r in st.modes.values()):
            return False
        t = to_ist(self.now_wall).time()
        if t < SELFTEST_TIMES[st.selftest_attempts] or t >= self.settings.NO_NEW_AFTER:
            return False
        return not risk.entry_halts(st.halts, account=Account.LIVE, book=SELFTEST_BOOK, strategy=SELFTEST_STRATEGY,
                                    underlying=NIFTY_SPOT, policy=None)

    def _selftest_tick(self) -> list[Command]:
        run = self.state.selftest_run
        if run is None:
            return self._selftest_start() if self._selftest_due() else []
        waited = self.now_mono - run.started_mono
        if run.stage in ("SPOT", "QUOTE") and waited > SELFTEST_STAGE_S:
            return self._selftest_fail(f"no {run.stage.lower()} quote within {SELFTEST_STAGE_S:.0f} s")
        if run.stage == "ORDER" and waited > SELFTEST_EXPECT_S:
            o = self.state.orders.get(run.order_pk or "")
            if o is None or not o.is_terminal:
                return self._selftest_fail(f"order {run.order_pk} not terminal within {SELFTEST_EXPECT_S:.0f} s")
        if run.stage == "VERIFY":
            if waited > SELFTEST_VERIFY_S:
                proof = self._selftest_proof
                missing = [what for seen, what in (
                    (proof is not None and proof.ws_tag, "no WS order update carried its tag"),
                    (proof is not None and proof.lookup_tag, "orders() never showed its tag"))
                    if not seen]
                return self._selftest_fail(f"order {run.order_pk} was cancelled unfilled, but {' and '.join(missing)} "
                                           f"within {SELFTEST_VERIFY_S:.0f} s (Kite API drift?)")
            return self._selftest_verify()
        return []

    def _selftest_start(self) -> list[Command]:
        attempt = self.state.selftest_attempts + 1
        run = self.state.selftest_run = SelfTestRun(attempt, "SPOT", self.now_mono)
        self.event("SELFTEST", {"attempt": attempt, "outcome": "STARTED"})
        spot = self.quotes.fresh(NIFTY_SPOT, self.settings.UNDERLYING_MAX_AGE_S, self.now_mono)
        if spot is None or not _pos(spot.ltp):
            run.request_id = self._rid("selftest")
            return [FetchQuotes(run.request_id, (NIFTY_SPOT,), int(Priority.ENTRY_LIVE))]
        return self._selftest_select(spot)

    def _selftest_select(self, spot: Quote) -> list[Command]:
        run = self.state.selftest_run
        inst_store = self.instruments
        if inst_store is None:
            return self._selftest_fail("today's instruments are not loaded")
        c = inst_store.select_contract(NIFTY_SPOT, "CE", StrikePref.ATM, spot.ltp, self.session)
        if isinstance(c, Refusal):
            return self._selftest_fail(f"no contract: {c.detail}")
        run.inst, run.stage, run.started_mono = c, "QUOTE", self.now_mono
        run.request_id = self._rid("selftest")
        return [FetchQuotes(run.request_id, (c.symbol,), int(Priority.ENTRY_LIVE))]

    def _selftest_quote(self, ev: QuoteResult) -> list[Command]:
        run = self.state.selftest_run
        if run.stage == "SPOT":
            spot = self.quotes.fresh(NIFTY_SPOT, self.settings.UNDERLYING_MAX_AGE_S, self.now_mono) if ev.ok else None
            if spot is None or not _pos(spot.ltp):
                return self._selftest_fail(f"no NIFTY spot ({ev.error or 'missing'})")
            return self._selftest_select(spot)
        if run.stage != "QUOTE" or run.inst is None:
            return []
        q = self.quotes.fresh(run.inst.symbol, self.settings.QUOTE_MAX_AGE_ENTRY_S, self.now_mono) if ev.ok else None
        valid_from = self.calendar.quotes_valid_from(self.session)
        if q is not None and (valid_from is None or q.recv_wall < valid_from
                              or (q.exch_wall is not None and q.exch_wall < valid_from)):
            # options have no pre-open: a book stamped before open + 30 s is a stale carry-over, and a price under
            # its bid could cross the real ask
            return self._selftest_fail(f"{run.inst.symbol} quote is stamped before {valid_from}: stale carry-over")
        limit = exits.selftest_limit(q, run.inst.tick)
        if q is None or limit is None:
            return self._selftest_fail(f"no usable {run.inst.symbol} quote ({ev.error or 'no bid below which to sit'})")
        return self._selftest_place(q, limit)

    def _selftest_place(self, q: Quote, limit: float) -> list[Command]:
        run = self.state.selftest_run
        inst = run.inst
        intent = Intent(intent_id=f"st{self.session:%y%m%d}{run.attempt}{uuid.uuid4().hex[:5]}",
                        strategy=SELFTEST_STRATEGY, strategy_version="1", underlying=NIFTY_SPOT,
                        direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ATM,
                        expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=inst.strike, recheck=Recheck(-math.inf, math.inf),
                        ttl_s=10.0, created_mono=self.now_mono, created_wall=self.now_wall,
                        reason={"selftest_attempt": run.attempt})
        self.store.insert_intent(intent, self.session)
        st = self.reserve(intent, book=SELFTEST_BOOK, account=Account.LIVE, policy=Policy.FULL)
        age = exits.receipt_age(q, self.now_mono)
        dq = DecisionQuote(q.bid, q.ask, q.mid(), int(age * 1000) if math.isfinite(age) else 0, q.source, q.ltp, None)
        leg, o, cmds = self.begin_entry(st, inst=inst, qty=inst.lot_size, limit_px=limit, decision_quote=dq,
                                        decision={"selftest_attempt": run.attempt, "limit": limit, "bid": q.bid},
                                        role=Role.SELFTEST)
        run.stage, run.order_pk, run.structure_id = "ORDER", o.order_pk, st.structure_id
        run.started_mono = self.now_mono
        self._selftest_proof = _SelfTestProof(o.order_pk)
        self.event("SELFTEST", {"attempt": run.attempt, "outcome": "SENT", "symbol": inst.symbol, "limit": limit,
                                "bid": q.bid}, order_pk=o.order_pk, structure_id=st.structure_id)
        return cmds

    def _selftest_order_done(self, o: Order) -> list[Command]:
        run = self.state.selftest_run
        if o.filled_qty > 0:
            self.state.selftest_run, self._selftest_proof = None, None
            self.state.selftest_attempts = max(self.state.selftest_attempts + 1, len(SELFTEST_TIMES))
            self.event("SELFTEST", {"order_pk": o.order_pk, "outcome": "FILLED", "filled": o.filled_qty}, "CRITICAL")
            return self.add_halt("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE",
                                 f"the self-test order {o.order_pk} FILLED {o.filled_qty}: exited at once")
        if run is None or run.order_pk != o.order_pk:
            return []                    # the attempt already ended (timeout)
        took = self.now_mono - run.started_mono
        if o.state is OrderState.CANCELLED and took <= SELFTEST_EXPECT_S:
            proof = self._selftest_proof
            if proof is None or proof.order_pk != o.order_pk:
                proof = self._selftest_proof = _SelfTestProof(o.order_pk)
            proof.latency_ms = int(took * 1000)
            run.stage, run.started_mono = "VERIFY", self.now_mono
            return self._selftest_verify()
        return self._selftest_fail(f"order {o.order_pk} ended {o.state} ({o.status_msg or 'no message'})")

    def _selftest_note(self, o: Order, ev: Event) -> None:
        """Record what an order event shows about the self-test order's tag: a WS update carrying it, or a lookup
        that found the order by it (a report with a fill proves nothing: that attempt fails or exits)."""
        proof = self._selftest_proof
        if proof is None or o.order_pk != proof.order_pk:
            return
        if isinstance(ev, OrderSnapshot) and ev.source == "WS" and ev.tag == o.order_pk and ev.filled_qty == 0:
            proof.ws_tag = True
        elif (isinstance(ev, LookupResult) and ev.ok and ev.found is not None and ev.found.tag == o.order_pk
              and ev.found.filled_qty == 0):
            proof.lookup_tag = True

    def _selftest_seen_in(self, ev: OrdersSnapshot) -> None:
        """An orders() read of the LIVE account that lists the self-test order under its tag proves the same."""
        proof = self._selftest_proof
        if proof is not None and Account(ev.account) is Account.LIVE and any(
                b.tag == proof.order_pk and b.filled_qty == 0 for b in ev.orders):
            proof.lookup_tag = True

    def _selftest_verify(self) -> list[Command]:
        """Stage VERIFY (the IOC was CANCELLED unfilled in time): PASSED once its tag was seen both in a WS order update
        and in orders(); meanwhile orders() is asked for the tag (LookupTag) every SELFTEST_LOOKUP_EVERY_S. Only what
        was seen is claimed. A miss fails the attempt at SELFTEST_VERIFY_S (_selftest_tick)."""
        run, proof = self.state.selftest_run, self._selftest_proof
        if run is None or run.stage != "VERIFY" or proof is None or proof.order_pk != run.order_pk:
            return []
        if proof.ws_tag and proof.lookup_tag:
            self.state.selftest_run, self._selftest_proof = None, None
            self.state.selftest_passed = True
            self.event("SELFTEST", {"attempt": run.attempt, "outcome": "PASSED", "latency_ms": proof.latency_ms,
                                    "order_pk": proof.order_pk, "ws_tag": True, "orders_tag": True})
            return [Alert("INFO", "SELFTEST", f"LIVE self-test passed ({proof.latency_ms} ms): the IOC BUY was "
                          "accepted and cancelled unfilled, and its tag came back in a WS order update and in orders()",
                          "selftest")]
        if not proof.lookup_tag and (proof.lookup_sent is None
                                     or self.now_mono - proof.lookup_sent >= SELFTEST_LOOKUP_EVERY_S):
            proof.lookup_sent = self.now_mono
            return [LookupTag(Account.LIVE, proof.order_pk)]
        return []

    def _selftest_fail(self, detail: str) -> list[Command]:
        run = self.state.selftest_run
        self.state.selftest_run, self._selftest_proof = None, None
        self.state.selftest_attempts += 1
        attempt = run.attempt if run is not None else self.state.selftest_attempts
        self.event("SELFTEST", {"attempt": attempt, "outcome": "FAILED", "detail": detail[:300]}, "WARN")
        if self.state.selftest_attempts >= len(SELFTEST_TIMES):
            return self.add_halt("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE", f"LIVE self-test failed twice: {detail}")
        return [Alert("WARN", "SELFTEST", f"LIVE self-test attempt {attempt} failed ({detail}); retry at "
                      f"{SELFTEST_TIMES[self.state.selftest_attempts]:%H:%M:%S}", "selftest")]

    # ------------------------------------------------------------------------------------------------------------
    # EOD timeline (§12.5)
    # ------------------------------------------------------------------------------------------------------------
    def eod_steps(self) -> list[tuple[str, datetime]]:
        sq = ist_datetime(self.session, self.settings.BROKER_SQUAREOFF_AT)
        own = [("FLATTEN", self.eod.flatten), ("BACKSTOP", self.eod.backstop), ("STOP_SENDING", self.eod.stop_sending)]
        return own + [(name, sq + timedelta(seconds=off)) for name, off in EOD_AFTER_SQUAREOFF_S]

    def _eod_tick(self) -> list[Command]:
        # after recovery only: a restart at 15:41 must reconcile before it exits (decide() still flattens at 15:15)
        if not self.ctx.recovered or not self.calendar.is_trading_day(self.session):
            return []
        out: list[Command] = []
        for step, at in self.eod_steps():
            if step in self.state.eod_done or self.now_wall < at:
                continue
            if step in LATE_HELD_STEPS and self._late_hold():
                break                    # a late start: the truth check and what follows wait for the square-off
            if self._eod_waits(step, at):
                break                    # due together with the steps before it: their answers and files first
            self.state.eod_done.add(step)
            self.state.eod_ran[step] = (self.now_mono, self.now_wall)
            self.event("PHASE", {"phase": "DONE" if step == "DONE" else "EOD", "step": step, "at": iso(at)})
            out += getattr(self, f"_eod_{step.lower()}")()
        return out

    def _eod_waits(self, step: str, at: datetime) -> bool:
        """A due step waits for an earlier one it reads (EOD_WAITS_FOR) that ran when this one was due already (they
        fell due together: a start after 15:40, or a late start's hold that ended after their times) while what that
        one sent or marked is still out (_eod_out), at most the normal timeline's gap between the two from when it
        ran. They all ran in one Timer before, and core_loop stops on DONE's Exit: the tradebook answers were never
        handled and EOD_TASKS' files never written (review round 2 follow-up). On a normal day every earlier step ran
        before `at`, so nothing waits."""
        offset = dict(EOD_AFTER_SQUAREOFF_S)
        for before in EOD_WAITS_FOR.get(step, ()):
            ran = self.state.eod_ran.get(before)
            if (ran is not None and ran[1] >= at and self.now_mono - ran[0] < offset[step] - offset[before]
                    and self._eod_out(before)):
                return True
        return False

    def _eod_out(self, step: str) -> bool:
        """What an EOD step sent or marked is not in yet: TRUTH's tradebook reads and contract notes, BAR_CHECK's bars,
        EOD_TASKS' files (_publish writes them at the start of the next handle(), or after a failed write's pause)."""
        st = self.state
        if step == "TRUTH":
            return "TRUTH" in st.tradebook_purpose.values() or bool(st.vcn_pending)
        if step == "BAR_CHECK":
            return bool(st.bar_requests)
        return bool(self._pub & EOD_FILES)

    def _not_flat(self) -> list[str]:
        return [f"{lg.book} {lg.inst.symbol} {lg.state}" for lg in self.state.legs_in(*_HELD)]

    def _eod_flatten(self) -> list[Command]:
        out: list[Command] = []
        for leg in self.state.legs_in(LegState.OPEN):
            out += self.force_exit(leg, "EOD_FLATTEN")
        return out

    def _eod_backstop(self) -> list[Command]:
        out: list[Command] = []
        left = self._not_flat()
        if left:
            out.append(Alert("CRITICAL", "NOT_FLAT", f"not flat at EOD_BACKSTOP: {', '.join(left)}; exits price at "
                             "band >= 3", f"eod:not_flat:{self.session}"))
        for acct in ACCOUNTS:
            out += self.start_recon(acct)
        return out

    def _eod_stop_sending(self) -> list[Command]:
        left = self._not_flat()
        if not left:
            return []
        return [Alert("CRITICAL", "NOT_FLAT", f"not flat at STOP_SENDING: {', '.join(left)}; no more orders, the "
                      "broker square-off is expected", f"eod:stop_sending:{self.session}")]

    def _eod_recon_1(self) -> list[Command]:
        out: list[Command] = []
        for acct in ACCOUNTS:
            out += self.start_recon(acct)
        return out

    _eod_recon_2 = _eod_recon_1

    def _eod_truth(self) -> list[Command]:
        """15:33: the tradebook of every account for the truth check, and the contract-note check (§9.8, §8.1)."""
        out: list[Command] = []
        for acct in ACCOUNTS:
            if acct is Account.LIVE and not self._vcn_orders(acct, 1):
                continue                 # no LIVE fills today: nothing of the bot's to verify on the real account
            self.state.tradebook_purpose[acct] = "TRUTH"
            out.append(FetchTradebook(acct))
        for acct, cap in ((Account.LIVE, None), (Account.PAPER, VCN_SAMPLE_MAX)):
            orders_ = self._vcn_orders(acct, cap)
            if orders_:
                self.state.vcn_pending.add(acct)
                out.append(ContractNote(acct, tuple(orders_)))
        return out

    def _vcn_orders(self, acct: Account, cap: int | None) -> list[ContractNoteOrder]:
        out = []
        for o in sorted(self.state.orders.values(), key=lambda x: x.order_pk):
            leg = self.state.legs.get(o.leg_id)
            if (Account(o.account) is not acct or leg is None or leg.session != self.session or o.filled_qty <= 0
                    or not o.broker_order_id or not _pos(o.avg_fill_px)):
                continue
            out.append(ContractNoteOrder(o.broker_order_id, leg.inst.exchange, leg.inst.tradingsymbol, Side(o.side),
                                         o.filled_qty, o.avg_fill_px))
            if cap is not None and len(out) >= cap:
                break
        return out

    def _truth_check(self, acct: Account, ev: TradebookSnapshot) -> list[Command]:
        # both sides by structure session (review round 2): a leftover of an earlier session settled at today's start
        #   is booked to its own session, so its settlement is checked against its own trade, not today's ledger
        ti = ledger.truth_inputs(self.store, acct, self.session)
        tc = recon.eod_truth_check(acct, self.session, ti.ledger_by_book, ti.order_books,
                                   ev.trades if ev.ok else None, parts=ti.parts, carried=ti.carried)
        for e in tc.events:
            self.event(e.kind, dict(e.payload), e.severity, account=acct)
        if not tc.ok and acct is not Account.LIVE:
            self.event("EOD_TRUTH_MISMATCH", {"account": str(acct), "books": list(tc.g1_mismatch_books)}, "CRITICAL",
                       account=acct)
        return list(tc.alerts)

    def _on_contract_note(self, ev: ContractNoteResult) -> list[Command]:
        acct = Account(ev.account)
        self.state.vcn_pending.discard(acct)
        if not ev.ok:
            if acct is not Account.LIVE:
                if not self.state.vcn_sample_unsupported:
                    self.state.vcn_sample_unsupported = True
                    self.event("CHARGES_CHECK", {"account": str(acct), "result": "VCN_PAPER_UNSUPPORTED"})
                return []
            self.event("CHARGES_CHECK", {"account": str(acct), "result": "UNAVAILABLE"}, "WARN", account=acct)
            return [Alert("WARN", "CHARGES_CHECK", "LIVE virtual contract note unavailable today", "vcn:live")]
        by_id = {o.broker_order_id: o for o in self.state.orders.values() if o.broker_order_id}
        drift = []
        for row in ev.rows:
            o = by_id.get(row.broker_order_id)
            if o is None or o.filled_qty <= 0 or not _pos(o.avg_fill_px):
                continue
            model = order_charges(OrderFills(Side(o.side), o.filled_qty, o.filled_qty * o.avg_fill_px,
                                             o.external_kind), self.rates).total
            if abs(float(row.total) - model) > VCN_TOLERANCE_RS:
                drift.append({"broker_order_id": row.broker_order_id, "note": float(row.total), "model": model})
        self.event("CHARGES_DRIFT" if drift else "CHARGES_CHECK",
                   {"account": str(acct), "orders": len(ev.rows), "drift": drift[:20]},
                   "WARN" if drift else "INFO", account=acct)
        if drift:
            return [Alert("WARN", "CHARGES_DRIFT", f"{acct}: {len(drift)} order(s) differ from the charges model by "
                          f"more than Rs {VCN_TOLERANCE_RS:.0f}: a new RateSet is needed", f"vcn:drift:{acct}")]
        return []

    def _eod_bar_check(self) -> list[Command]:
        """15:36: Kite's 09:15 candle for every symbol opening_drive fired on plus 20 seeded samples (§10.3)."""
        inst = self.instruments
        bounds = self.calendar.session_bounds(self.session)
        if inst is None or bounds is None:
            self.event("BAR_CHECK", {"skipped": "no instruments" if inst is None else "no session"}, "WARN")
            return []
        fired = [r["underlying"] for r in self.store.query(
            "SELECT DISTINCT underlying FROM intents WHERE session = ? AND strategy = 'opening_drive' AND "
            "is_placebo = 0", (self.session.isoformat(),))]
        s = self.settings
        universe = inst.universe(self.ban_list or (), s.STOCK_ALLOWLIST, s.STOCK_DENYLIST)
        out: list[Command] = []
        start = bounds[0]
        for sym in bar_check_symbols(fired, universe, self.session):
            u = inst.underlying(sym)
            if u is None:
                continue
            rid = self._rid("bar")
            self.state.bar_requests[rid] = sym
            out.append(FetchHistorical(rid, sym, u.token, start, start + timedelta(minutes=5)))
        return out

    def _on_historical(self, ev: HistoricalResult) -> list[Command]:
        sym = self.state.bar_requests.pop(ev.request_id, None)
        bounds = self.calendar.session_bounds(self.session)
        if sym is None or bounds is None:
            return []
        start = bounds[0]
        live = find_bar(self.store.bars(sym, source="LIVE", start_from=start, start_to=start), start)
        hist = find_bar(ev.bars, start) if ev.ok else None
        bc = check_bar(live, hist)
        self.store.put_bar_check(self.session, sym, start, bc.ok, bc.detail())
        self.event("BAR_CHECK", {"symbol": sym, "ok": bc.ok, "reasons": list(bc.reasons)}, "INFO" if bc.ok else "WARN")
        return []

    def _eod_eod_tasks(self) -> list[Command]:
        """15:38: nightly G1, live review, weekly prune; exports, the daily report and trades_90d are written once
        this transaction committed (next handle())."""
        out: list[Command] = []
        run = g1.run_nightly(self.store, self.now_wall, self.now_mono)
        out += list(run.alerts)
        if run.mode_changes:
            self.reload_modes()
        out += self._live_review()
        if self.calendar.is_last_session_of_week(self.session) and self.ctx.exports_dir is not None:
            res = self.store.prune(self.now_wall, self.ctx.exports_dir)
            self.event("PRUNE", {"quote_checks": res.quote_checks_deleted,
                                 "broker_snapshots": res.broker_snapshots_deleted})
        self._pub |= {"g1", "trades_90d", "exports", "report"}
        return out

    def _eod_done(self) -> list[Command]:
        if self.state.exit_sent:
            return []
        self.state.exit_sent = True
        operator.release_stop_flatten(self, "the trading day is done")
        return [Exit(0)]

    # ------------------------------------------------------------------------------------------------------------
    # SIGTERM (§9.5 step 17): stop entries, cancel entry orders, let in-flight exits finish, exit 0, never flatten
    # ------------------------------------------------------------------------------------------------------------
    def _on_shutdown(self, ev: Shutdown) -> list[Command]:
        st = self.state
        if st.stopping:
            return []
        st.stopping, st.stop_mono = True, self.now_mono
        self.ctx.stop_mono = self.now_mono               # survives an engine reload (Engine.__init__)
        self.event("SHUTDOWN", {"reason": ev.reason, "phase": "STOPPING"}, "WARN")
        # entries are refused from now on (stopping): the laptop's Ctrl+C FLATTEN halt has done its job once flat
        operator.release_stop_flatten(self, f"stopping ({ev.reason})")
        out: list[Command] = []
        for o in list(st.orders.values()):
            if o.role in (Role.ENTRY, Role.SELFTEST) and o.state is OrderState.WORKING:
                out += self.request_cancel(o, "SHUTDOWN")
        return out + self._shutdown_tick()

    def _shutdown_tick(self) -> list[Command]:
        st = self.state
        if not st.stopping or st.exit_sent:
            return []
        busy = st.working_orders()
        if busy and self.now_mono - st.stop_mono < SHUTDOWN_WAIT_S:
            return []
        st.exit_sent = True
        self.event("SHUTDOWN", {"phase": "EXIT", "still_working": [o.order_pk for o in busy]},
                   "WARN" if busy else "INFO")
        return [Exit(0)]

    # ------------------------------------------------------------------------------------------------------------
    # pub/ (the dashboard's only input) and the EOD files, written after the previous transaction committed
    # ------------------------------------------------------------------------------------------------------------
    def _publish(self) -> list[Command]:
        """Write what committed events marked (pub/ files, exports, the daily report) before the next event.
        Observability only: a failed write is a WARN, never a halt. After a failure every write pauses PUB_RETRY_S
        (a full disk must not log at the event rate) and a marked file is marked again, until PUB_MAX_TRIES
        failures in a row; then its next change retries it. status.json and the events tail are never marked: they
        are rewritten at their own cadence once the pause ends."""
        jobs: list[tuple[str, Callable[[], Any], Callable[[], None] | None]] = []
        pub, now = self.ctx.pub_dir, self.now_mono
        if self._pub_retry_at is not None and now < self._pub_retry_at:
            return []
        self._pub_retry_at = None
        todo, self._pub = self._pub, set()
        trade_ids, self._pub_trades = sorted(self._pub_trades), set()
        if pub is not None:
            if "trades_today" in todo:
                jobs.append(("trade files", lambda: self._republish_trades(pub),
                             lambda: self._pub.add("trades_today")))
            if trade_ids:
                jobs.append(("trades_today.json",
                             lambda: ledger.publish_trades_today(self.store, pub, self.session, self.now_wall),
                             lambda: self._pub_trades.update(trade_ids)))
                for sid in trade_ids:
                    jobs.append((f"trade/{sid}.json",
                                 lambda sid=sid: ledger.publish_trade_detail(self.store, pub, sid, self.now_wall),
                                 lambda sid=sid: self._pub_trades.add(sid)))
            marked = (("acks", "acks.json", lambda: ledger.publish_acks(self.store, pub, self.now_wall)),
                      ("g1", "g1.json", lambda: ledger.publish_g1(pub, g1.scorecards(self.store), self.now_wall)),
                      ("trades_90d", "trades_90d.json",
                       lambda: ledger.publish_trades_90d(self.store, pub, self.session, self.now_wall)))
            for key, name, fn in marked:
                if key in todo:
                    jobs.append((name, fn, lambda key=key: self._pub.add(key)))
            if self._last_status is None or now - self._last_status >= STATUS_EVERY_S:
                self._last_status = now
                jobs.append(("status.json", lambda: ledger.write_pub_json(pub, "status.json", self.status_doc()),
                             None))
            if self._last_events is None or now - self._last_events >= EVENTS_TAIL_EVERY_S:
                self._last_events = now
                jobs.append(("events_tail.json", self._publish_events, None))
        if "exports" in todo and self.ctx.exports_dir is not None:
            jobs.append(("exports", lambda: ledger.export_session(self.store, self.ctx.exports_dir, self.session),
                         lambda: self._pub.add("exports")))
        out: list[Command] = []
        if "report" in todo:
            jobs.append(("report", lambda: self._daily_report(out), lambda: self._pub.add("report")))
        for name, job, again in jobs:
            try:
                job()
                self._pub_fails.pop(name, None)
            except Exception as exc:  # observability only: a failed write is a WARN and never stops trading
                n = self._pub_fails[name] = self._pub_fails.get(name, 0) + 1
                log.exception("writing %s failed (%d in a row)", name, n)
                out.append(Alert("WARN", "PUB_WRITE", f"writing {name} failed: {exc!r}"[:300], f"pub:{name}"))
                self._pub_retry_at = now + PUB_RETRY_S
                if again is not None and n < PUB_MAX_TRIES:
                    again()
        return out

    def _republish_trades(self, pub: Any) -> None:
        """After a load: trades_today.json and today's trade/<id>.json from the ledger (a crash or an Exit right
        after a close never ran the next event's write)."""
        ledger.publish_trades_today(self.store, pub, self.session, self.now_wall)
        for t in self.store.trades(session=self.session):
            ledger.publish_trade_detail(self.store, pub, int(t["structure_id"]), self.now_wall)

    def _publish_events(self) -> None:
        self._events_seq = ledger.publish_events_tail(self.store, self.ctx.pub_dir, self.now_wall,
                                                      last_seq=self._events_seq)

    def _daily_report(self, out: list[Command]) -> None:
        """The Telegram report first (a retry after a failed file write repeats it inside the 5 min dedup)."""
        report = ledger.daily_report(self.store, self.session, self.now_wall)
        out.append(Alert("INFO", "DAILY_REPORT", report.telegram, f"daily_report:{self.session}"))
        if self.ctx.reports_dir is not None:
            ledger.write_daily_report(report, self.ctx.reports_dir)

    def _status_books(self) -> list[str]:
        books = {x.book for x in self.state.structures.values() if risk.parse_book(x.book) is not None}
        for s, r in self.state.modes.items():
            books.update(risk.books_for(s, r["mode"]))
            if r["mode"] in ("PAPER", "LIVE"):
                books.add(f"PLACEBO:{s}")
        return sorted(books)

    def status_doc(self) -> dict[str, Any]:
        """pub/status.json (DETAIL §4.7). MTM is at the realizable bid, None when unknown; LTP is info only."""
        now, wall, s = self.now_mono, self.now_wall, self.settings
        h = self.ctx.feed_health()
        last = self.state.last_timer
        accounts = {}
        for acct, bv in self.state.broker.items():
            m = bv.margins
            accounts[acct.value] = {
                "recon": bv.recon.status,
                "recon_age_s": None if bv.last_recon_mono is None else round(now - bv.last_recon_mono, 1),
                "broker_positions": [{"symbol": r.symbol, "tradingsymbol": r.tradingsymbol, "product": r.product,
                                      "qty": r.net_qty} for r in (bv.positions.rows if bv.positions else ())],
                "available_rs": m.margins.available_rs if m is not None and m.ok and m.margins is not None else None}
        books = {}
        for book in self._status_books():
            b = self.book_state(book)
            books[book] = {
                "policy": str(b.policy), "realized_net": b.realized_net, "mtm_net": b.mtm_net, "entries": b.entries,
                "max_entries": s.MAX_ENTRIES_PER_DAY, "open": b.open, "reserved": b.reserved,
                "daily_stop_room": risk.daily_stop_room(b.realized_net, b.mtm_net, s.DAILY_STOP_RS),
                "worst_case_room": risk.worst_case_room(b.realized_net, b.exposures_rs, s.MAX_WORST_CASE_DAY_LOSS_RS),
                "cooldowns": {u: to_ist(t).strftime("%H:%M:%S") for u, t in sorted(b.cooldown_until.items())
                              if t > wall}}
        strategies = []
        for name, r in sorted(self.state.modes.items()):
            strategies.append({"name": name, "version": r["version"], "mode": r["mode"],
                               "pending_mode": r["pending_mode"],
                               "trial": g1.trial_summary(self.store, name, r["version"])})
        return {
            "ts": iso(wall), "session": self.session.isoformat(), "phase": self.phase,
            "heartbeat_age_s": None if last is None else round(max(0.0, now - last[0]), 2),
            "allow_live": self.ctx.allow_live is True,
            "entry_halts": [{"halt_id": x.halt_id, "scope": x.scope, "kind": x.kind, "reason": x.reason,
                             "since": iso(x.created_wall), "clears": str(x.clears)} for x in self.state.halts],
            "feed": {"connected": h.connected,
                     "last_tick_age_s": (None if h.last_underlying_tick_mono is None
                                         else round(now - h.last_underlying_tick_mono, 2)),
                     "reconnects": h.reconnects, "degraded": h.degraded,
                     "warm": bool(h.connected and h.connected_since_mono is not None
                                  and now - h.connected_since_mono >= s.FEED_WARM_S)},
            "accounts": accounts, "books": books,
            "positions": [self._position_doc(leg) for leg in self.state.legs_in(*_HELD) if leg.entry_filled > 0],
            "strategies": strategies, "pending_commands": len(self.state.flatten),
            "unacked_critical": self.ctx.unacked_critical()}

    def _position_doc(self, leg: Leg) -> dict[str, Any]:
        st = self.state.structures[leg.structure_id]
        p = st.exit_params
        q = self.quotes.last(leg.inst.symbol)
        open_qty = leg.open_qty
        rb = exits.realizable_bid(q, open_qty)[0] if q is not None and open_qty > 0 else None
        entry = p.entry_avg if p is not None else None
        age = exits.receipt_age(q, self.now_mono) if q is not None else math.inf
        return {
            "structure_id": st.structure_id, "book": st.book, "symbol": leg.inst.symbol, "qty": open_qty,
            "entry_avg": entry, "bid": q.bid if q else None, "ask": q.ask if q else None,
            "age_ms": int(age * 1000) if math.isfinite(age) else None, "src": q.source if q else None,
            "rb": _num(rb), "mtm_net": self.leg_mtm(leg), "stop": _num(leg.stop_px), "lock_armed": leg.lock_armed,
            "tp": p.tp_px if p is not None else None,
            "time_stop_in_s": int((p.time_stop_at - self.now_wall).total_seconds()) if p is not None else None,
            "mfe_pct": round(leg.peak_rb / entry * 100 - 100, 3) if _pos(leg.peak_rb) and _pos(entry) else None,
            "mae_pct": round(leg.trough_rb / entry * 100 - 100, 3) if _pos(leg.trough_rb) and _pos(entry) else None,
            "state": str(leg.state), "exit_reason": leg.exit_reason, "ltp_info": _num(q.ltp) if q else None}
