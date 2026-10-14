"""Order state machine, apply_cum and the order timers (spec §5.1-5.2, §5.9-5.10; DETAIL §5.1-5.2, §5.9-5.10).

Every bot order is LIMIT + IOC + MIS. create_order() inserts it as SUBMITTING in the same transaction that returns
its Place command (write-ahead), so the row exists before any HTTP call. NEW is only for phase-2 basket legs that
are planned but not yet dispatched (create_order(planned=True), then submit()).

TRANSITIONS is the normative (state, event) table. Every handler refuses a pair that is not in it with
IllegalTransition (an InvariantViolation) and checks that it left the order in a state the table allows. Broker
answers that arrive late, twice or out of order are listed (they happen) and change nothing:
- a PlaceResult after a WS snapshot or a tag lookup already adopted the order;
- a snapshot older than what the order already shows (non-terminal status, filled qty not higher);
- a LookupResult or CancelResult that arrives after the order resolved.
A terminal order never changes (DB trigger, I9). A later report that contradicts it writes one INVARIANT_VIOLATION
event, a CRITICAL alert and a PollOrder, and sets Effects.unresolved when the order's fills are in doubt.

Contract with the engine (core thread, inside store.tx()):
- each function mutates the Order (and its Leg on a fill), writes the rows and an events row per change, and
  returns Effects: commands to dispatch after the commit, halts to raise, became_terminal (the engine then runs the
  role's reaction: positions.on_entry_terminal, the exit ladder, the self-test check), filled (qty newly filled) and
  unresolved (call positions.mark_unresolved);
- OrderTimers keep each order's monotonic timers in RAM. Call on_timer() on every Timer for each order where
  needs_timer() is true; after Engine.load use timers_after_reload(), which also re-polls (§3.3).

Quantities only grow (apply_cum). Fill prices come from the broker's cumulative average, refined later by TRADE rows
(FetchTrades), which replace the CUM rows once they explain filled_qty (store.effective_fills).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping, MutableMapping

from titan2.clock import session_of
from titan2.core.events import (
    CANCEL_OUTCOMES, ORDER_STATUSES, PLACE_OUTCOMES, Alert, Cancel, CancelResult, Command, FetchTrades, LookupResult,
    LookupTag, OrderSnapshot, Place, PlaceResult, PollOrder, Timer, TradesResult,
)
from titan2.core.recon import HaltRequest
from titan2.core.risk import HALT_RULES
from titan2.model import (
    PRICE_EPS, ROLE_SIDE, TERMINAL_ORDER, Account, DecisionQuote, Fill, InvariantViolation, Leg, LegState, Order,
    OrderRequest, OrderState, Role, Side, _StrEnum, ceil_tick, floor_tick, make_tag, order_request_problem,
    parse_tag,
)
from titan2.settings import (
    ENTRY_MAX_OVER_MID, FILL_PX_MAX_DEV, LOOKUP_AT_S, MAX_EXIT_DISCOUNT, ORDER_CANCEL_AFTER_S, ORDER_POLL_AFTER_S,
    ORDER_POLL_EVERY_S, ORDER_STUCK_AFTER_S,
)

if TYPE_CHECKING:
    from titan2.store import Store

__all__ = [
    "BLOCK_RE", "Effects", "IllegalTransition", "MARGIN_RE", "OrderEvent", "OrderTimers", "TRANSITIONS",
    "adopted_report", "allowed", "apply_cum", "create_order", "event_kind", "find_order", "handle", "needs_timer",
    "on_cancel_result", "on_lookup_result", "on_place_result", "on_snapshot", "on_timer", "on_trades_result",
    "reject_effects", "request_cancel", "session_ended", "submit", "timers_after_reload", "whole_ms",
]

# Fault-handling limits of §5.2 that DETAIL §8.4 does not list as code constants.
STUCK_POLL_EVERY_S = 2.0            # CANCEL_REQUESTED once stuck: poll every 2 s
LOOKUP_RETRY_S = 2.0                # a failed or inconclusive tag lookup is retried after 2 s
LOOKUP_STUCK_AFTER_S = 30.0         # still unresolved this long after lookups began: ORDER_STUCK
# NOT_PLACED is the bot's own conclusion, so it must not come early. §5.2 says two misses (+1 s, +3 s); but Kite can
#   accept an order whose answer is lost and leave it out of orders() for longer (orderbook lag), and a wrong
#   NOT_PLACED sells an exit a second time (a naked short) or abandons a filled entry (post-build review). So the tag
#   is looked up again at +7 s and +15 s, and only NOT_PLACED_LOOKUPS misses, the last >= NOT_PLACED_AFTER_S after
#   submit, make the order NOT_PLACED. Every account uses the same schedule (paper keeps live's timing).
LATE_LOOKUP_AT_S = (7.0, 15.0)
LOOKUP_SCHEDULE_S = (*LOOKUP_AT_S, *LATE_LOOKUP_AT_S)    # tag lookups after an AMBIGUOUS answer (or a reload)
NOT_PLACED_LOOKUPS = len(LOOKUP_SCHEDULE_S)
NOT_PLACED_AFTER_S = LOOKUP_SCHEDULE_S[-1]
THROTTLE_BACKOFF_S = (1.0, 2.0, 4.0, 8.0)  # AMBIGUOUS with HTTP 429: lookups after gaps of 1, 2, 4 and 8 s
PLACE_ANSWER_WAIT_S = 10.0          # SUBMITTING with no answer this long (lost PlaceResult): start tag lookups
# A place that may have reached Kite (AMBIGUOUS: a client timeout, a dropped connection, a 5xx; or no answer at all)
#   can be registered by the OMS long after its answer: KiteConnect's POST is synchronous and a timeout says nothing
#   about when the OMS takes the order (post-build review, broker API). Such an order stays UNKNOWN, looked up every
#   LATE_LOOKUP_EVERY_S, until LATE_ACCEPT_S after its answer (OrderTimers.late_until); only a miss from then on
#   makes it NOT_PLACED: a miss whose orders() read was asked at or after that time (LookupResult.req_mono), since
#   a read asked inside the window can be answered after it on a slow lane (review round 3). Until then no other
#   attempt goes out on its leg (I4), so a late exit is never sold twice, and a late WS update, poll or orders() row
#   adopts it. An in-process reload keeps the deadline the answer set (the engine remembers it for the process,
#   EngineContext.late_until; without one it counts from the submit + PLACE_TIMEOUT_S, the answer's latest normal
#   time); a restart starts it again.
# Decision B2 (2026-10-11): keep 30 s for paper and the first LIVE weeks. A shorter window lets a second SELL go out
#   while the first may still be accepted: a double sell, i.e. an uncapped naked short. Waiting costs at most a 1-lot
#   long's premium, and the day's worst case is capped at Rs 50K (Rs 40K before 2026-10-13). Paper never times out, so the burn-in cannot measure it. Shorten
#   (to about 15 s) only with LIVE evidence that orders() lists new orders within a few seconds: every ambiguous place
#   on LIVE is an ORDER_PLACE_RESULT WARN event with outcome AMBIGUOUS, and the gap to the order's first adopting
#   broker report (an ORDER_UPDATE from LOOKUP, POLL or WS) is the measurement. Any change is a reviewed code change,
#   never a setting (docs/RUNBOOK.md §10, Decisions).
LATE_ACCEPT_S = 30.0
LATE_LOOKUP_EVERY_S = 5.0
PLACE_TIMEOUT_S = 5.0               # titan2.token.KITE_TIMEOUT_S: the client timeout an AMBIGUOUS answer comes from
TRADES_RETRY_S = 2.0                # a failed FetchTrades is retried, at most TRADES_MAX_TRIES requests in all
TRADES_MAX_TRIES = 3
REJECTS_PER_SESSION = 3             # §5.9: entry rejects per book per session -> HALT(REJECTS, BOOK, ACK)
# §5.9 regex with word boundaries on "ban": the spec's bare "ban" also matched "band" (every LPP/price-band reject)
# and "BANKNIFTY", which would have blocked the symbol for the day on an ordinary price reject.
BLOCK_RE = re.compile(r"(?i)(block|not allowed|restricted|\bban\b|\bbanned\b)")
MARGIN_RE = re.compile(r"(?i)(margin|\brms\b|insufficient)")
_TIME_EPS = 1e-6                    # monotonic float noise at timer boundaries


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _positive(x: object) -> bool:
    return _finite(x) and x > 0


def _num(x: object) -> float | None:
    """A finite number for an event payload (JSON refuses NaN), else None."""
    return float(x) if _finite(x) else None


def _plain_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def whole_ms(t: datetime) -> datetime:
    """t truncated to whole milliseconds, the precision titan2.db stores, so an object equals its reloaded copy."""
    return t.replace(microsecond=t.microsecond // 1000 * 1000)


# ---------------------------------------------------------------------------------------------------------------
# Errors, effects, timers
# ---------------------------------------------------------------------------------------------------------------
class IllegalTransition(InvariantViolation):
    """A (state, event) pair the transition tables do not list: a bug or a broker anomaly (an InvariantViolation).

    The core loop rolls the event back and reloads; scope GLOBAL halts entries (exits continue).
    """

    def __init__(self, what: str, state: object, event: object, detail: str = "", scope: str = "GLOBAL") -> None:
        self.state = state
        self.event = event
        text = f"{what}: {event} in state {state}" + (f" ({detail})" if detail else "")
        super().__init__("ILLEGAL_TRANSITION", text, scope)


@dataclass(slots=True)
class Effects:
    """What a transition asks of the engine once its rows are written."""

    commands: list[Command] = field(default_factory=list)    # dispatched after the commit, in order
    halts: list[HaltRequest] = field(default_factory=list)   # raised through the engine's halt path
    became_terminal: bool = False                            # the order reached a terminal state in this step
    filled: int = 0                                          # quantity newly filled in this step
    unresolved: bool = False                                 # the order's fills are in doubt: mark the P&L UNRESOLVED

    def add(self, other: Effects) -> Effects:
        self.commands.extend(other.commands)
        self.halts.extend(other.halts)
        self.became_terminal = self.became_terminal or other.became_terminal
        self.filled += other.filled
        self.unresolved = self.unresolved or other.unresolved
        return self


@dataclass(slots=True)
class OrderTimers:
    """One order's timers (monotonic seconds, RAM only; rebuilt by timers_after_reload after a load)."""

    next_poll: float | None = None          # next PollOrder (None: WORKING polls from submit + 3 s)
    cancel_sent: float | None = None        # when the Cancel went out (stuck after ORDER_STUCK_AFTER_S)
    lookups_due: list[float] = field(default_factory=list)   # pending LookupTag times, ascending
    lookup_since: float | None = None       # when tag lookups began (stuck after LOOKUP_STUCK_AFTER_S)
    lookup_ok: float | None = None          # the last lookup that answered (not found): restarts the stuck clock
    lookup_sent: float | None = None        # when the last LookupTag went out
    late_until: float | None = None         # AMBIGUOUS: no NOT_PLACED before this (LATE_ACCEPT_S after the answer)
    await_token: bool = False               # FATAL: look the tag up once the token works again
    trades_due: float | None = None         # FetchTrades retry after a failed TradesResult
    trades_tries: int = 0                   # FetchTrades requests sent
    last_conflict: str | None = None        # the contradiction already recorded (record and poll once each)


def needs_timer(order: Order, t: OrderTimers) -> bool:
    """True while on_timer() has work for this order: it is not terminal, or a FetchTrades retry is pending."""
    return not order.is_terminal or t.trades_due is not None


# ---------------------------------------------------------------------------------------------------------------
# The transition table (§5.2)
# ---------------------------------------------------------------------------------------------------------------
class OrderEvent(_StrEnum):
    SUBMIT = "SUBMIT"                       # dispatch a planned NEW order (phase-2 baskets)
    PLACE_OK = "PLACE_OK"
    PLACE_REJECTED = "PLACE_REJECTED"
    PLACE_AMBIGUOUS = "PLACE_AMBIGUOUS"
    PLACE_FATAL = "PLACE_FATAL"
    SNAP_WORKING = "SNAP_WORKING"           # OrderSnapshot by normalized status
    SNAP_COMPLETE = "SNAP_COMPLETE"
    SNAP_CANCELLED = "SNAP_CANCELLED"
    SNAP_REJECTED = "SNAP_REJECTED"
    LOOKUP_FOUND = "LOOKUP_FOUND"
    LOOKUP_NOT_FOUND = "LOOKUP_NOT_FOUND"
    LOOKUP_ERROR = "LOOKUP_ERROR"           # LookupResult(ok=False)
    CANCEL_REQUEST = "CANCEL_REQUEST"       # the engine asks for a cancel (the +6 s timer, FLATTEN_ALL, SIGTERM)
    CANCEL_OK = "CANCEL_OK"
    CANCEL_BENIGN = "CANCEL_BENIGN"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    CANCEL_AMBIGUOUS = "CANCEL_AMBIGUOUS"
    CANCEL_FATAL = "CANCEL_FATAL"
    TRADES = "TRADES"                       # TradesResult
    TIMER = "TIMER"
    SESSION_ENDED = "SESSION_ENDED"         # an earlier session's order still open at startup (session_ended())


def _table() -> dict[tuple[OrderState, OrderEvent], frozenset[OrderState]]:
    S, U, W, CR = OrderState.SUBMITTING, OrderState.UNKNOWN, OrderState.WORKING, OrderState.CANCEL_REQUESTED
    C, X, R, NP, NEW = (OrderState.COMPLETE, OrderState.CANCELLED, OrderState.REJECTED, OrderState.NOT_PLACED,
                        OrderState.NEW)
    E = OrderEvent
    place = (E.PLACE_OK, E.PLACE_REJECTED, E.PLACE_AMBIGUOUS, E.PLACE_FATAL)
    snaps = {E.SNAP_WORKING: W, E.SNAP_COMPLETE: C, E.SNAP_CANCELLED: X, E.SNAP_REJECTED: R}
    lookups = (E.LOOKUP_FOUND, E.LOOKUP_NOT_FOUND, E.LOOKUP_ERROR)
    cancels = (E.CANCEL_OK, E.CANCEL_BENIGN, E.CANCEL_REJECTED, E.CANCEL_AMBIGUOUS, E.CANCEL_FATAL)
    t: dict[tuple[OrderState, OrderEvent], frozenset[OrderState]] = {}

    def allow(state: OrderState, events: Iterable[OrderEvent], *to: OrderState) -> None:
        for e in events:
            t[(state, e)] = frozenset(to)

    # NEW: a planned basket leg, never sent
    allow(NEW, [E.SUBMIT], S)
    allow(NEW, [E.CANCEL_REQUEST], NP)
    allow(NEW, [E.TIMER], NEW)
    # SUBMITTING and UNKNOWN: waiting for the place answer, a snapshot or a tag lookup. Neither has a broker id, so
    # a cancel request waits for the IOC to resolve on its own.
    allow(S, [E.PLACE_OK], W, U)            # an OK without a broker id is handled like AMBIGUOUS
    allow(S, [E.PLACE_REJECTED], R)
    allow(S, [E.PLACE_AMBIGUOUS, E.PLACE_FATAL], U)
    for s in (S, U):
        for e, to in snaps.items():
            allow(s, [e], to)               # a WS update may arrive before the PlaceResult: adopt and apply
        allow(s, [E.LOOKUP_FOUND], W, C, X, R)
        allow(s, [E.LOOKUP_NOT_FOUND], s, NP)
        allow(s, [E.LOOKUP_ERROR, E.CANCEL_REQUEST, E.TIMER], s)
    # WORKING
    allow(W, place, W)                      # late answers: a snapshot or lookup adopted the order first
    for e, to in snaps.items():
        allow(W, [e], to)
    allow(W, [E.LOOKUP_FOUND], W, C, X, R)
    allow(W, [E.LOOKUP_NOT_FOUND, E.LOOKUP_ERROR, E.TRADES], W)
    allow(W, [E.CANCEL_REQUEST], CR)
    allow(W, [E.TIMER], W, CR)              # +3 s polls, +6 s cancel
    # CANCEL_REQUESTED: poll until terminal; stuck after 10 s, but never given up
    allow(CR, [*place, E.SNAP_WORKING, E.LOOKUP_NOT_FOUND, E.LOOKUP_ERROR, E.CANCEL_REQUEST, *cancels, E.TRADES,
               E.TIMER], CR)
    allow(CR, [E.SNAP_COMPLETE], C)
    allow(CR, [E.SNAP_CANCELLED], X)
    allow(CR, [E.SNAP_REJECTED], R)
    allow(CR, [E.LOOKUP_FOUND], CR, C, X, R)
    # terminal: nothing changes; late or duplicate reports are no-ops, contradicting ones are recorded
    for s in (C, X, R, NP):
        allow(s, [*place, *snaps, *lookups, E.CANCEL_REQUEST, E.TRADES, E.TIMER, E.SESSION_ENDED], s)
        if s is not NP:                     # a NOT_PLACED order never had a cancel sent
            allow(s, cancels, s)
    # an earlier session's order the DB still holds open (§9.6 step 7): the broker serves only today's orders, so
    #   nothing it says can end it; the bot concludes it (session_ended): CANCELLED once it had reached the broker,
    #   NOT_PLACED otherwise, its fills in doubt
    allow(NEW, [E.SESSION_ENDED], NP)
    for s in (S, U):
        allow(s, [E.SESSION_ENDED], NP)
    for s in (W, CR):
        allow(s, [E.SESSION_ENDED], X)
    return t


TRANSITIONS: Mapping[tuple[OrderState, OrderEvent], frozenset[OrderState]] = MappingProxyType(_table())


def allowed(state: OrderState | str, event: OrderEvent | str) -> frozenset[OrderState] | None:
    """The states (state, event) may lead to, or None when the pair is illegal."""
    return TRANSITIONS.get((OrderState(state), OrderEvent(event)))


_PLACE_KIND = {"OK": OrderEvent.PLACE_OK, "REJECTED": OrderEvent.PLACE_REJECTED,
               "AMBIGUOUS": OrderEvent.PLACE_AMBIGUOUS, "FATAL": OrderEvent.PLACE_FATAL}
_CANCEL_KIND = {"OK": OrderEvent.CANCEL_OK, "BENIGN": OrderEvent.CANCEL_BENIGN,
                "REJECTED": OrderEvent.CANCEL_REJECTED, "AMBIGUOUS": OrderEvent.CANCEL_AMBIGUOUS,
                "FATAL": OrderEvent.CANCEL_FATAL}
_SNAP_KIND = {"WORKING": OrderEvent.SNAP_WORKING, "COMPLETE": OrderEvent.SNAP_COMPLETE,
              "CANCELLED": OrderEvent.SNAP_CANCELLED, "REJECTED": OrderEvent.SNAP_REJECTED}
assert set(_PLACE_KIND) == set(PLACE_OUTCOMES) and set(_CANCEL_KIND) == set(CANCEL_OUTCOMES)
assert set(_SNAP_KIND) == set(ORDER_STATUSES)


def event_kind(ev: object) -> OrderEvent:
    """The table event for an engine event. An unknown place/cancel outcome counts as AMBIGUOUS (look it up, poll);
    an unknown snapshot status is a broken adapter and raises."""
    if isinstance(ev, PlaceResult):
        return _PLACE_KIND.get(ev.outcome, OrderEvent.PLACE_AMBIGUOUS)
    if isinstance(ev, OrderSnapshot):
        kind = _SNAP_KIND.get(ev.status)
        if kind is None:
            raise IllegalTransition(f"order {ev.tag or ev.broker_order_id}", "?", f"OrderSnapshot({ev.status!r})",
                                    "status is not one of " + "/".join(ORDER_STATUSES))
        return kind
    if isinstance(ev, LookupResult):
        if not ev.ok:
            return OrderEvent.LOOKUP_ERROR
        return OrderEvent.LOOKUP_FOUND if ev.found is not None else OrderEvent.LOOKUP_NOT_FOUND
    if isinstance(ev, CancelResult):
        return _CANCEL_KIND.get(ev.outcome, OrderEvent.CANCEL_AMBIGUOUS)
    if isinstance(ev, TradesResult):
        return OrderEvent.TRADES
    if isinstance(ev, Timer):
        return OrderEvent.TIMER
    raise TypeError(f"{type(ev).__name__} is not an order event")


def _require(order: Order, event: OrderEvent) -> frozenset[OrderState]:
    to = TRANSITIONS.get((OrderState(order.state), event))
    if to is None:
        raise IllegalTransition(f"order {order.order_pk}", order.state, event)
    return to


def _finish(order: Order, prev: OrderState, event: OrderEvent, to: frozenset[OrderState], fx: Effects) -> Effects:
    if order.state not in to:
        raise IllegalTransition(f"order {order.order_pk}", prev, event,
                                f"handler left it in {order.state}; the table allows {sorted(map(str, to))}")
    fx.became_terminal = prev not in TERMINAL_ORDER and order.state in TERMINAL_ORDER
    return fx


def _same_account(order: Order, account: object) -> None:
    if Account(account) is not Account(order.account):
        raise IllegalTransition(f"order {order.order_pk}", order.state, f"an event for account {account}",
                                f"the order is on {order.account}")


def _leg_scope(order: Order) -> str:
    return f"LEG:{order.leg_id}"


# ---------------------------------------------------------------------------------------------------------------
# Rows, events, alerts, halts
# ---------------------------------------------------------------------------------------------------------------
def _event(store: Store, kind: str, o: Order, now_mono: float, now_wall: datetime, payload: Mapping,
           severity: str = "INFO") -> None:
    store.append_event(kind, wall=now_wall, mono=now_mono, payload=payload, severity=severity, account=o.account,
                       book=o.book, structure_id=o.structure_id, leg_id=o.leg_id, order_pk=o.order_pk)


def _halt(kind: str, scope: str, reason: str) -> HaltRequest:
    return HaltRequest(kind, scope, HALT_RULES[kind].clears, reason[:500])


def _token_effects(o: Order, message: str | None) -> Effects:
    text = f"token or permission failure on {o.order_pk}: exits are impossible until the token is restored ({message})"
    return Effects(commands=[Alert("CRITICAL", "TOKEN", text, "token:fatal")],
                   halts=[_halt("TOKEN", "GLOBAL", text)])


def _poll(o: Order) -> PollOrder:
    return PollOrder(o.account, o.order_pk, o.broker_order_id)


def _conflict(store: Store, o: Order, t: OrderTimers, detail: str, fills_in_doubt: bool, now_mono: float,
              now_wall: datetime) -> Effects:
    """A broker report that contradicts the order's record: never applied. Recorded and polled once per distinct
    contradiction (a poll that brings the same report back must not loop)."""
    if t.last_conflict == detail:
        return Effects()
    t.last_conflict = detail
    _event(store, "INVARIANT_VIOLATION", o, now_mono, now_wall,
           {"code": "ORDER_REPORT_CONFLICT", "detail": detail, "state": str(o.state), "filled": o.filled_qty,
            "avg": _num(o.avg_fill_px), "fills_in_doubt": fills_in_doubt}, "CRITICAL")
    text = f"{o.book} order {o.order_pk}: {detail}"
    return Effects(commands=[Alert("CRITICAL", "ORDER_CONFLICT", text, f"order_conflict:{o.order_pk}"), _poll(o)],
                   unresolved=fills_in_doubt)


def reject_effects(store: Store, o: Order, msg: str | None, now_wall: datetime) -> Effects:
    """§5.9 after the order became REJECTED (its row already written): a WARN alert for every role; for ENTRY,
    HALT(CONTRACT_BLOCKED, SYMBOL, NEXT_SESSION) on a block/ban message (it applies to every book) and
    HALT(REJECTS, BOOK, ACK) from the 3rd entry reject of the session in the book. Exit rejects are re-priced by
    the exit ladder (§5.7)."""
    msg = msg or ""
    margin = bool(MARGIN_RE.search(msg))
    text = f"{o.book} {o.symbol} {o.role} {o.order_pk} rejected: {msg or '(no message)'}"
    fx = Effects(commands=[Alert("WARN", "MARGIN_REJECT" if margin else "ORDER_REJECTED", text,
                                 f"reject:{o.book}:{o.symbol}:{o.role}")])
    if o.role != Role.ENTRY:
        return fx
    if BLOCK_RE.search(msg):
        fx.halts.append(_halt("CONTRACT_BLOCKED", f"SYMBOL:{o.symbol}", text))
    session = session_of(o.created_wall or now_wall).isoformat()
    n = store.scalar("SELECT COUNT(*) FROM orders WHERE book=? AND role='ENTRY' AND state='REJECTED' "
                     "AND substr(created_ts,1,10)=?", (o.book, session))
    if n >= REJECTS_PER_SESSION:
        fx.halts.append(_halt("REJECTS", f"BOOK:{o.book}", f"{n} entry rejects in {o.book} on {session}; last: "
                                                           f"{msg or '(no message)'}"))
    return fx


def _terminal(store: Store, o: Order, t: OrderTimers, state: OrderState, msg: str | None, now_mono: float,
              now_wall: datetime, source: str, *, from_broker: bool = True) -> None:
    """Make the order terminal. from_broker=False for the bot's own conclusions (NOT_PLACED), which are not a
    broker status."""
    o.state = state
    o.terminal_wall = whole_ms(now_wall)
    if from_broker:
        o.broker_status = str(state)
    if msg is not None:
        o.status_msg = msg
    store.update_order(o)
    t.next_poll = t.cancel_sent = t.lookup_since = t.lookup_ok = t.late_until = None
    t.lookups_due = []
    t.await_token = False
    _event(store, "ORDER_TERMINAL", o, now_mono, now_wall,
           {"state": str(state), "filled": o.filled_qty, "qty": o.qty, "avg": _num(o.avg_fill_px),
            "msg": o.status_msg, "source": source, "stuck": o.stuck})


def _stuck(store: Store, o: Order, why: str, now_mono: float, now_wall: datetime) -> Effects:
    """ORDER_STUCK: CRITICAL + HALT(ORDER_STUCK, BOOK, ACK). The order keeps being polled or looked up, no new
    attempt is made on its leg (I4), and an EXITING leg stays EXITING until the order resolves."""
    o.stuck = True
    store.update_order(o)
    _event(store, "ORDER_STUCK", o, now_mono, now_wall, {"state": str(o.state), "why": why}, "CRITICAL")
    text = f"{o.book} {o.symbol} order {o.order_pk} stuck in {o.state}: {why}"
    return Effects(commands=[Alert("CRITICAL", "ORDER_STUCK", text, f"order_stuck:{o.order_pk}")],
                   halts=[_halt("ORDER_STUCK", f"BOOK:{o.book}", text)])


def _start_lookups(t: OrderTimers, now: float, throttled: bool = False) -> None:
    if throttled:
        due, at = [], now
        for gap in THROTTLE_BACKOFF_S:
            at += gap
            due.append(at)
    else:
        due = [now + s for s in LOOKUP_SCHEDULE_S]
    t.lookups_due = sorted(due)
    t.await_token = False
    if t.lookup_since is None:
        t.lookup_since = now


def _late_window(t: OrderTimers, now: float, until: float) -> None:
    """The order may have reached Kite: no NOT_PLACED before `until`, and lookups every LATE_LOOKUP_EVERY_S after
    the scheduled ones up to it, the last one at `until` (LATE_ACCEPT_S)."""
    t.late_until = until
    extra, at = [], max([now, *t.lookups_due]) + LATE_LOOKUP_EVERY_S
    while at < until - _TIME_EPS:
        extra.append(at)
        at += LATE_LOOKUP_EVERY_S
    if until > now + _TIME_EPS:
        extra.append(until)
    t.lookups_due = sorted({*t.lookups_due, *extra})


def _in_late_window(t: OrderTimers, now: float) -> bool:
    return t.late_until is not None and now < t.late_until - _TIME_EPS


def _submitted(o: Order, now_mono: float) -> float:
    """The order's submit time; a missing one is re-anchored to now (monotonic times are never persisted)."""
    if o.submitted_mono is None:
        o.submitted_mono = now_mono
    return o.submitted_mono


# ---------------------------------------------------------------------------------------------------------------
# apply_cum (§5.2): the only way an order's filled quantity grows
# ---------------------------------------------------------------------------------------------------------------
def apply_cum(store: Store, order: Order, leg: Leg, cum_qty: int, avg_px: float | None, now_mono: float,
              now_wall: datetime, *, exch_wall: datetime | None = None) -> Effects:
    """Apply a cumulative fill report (filled qty `cum_qty` at broker average `avg_px`). Idempotent.

    cum < filled raises InvariantViolation I9 (leg scope); cum == filled changes nothing; cum > filled inserts
    Fill("cum:<cum>", CUM, cum - prev) at (avg*cum - prev_avg*prev) / (cum - prev), or at avg (WARN) when that is
    <= 0 or more than FILL_PX_MAX_DEV from avg. With no usable avg the quantity is still recorded but no CUM row is
    written: the ledger then values it conservatively and marks it ESTIMATED/UNRESOLVED until TRADE rows arrive.
    Sets the leg's last_fill_mono / last_fill_wall (the post-fill rule, §9.2).
    """
    if leg.leg_id != order.leg_id:
        raise InvariantViolation("I3", f"order {order.order_pk} is not on leg {leg.leg_id}", _leg_scope(order))
    prev, prev_avg = order.filled_qty, order.avg_fill_px
    if not _plain_int(cum_qty) or cum_qty < 0:
        raise InvariantViolation("I9", f"order {order.order_pk}: filled qty {cum_qty!r} is not an int >= 0",
                                 _leg_scope(order))
    if cum_qty < prev:
        raise InvariantViolation("I9", f"order {order.order_pk}: broker filled {cum_qty} < recorded {prev}",
                                 _leg_scope(order))
    if cum_qty > order.qty:
        raise InvariantViolation("I9", f"order {order.order_pk}: broker filled {cum_qty} > ordered {order.qty}",
                                 _leg_scope(order))
    fx = Effects()
    if cum_qty == prev:
        return fx
    add = cum_qty - prev
    px: float | None = None
    fallback = None
    if _positive(avg_px):
        if prev == 0:
            px = float(avg_px)
        elif _positive(prev_avg):
            px = (avg_px * cum_qty - prev_avg * prev) / add
            if not (_positive(px) and abs(px / avg_px - 1.0) <= FILL_PX_MAX_DEV):
                fallback = f"derived fill price {px!r} is <= 0 or more than {FILL_PX_MAX_DEV:.0%} from avg {avg_px}"
                px = float(avg_px)
        else:
            fallback = "the earlier filled qty has no average price"
            px = float(avg_px)
        store.insert_fill(Fill(order.order_pk, f"cum:{cum_qty}", "CUM", add, px, whole_ms(now_wall), exch_wall))
    else:
        fallback = f"no usable average price ({avg_px!r}); qty recorded without a CUM fill"
    order.filled_qty = cum_qty
    order.avg_fill_px = float(avg_px) if _positive(avg_px) else None
    store.update_order(order)
    leg.last_fill_mono = now_mono
    leg.last_fill_wall = whole_ms(now_wall)
    store.update_leg(leg)
    _event(store, "FILL", order, now_mono, now_wall,
           {"seq_key": f"cum:{cum_qty}", "source": "CUM", "qty": add, "px": _num(px), "cum": cum_qty,
            "avg": _num(avg_px), "side": str(order.side), "fallback": fallback},
           "WARN" if fallback else "INFO")
    if fallback:
        fx.commands.append(Alert("WARN", "FILL_PX_FALLBACK", f"{order.book} order {order.order_pk}: {fallback}",
                                 f"fill_px:{order.order_pk}"))
    fx.filled = add
    return fx


# ---------------------------------------------------------------------------------------------------------------
# Creating and submitting orders
# ---------------------------------------------------------------------------------------------------------------
def _entry_limit_problem(role: Role, limit: float, tick: float, d: DecisionQuote | None) -> str | None:
    """I13 for the opening BUYs, against the decision quote: ENTRY ask <= limit <= floor_tick(mid x 1.01); SELFTEST
    strictly below the bid (it can never fill). A problem refuses the order (fails closed)."""
    if role is Role.ENTRY:
        if d is None or not (_positive(d.ask) and _positive(d.mid)):
            return "an ENTRY needs a decision quote with an ask and a mid"
        cap = floor_tick(d.mid * (1 + ENTRY_MAX_OVER_MID), tick)
        if not d.ask - PRICE_EPS <= limit <= cap + PRICE_EPS:
            return f"ENTRY limit {limit} outside [ask {d.ask}, floor_tick(mid x 1.01) {cap}]"
    elif role is Role.SELFTEST:
        if d is None or not _positive(d.bid) or not limit < d.bid - PRICE_EPS:
            return f"SELFTEST limit {limit} must sit below the decision bid {None if d is None else d.bid}"
    return None


def _exit_limit_bound(role: Role, limit: float, tick: float, d: DecisionQuote | None) -> float:
    """I13 for the closing orders, which a bad input must never block: an EXIT limit below
    floor_tick(ref_bid x (1 - MAX_EXIT_DISCOUNT)) is raised to ceil_tick(ref_bid x 0.88) (exits.sell_limit's own
    floor); a COVER limit above floor_tick(ref_ask x 1.12) is lowered to it."""
    if role is Role.EXIT and d is not None and _positive(d.bid):
        if limit < floor_tick(d.bid * (1 - MAX_EXIT_DISCOUNT), tick) - PRICE_EPS:
            return ceil_tick(d.bid * (1 - MAX_EXIT_DISCOUNT), tick)
    elif role is Role.COVER and d is not None and _positive(d.ask):
        cap = floor_tick(d.ask * (1 + MAX_EXIT_DISCOUNT), tick)
        if limit > cap + PRICE_EPS:
            return cap
    return limit


def _request(order: Order, leg: Leg) -> OrderRequest:
    """The broker request of a bot order. It carries the decision quote's exchange time (none for an order without
    a decision quote, or one reloaded from titan2.db, which does not keep it)."""
    i = leg.inst
    return OrderRequest(account=Account(order.account), tag=order.order_pk, role=Role(order.role), symbol=i.symbol,
                        tradingsymbol=i.tradingsymbol, exchange=i.exchange, token=i.token, side=Side(order.side),
                        qty=order.qty, limit_px=order.limit_px, lot_size=i.lot_size, tick=i.tick, book=order.book,
                        decision_exch_wall=None if order.decision is None else order.decision.exch_wall)


def _submitted_event(store: Store, o: Order, now_mono: float, now_wall: datetime) -> None:
    d = o.decision
    _event(store, "ORDER_SUBMITTED", o, now_mono, now_wall,
           {"symbol": o.symbol, "role": str(o.role), "side": str(o.side), "qty": o.qty, "limit": _num(o.limit_px),
            "bid": _num(d.bid) if d else None, "ask": _num(d.ask) if d else None, "mid": _num(d.mid) if d else None,
            "age_ms": d.age_ms if d else None, "src": d.src if d else None, "ltp": _num(d.ltp) if d else None,
            "walk_px": _num(d.walk_px) if d else None, "attempt": o.attempt, "band": o.band_idx})


def create_order(store: Store, orders: MutableMapping[str, Order], leg: Leg, *, role: Role | str, qty: int,
                 limit_px: float, now_mono: float, now_wall: datetime, decision: DecisionQuote | None = None,
                 band_idx: int | None = None, live_allowed: bool = False,
                 planned: bool = False) -> tuple[Order, Effects]:
    """Create a bot order on `leg` (bound to the index `orders`): its row as SUBMITTING plus the Place command, or
    as NEW without one when planned (phase-2 baskets; submit() sends it later).

    attempt = 1 + the highest attempt on the leg, so every retry gets a new tag (make_tag). Refused with an
    InvariantViolation: a second non-terminal bot order on the leg (I4), an EXIT above the open qty (I6), a request
    that breaks the fixed order rules (I7, I12 lot multiple, I13 tick grid), an ENTRY/SELFTEST limit outside its
    decision-quote bounds (I13), an ENTRY or SELFTEST on LIVE without live_allowed (I14: the caller passes True only
    with ALLOW_LIVE=1 and, for ENTRY, a LIVE strategy with a valid unlock and today's self-test). A bad input never
    blocks an exit: an EXIT/COVER limit beyond its I13 bound is moved onto the bound, with a WARN.
    ENTRY and SELFTEST need an ENTERING leg, EXIT an EXITING one; a COVER may hang off any leg (it repairs a broker
    short, §9.3).
    """
    role = Role(role)
    if role not in ROLE_SIDE:
        raise ValueError(f"role {role} cannot be placed by the bot")
    what = f"{role} order on leg {leg.leg_id}"
    need = {Role.ENTRY: LegState.ENTERING, Role.SELFTEST: LegState.ENTERING, Role.EXIT: LegState.EXITING}.get(role)
    if need is not None and leg.state != need:
        raise IllegalTransition(what, leg.state, "create", f"needs a {need} leg")
    current = leg.leg_orders()
    busy = [o.order_pk for o in current if o.role != Role.EXTERNAL and not o.is_terminal]
    if busy:
        raise InvariantViolation("I4", f"{what}: order {busy[0]} is not terminal yet")
    if role is Role.EXIT and (not _plain_int(qty) or qty > leg.open_qty):
        raise InvariantViolation("I6", f"{what}: EXIT qty {qty!r} exceeds open qty {leg.open_qty}")
    if Account(leg.account) is Account.LIVE and role in (Role.ENTRY, Role.SELFTEST) and live_allowed is not True:
        raise InvariantViolation("I14", f"{what}: LIVE {role} orders are not allowed")
    attempt = 1 + max((o.attempt for o in current if o.role != Role.EXTERNAL), default=0)
    pk = make_tag(role, leg.account, leg.session, leg.seq_no, attempt)
    asked = limit_px
    if _finite(limit_px):
        limit_px = _exit_limit_bound(role, limit_px, leg.inst.tick, decision)
    order = Order(order_pk=pk, leg_id=leg.leg_id, structure_id=leg.structure_id, account=Account(leg.account),
                  book=leg.book, role=role, side=ROLE_SIDE[role], symbol=leg.inst.symbol, qty=qty,
                  limit_px=limit_px, attempt=attempt, band_idx=band_idx,
                  state=OrderState.NEW if planned else OrderState.SUBMITTING, decision=decision,
                  created_wall=whole_ms(now_wall), submitted_mono=None if planned else now_mono)
    req = _request(order, leg)
    problem = order_request_problem(req)
    if problem is not None:
        code = "I13" if "tick" in problem else "I12" if "lot" in problem else "I7"
        raise InvariantViolation(code, f"{what}: {problem}")
    problem = _entry_limit_problem(role, limit_px, leg.inst.tick, decision)
    if problem is not None:
        raise InvariantViolation("I13", f"{what}: {problem}")
    fx = Effects(commands=[] if planned else [Place(order.account, pk, req)])
    with store.tx():
        store.insert_order(order, submitted_wall=None if planned else now_wall)
        if planned:
            _event(store, "ORDER_CREATED", order, now_mono, now_wall,
                   {"symbol": order.symbol, "role": str(role), "qty": qty, "limit": _num(limit_px)})
        else:
            _submitted_event(store, order, now_mono, now_wall)
        if limit_px != asked:
            text = (f"{order.book} {role} {pk}: limit {asked} breaks the I13 bound for decision quote "
                    f"bid {decision.bid} / ask {decision.ask}; sent at {limit_px}")
            _event(store, "ORDER_UPDATE", order, now_mono, now_wall,
                   {"warning": text, "asked_limit": _num(asked), "limit": limit_px}, "WARN")
            fx.commands.append(Alert("WARN", "LIMIT_BOUNDED", text, f"limit_bounded:{order.book}:{order.symbol}"))
    orders[pk] = order
    leg.orders.append(pk)
    return order, fx


def submit(store: Store, order: Order, leg: Leg, t: OrderTimers, now_mono: float, now_wall: datetime) -> Effects:
    """NEW -> SUBMITTING: send a planned order (phase-2 baskets)."""
    to = _require(order, OrderEvent.SUBMIT)
    prev = order.state
    with store.tx():
        order.state = OrderState.SUBMITTING
        order.submitted_mono = now_mono
        store.update_order(order, submitted_wall=now_wall)
        _submitted_event(store, order, now_mono, now_wall)
    return _finish(order, prev, OrderEvent.SUBMIT, to, Effects(commands=[Place(order.account, order.order_pk,
                                                                               _request(order, leg))]))


# ---------------------------------------------------------------------------------------------------------------
# Broker answers
# ---------------------------------------------------------------------------------------------------------------
def on_place_result(store: Store, order: Order, leg: Leg, ev: PlaceResult, t: OrderTimers, now_mono: float,
                    now_wall: datetime) -> Effects:
    """SUBMITTING: OK -> WORKING (polls from submit + 3 s); REJECTED -> REJECTED (§5.9); AMBIGUOUS -> UNKNOWN with
    tag lookups at +1/+3/+7/+15 s (after gaps of 1/2/4/8 s when throttled: the same times), then every 5 s up to
    +LATE_ACCEPT_S (no NOT_PLACED before it); FATAL -> UNKNOWN, HALT(TOKEN, GLOBAL, AUTO) + CRITICAL, lookups once
    the token works. Later answers (a snapshot or lookup adopted the order first) change nothing."""
    _same_account(order, ev.account)
    kind = event_kind(ev)
    to = _require(order, kind)
    prev = order.state
    with store.tx():
        fx = _place(store, order, ev, kind, t, now_mono, now_wall)
    return _finish(order, prev, kind, to, fx)


def _place(store: Store, o: Order, ev: PlaceResult, kind: OrderEvent, t: OrderTimers, now_mono: float,
           now_wall: datetime) -> Effects:
    bid = ev.broker_order_id or None
    severity = {OrderEvent.PLACE_OK: "INFO", OrderEvent.PLACE_FATAL: "CRITICAL"}.get(kind, "WARN")
    _event(store, "ORDER_PLACE_RESULT", o, now_mono, now_wall,
           {"outcome": ev.outcome, "broker_order_id": bid, "error_class": ev.error_class, "message": ev.message,
            "throttled": bool(ev.throttled), "state": str(o.state)}, severity)
    fx = _token_effects(o, ev.message) if kind is OrderEvent.PLACE_FATAL else Effects()
    if o.state is OrderState.SUBMITTING:
        if kind is OrderEvent.PLACE_OK and bid:
            o.broker_order_id = bid
            o.state = OrderState.WORKING
            o.broker_status = str(OrderState.WORKING)
            t.lookups_due, t.lookup_since, t.next_poll, t.late_until = [], None, None, None
            store.update_order(o, acked_wall=now_wall)
        elif kind is OrderEvent.PLACE_REJECTED:
            _terminal(store, o, t, OrderState.REJECTED, ev.message or "", now_mono, now_wall, "PLACE")
            fx.add(reject_effects(store, o, ev.message, now_wall))
        else:                                   # AMBIGUOUS, FATAL, or an OK that carries no broker id
            o.state = OrderState.UNKNOWN
            if ev.message:
                o.status_msg = ev.message
            if kind is OrderEvent.PLACE_FATAL:
                t.await_token, t.lookups_due = True, []
            else:
                _start_lookups(t, now_mono, throttled=bool(ev.throttled))
                _late_window(t, now_mono, now_mono + LATE_ACCEPT_S)
            store.update_order(o)
        return fx
    # A later answer: a snapshot or lookup adopted the order first (WORKING / CANCEL_REQUESTED / terminal).
    if kind is OrderEvent.PLACE_OK:
        if bid and o.broker_order_id and bid != o.broker_order_id:
            fx.add(_conflict(store, o, t, f"PlaceResult id {bid} differs from adopted id {o.broker_order_id}",
                             True, now_mono, now_wall))
        elif bid and bid == o.broker_order_id:
            store.update_order(o, acked_wall=now_wall)      # e.g. accepted by the API, then rejected by the exchange
        elif o.state in (OrderState.NOT_PLACED, OrderState.REJECTED):
            fx.add(_conflict(store, o, t, f"the broker accepted ({bid}) an order recorded {o.state}", True,
                             now_mono, now_wall))
    elif kind is OrderEvent.PLACE_REJECTED and o.state not in (OrderState.REJECTED, OrderState.NOT_PLACED):
        fx.add(_conflict(store, o, t, f"PlaceResult REJECTED for an order the broker reports {o.state}", False,
                         now_mono, now_wall))
    return fx


def on_snapshot(store: Store, order: Order, leg: Leg, ev: OrderSnapshot, t: OrderTimers, now_mono: float,
                now_wall: datetime) -> Effects:
    """Apply a broker report of the order (WS, POLL, LOOKUP, SIM, RECOVERY), matched by tag or broker id.

    SUBMITTING/UNKNOWN adopt the broker id first (a WS update may beat the PlaceResult). WORKING: filled up ->
    apply_cum; an older report (lower filled) is ignored. COMPLETE/CANCELLED: apply_cum, then terminal, FetchTrades
    when filled > 0; a COMPLETE with filled != qty is a WARN and filled_qty is trusted; a final report with less
    filled than recorded is I9. REJECTED with any fill is an InvariantViolation. Terminal orders: duplicates and
    older reports are no-ops; anything else is a recorded contradiction (never applied).
    """
    _same_account(order, ev.account)
    kind = event_kind(ev)
    to = _require(order, kind)
    prev = order.state
    with store.tx():
        fx = _snapshot(store, order, leg, t, broker_id=ev.broker_order_id, tag=ev.tag, status=ev.status,
                       cum=ev.filled_qty, avg=ev.avg_px, msg=ev.status_msg, exch_wall=ev.exch_wall, source=ev.source,
                       now_mono=now_mono, now_wall=now_wall)
    return _finish(order, prev, kind, to, fx)


def _snapshot(store: Store, o: Order, leg: Leg, t: OrderTimers, *, broker_id: str | None, tag: str | None,
              status: str, cum: int, avg: float | None, msg: str | None, exch_wall: datetime | None, source: str,
              now_mono: float, now_wall: datetime) -> Effects:
    what = f"order {o.order_pk}"
    if broker_id and o.broker_order_id and broker_id != o.broker_order_id:
        raise IllegalTransition(what, o.state, f"a report for broker order {broker_id}",
                                f"the order is broker order {o.broker_order_id}")
    if tag and tag != o.order_pk and not adopted_report(o, tag, broker_id):
        raise IllegalTransition(what, o.state, f"a report tagged {tag}")
    if not _plain_int(cum) or cum < 0:
        raise IllegalTransition(what, o.state, f"a report with filled qty {cum!r}")
    if o.is_terminal:
        return _terminal_report(store, o, t, status, cum, avg, now_mono, now_wall)
    fx = Effects()
    changed = False
    if o.state in (OrderState.SUBMITTING, OrderState.UNKNOWN):
        if broker_id:
            o.broker_order_id = broker_id
        t.lookups_due, t.lookup_since, t.await_token, t.next_poll = [], None, False, None
        t.lookup_ok = t.late_until = None
        changed = True
    if status == "REJECTED":
        if cum > 0 or o.filled_qty > 0:
            raise InvariantViolation("I9", f"{what}: REJECTED reported with filled {cum} (recorded {o.filled_qty})",
                                     _leg_scope(o))
        _terminal(store, o, t, OrderState.REJECTED, msg or "", now_mono, now_wall, source)
        return fx.add(reject_effects(store, o, msg, now_wall))
    if status == "WORKING":
        if cum > o.filled_qty:
            fx.add(apply_cum(store, o, leg, cum, avg, now_mono, now_wall, exch_wall=exch_wall))
            changed = True
        if o.state in (OrderState.SUBMITTING, OrderState.UNKNOWN):
            o.state = OrderState.WORKING
        if o.broker_status != "WORKING" or (msg and msg != o.status_msg):
            o.broker_status = "WORKING"
            o.status_msg = msg or o.status_msg
            changed = True
        if changed:
            store.update_order(o)
            _event(store, "ORDER_UPDATE", o, now_mono, now_wall,
                   {"status": status, "filled": o.filled_qty, "avg": _num(o.avg_fill_px), "msg": msg,
                    "source": source, "state": str(o.state)})
        return fx
    # COMPLETE or CANCELLED: the broker's final word
    if cum < o.filled_qty:
        raise InvariantViolation("I9", f"{what}: final {status} reports filled {cum} < recorded {o.filled_qty}",
                                 _leg_scope(o))
    if cum > o.filled_qty:
        fx.add(apply_cum(store, o, leg, cum, avg, now_mono, now_wall, exch_wall=exch_wall))
    _terminal(store, o, t, OrderState(status), msg, now_mono, now_wall, source)
    if status == "COMPLETE" and o.filled_qty != o.qty:
        text = f"{o.book} order {o.order_pk}: COMPLETE with filled {o.filled_qty} != qty {o.qty}; trusting filled"
        _event(store, "ORDER_UPDATE", o, now_mono, now_wall, {"warning": text}, "WARN")
        fx.commands.append(Alert("WARN", "COMPLETE_PARTIAL", text, f"complete_partial:{o.order_pk}"))
    if o.filled_qty > 0 and o.broker_order_id:
        fx.commands.append(FetchTrades(o.account, o.order_pk, o.broker_order_id))
        t.trades_tries, t.trades_due = 1, None
    return fx


def _terminal_report(store: Store, o: Order, t: OrderTimers, status: str, cum: int, avg: float | None,
                     now_mono: float, now_wall: datetime) -> Effects:
    if status == str(o.state) and cum == o.filled_qty:
        return Effects()                         # a duplicate
    if status == "WORKING" and cum <= o.filled_qty and o.state is not OrderState.NOT_PLACED:
        return Effects()                         # an older report delivered late
    avg_moved = cum > 0 and _positive(avg) and _positive(o.avg_fill_px) and abs(avg - o.avg_fill_px) > 1e-6
    detail = (f"recorded {o.state} filled {o.filled_qty}, broker reports {status} filled {cum}"
              + (f" avg {avg}" if avg_moved else ""))
    return _conflict(store, o, t, detail, cum != o.filled_qty or avg_moved, now_mono, now_wall)


def on_lookup_result(store: Store, order: Order, leg: Leg, ev: LookupResult, t: OrderTimers, now_mono: float,
                     now_wall: datetime) -> Effects:
    """Tag lookup answer. Found -> adopt and apply like a snapshot. Not found NOT_PLACED_LOOKUPS times (+1/+3/+7/
    +15 s), the deciding read asked (ev.req_mono) >= NOT_PLACED_AFTER_S since submit and, after an AMBIGUOUS place,
    no earlier than its late_until (LATE_ACCEPT_S) -> NOT_PLACED; otherwise another lookup. API error -> retry every
    2 s (ORDER_STUCK once no lookup answered for 30 s, from on_timer). Answers that arrive after the order resolved
    are applied like any report (found) or ignored."""
    _same_account(order, ev.account)
    kind = event_kind(ev)
    to = _require(order, kind)
    prev = order.state
    with store.tx():
        if kind is OrderEvent.LOOKUP_FOUND:
            b = ev.found
            if b.tag != order.order_pk:
                raise IllegalTransition(f"order {order.order_pk}", order.state, f"a lookup that found tag {b.tag!r}")
            fx = _snapshot(store, order, leg, t, broker_id=b.broker_order_id, tag=b.tag, status=b.status,
                           cum=b.filled_qty, avg=b.avg_px, msg=b.status_msg, exch_wall=b.exch_wall, source="LOOKUP",
                           now_mono=now_mono, now_wall=now_wall)
        elif order.state in (OrderState.SUBMITTING, OrderState.UNKNOWN):
            fx = _lookup_miss(store, order, leg, kind, t, now_mono, now_wall, asked=ev.req_mono)
        else:
            fx = Effects()
    return _finish(order, prev, kind, to, fx)


def _decider_sent(t: OrderTimers, sub: float) -> bool:
    """A LookupTag went out at or after the first moment a miss may make the order NOT_PLACED (NOT_PLACED_AFTER_S
    after submit, and the late window's end): its answer will decide, so no other lookup is needed."""
    start = max(sub + NOT_PLACED_AFTER_S, t.late_until if t.late_until is not None else -math.inf)
    return t.lookup_sent is not None and t.lookup_sent >= start - _TIME_EPS


def _lookup_miss(store: Store, o: Order, leg: Leg, kind: OrderEvent, t: OrderTimers, now_mono: float,
                 now_wall: datetime, *, asked: float | None = None) -> Effects:
    if leg.leg_id != o.leg_id:
        raise InvariantViolation("I3", f"order {o.order_pk} is not on leg {leg.leg_id}", _leg_scope(o))
    sub = _submitted(o, now_mono)
    if t.lookup_since is None:
        t.lookup_since = now_mono
    if kind is OrderEvent.LOOKUP_NOT_FOUND:
        o.lookups += 1
        t.lookup_ok = now_mono
        # A miss says the tag was not in orders() when its read was asked, so NOT_PLACED rests on that time, not on
        #   when the answer came: in a Kite brownout the FIFO order lane answers a read asked inside the late window
        #   after the window ended, while the lookup asked at its end is still queued (review round 3). An answer
        #   with no request time counts as asked when it arrived.
        asked = min(asked, now_mono) if _finite(asked) else now_mono
        elapsed = asked - sub
        late = _in_late_window(t, asked)
        _event(store, "ORDER_LOOKUP", o, now_mono, now_wall,
               {"ok": True, "found": False, "lookups": o.lookups, "since_submit_s": round(now_mono - sub, 3),
                "asked_since_submit_s": round(elapsed, 3),
                "late_window_s": round(t.late_until - asked, 3) if late else None})
        if o.lookups >= NOT_PLACED_LOOKUPS and elapsed >= NOT_PLACED_AFTER_S - _TIME_EPS and not late:
            _terminal(store, o, t, OrderState.NOT_PLACED, f"not found by {o.lookups} tag lookups", now_mono,
                      now_wall, "LOOKUP", from_broker=False)
            # It may exist after all (orders() lag, or a tag orders() does not echo). For recon's post-fill rule
            #   (§9.2) this moment counts like a fill on the leg: no exit, cover or verify-flat is decided from a
            #   broker read requested before now + POSITIONS_POST_FILL_LAG_S, and the symbol is in grace meanwhile.
            #   update_leg pairs it with the leg's stored fill time, so a rollback reload keeps it (review round 3).
            leg.last_fill_mono = now_mono if leg.last_fill_mono is None else max(leg.last_fill_mono, now_mono)
            store.update_leg(leg)
            return Effects()
        if not t.lookups_due:
            if o.lookups < NOT_PLACED_LOOKUPS:
                t.lookups_due = [now_mono + LOOKUP_RETRY_S]
            elif now_mono - sub < NOT_PLACED_AFTER_S - _TIME_EPS:
                t.lookups_due = [sub + NOT_PLACED_AFTER_S]
            elif _in_late_window(t, now_mono):      # inside the late window: keep looking until it ends
                t.lookups_due = [min(now_mono + LATE_LOOKUP_EVERY_S, t.late_until)]
            elif not _decider_sent(t, sub):         # this read was asked too early: one more, asked from now
                t.lookups_due = [now_mono]
        return Effects()
    _event(store, "ORDER_LOOKUP", o, now_mono, now_wall, {"ok": False, "lookups": o.lookups}, "WARN")
    if not t.await_token and not any(d <= now_mono + LOOKUP_RETRY_S + _TIME_EPS for d in t.lookups_due):
        t.lookups_due = sorted([*t.lookups_due, now_mono + LOOKUP_RETRY_S])
    return Effects()


def on_cancel_result(store: Store, order: Order, ev: CancelResult, t: OrderTimers, now_mono: float,
                     now_wall: datetime) -> Effects:
    """CANCEL_REQUESTED: poll until terminal (now, then every 1 s; every 2 s once stuck). A failed cancel is only
    polled (an IOC resolves on its own); FATAL also raises HALT(TOKEN). A late answer for a terminal order is a
    no-op."""
    _same_account(order, ev.account)
    kind = event_kind(ev)
    to = _require(order, kind)
    prev = order.state
    fx = Effects()
    if not order.is_terminal:
        with store.tx():
            severity = {OrderEvent.CANCEL_FATAL: "CRITICAL", OrderEvent.CANCEL_OK: "INFO",
                        OrderEvent.CANCEL_BENIGN: "INFO"}.get(kind, "WARN")
            _event(store, "ORDER_UPDATE", order, now_mono, now_wall,
                   {"cancel_outcome": ev.outcome, "message": ev.message, "state": str(order.state)}, severity)
        if kind is OrderEvent.CANCEL_FATAL:
            fx.add(_token_effects(order, ev.message))
        fx.commands.append(_poll(order))
        t.next_poll = now_mono + (STUCK_POLL_EVERY_S if order.stuck else ORDER_POLL_EVERY_S)
    return _finish(order, prev, kind, to, fx)


def on_trades_result(store: Store, order: Order, ev: TradesResult, t: OrderTimers, now_mono: float,
                     now_wall: datetime) -> Effects:
    """Insert the order's TRADE fills ("trade:<id>", duplicates dropped by UNIQUE). Refused (WARN) when a trade
    belongs to another order or side, or the trades add up to more than filled_qty. A failed read is retried
    (TRADES_MAX_TRIES requests in all); the CUM fills stay effective meanwhile."""
    _same_account(order, ev.account)
    to = _require(order, OrderEvent.TRADES)
    prev = order.state
    with store.tx():
        _trades(store, order, ev, t, now_mono, now_wall)
    return _finish(order, prev, OrderEvent.TRADES, to, Effects())


def _trades(store: Store, o: Order, ev: TradesResult, t: OrderTimers, now_mono: float, now_wall: datetime) -> None:
    if not ev.ok:
        if t.trades_tries < TRADES_MAX_TRIES:
            t.trades_due = now_mono + TRADES_RETRY_S
        else:
            t.trades_due = None
            _event(store, "ORDER_UPDATE", o, now_mono, now_wall,
                   {"warning": f"trades unavailable after {t.trades_tries} requests; CUM fills stay effective"},
                   "WARN")
        return
    t.trades_due = None
    trades = tuple(ev.trades)
    bad = [tr.trade_id for tr in trades
           if (o.broker_order_id and tr.broker_order_id != o.broker_order_id) or Side(tr.side) is not Side(o.side)
           or not (_plain_int(tr.qty) and tr.qty > 0 and _positive(tr.px))]
    total = sum(tr.qty for tr in trades if _plain_int(tr.qty))
    if bad or total > o.filled_qty:
        _event(store, "ORDER_UPDATE", o, now_mono, now_wall,
               {"warning": "trades refused", "bad_trade_ids": [str(x) for x in bad], "trades_qty": total,
                "filled": o.filled_qty}, "WARN")
        return
    new = 0
    for tr in trades:
        if store.insert_fill(Fill(o.order_pk, f"trade:{tr.trade_id}", "TRADE", tr.qty, float(tr.px),
                                  whole_ms(tr.fill_wall or now_wall), tr.exch_wall)):
            new += 1
    if new:
        _event(store, "FILL", o, now_mono, now_wall,
               {"source": "TRADE", "rows": new, "trades_qty": total, "filled": o.filled_qty})


# ---------------------------------------------------------------------------------------------------------------
# Engine-initiated: cancel and timers
# ---------------------------------------------------------------------------------------------------------------
def request_cancel(store: Store, order: Order, t: OrderTimers, now_mono: float, now_wall: datetime, *,
                   reason: str = "TIMEOUT") -> Effects:
    """WORKING -> CANCEL_REQUESTED + Cancel (the +6 s timer, FLATTEN_ALL, SIGTERM); NEW -> NOT_PLACED. SUBMITTING
    and UNKNOWN have no broker id to cancel and resolve on their own (IOC); anything else is a no-op."""
    to = _require(order, OrderEvent.CANCEL_REQUEST)
    prev = order.state
    fx = Effects()
    with store.tx():
        if order.state is OrderState.NEW:
            _terminal(store, order, t, OrderState.NOT_PLACED, f"cancelled before dispatch: {reason}", now_mono,
                      now_wall, "ENGINE", from_broker=False)
        elif order.state is OrderState.WORKING:
            order.state = OrderState.CANCEL_REQUESTED
            store.update_order(order)
            t.cancel_sent = now_mono
            t.next_poll = now_mono + ORDER_POLL_EVERY_S
            _event(store, "ORDER_UPDATE", order, now_mono, now_wall,
                   {"action": "CANCEL", "reason": reason, "state": str(order.state)})
            fx.commands.append(Cancel(order.account, order.order_pk, order.broker_order_id))
    return _finish(order, prev, OrderEvent.CANCEL_REQUEST, to, fx)


def session_ended(store: Store, order: Order, t: OrderTimers, now_mono: float, now_wall: datetime, *,
                  session: object = None) -> Effects:
    """End a bot order of an earlier session that the DB still holds open (§9.6 step 7: the bot died mid-order and
    came back on a later day). Kite and the paper exchange serve only today's orders, so no report can end it and it
    would hold its leg (I4) and its symbol (recon grace) for good. The bot's own conclusion (never a broker status):
    CANCELLED once it had reached the broker (IOC: nothing of it works after its day), NOT_PLACED otherwise. Its
    fills are in doubt (Effects.unresolved); a terminal order is left as it is."""
    to = _require(order, OrderEvent.SESSION_ENDED)
    prev = order.state
    fx = Effects()
    if not order.is_terminal:
        state = OrderState.CANCELLED if prev in (OrderState.WORKING, OrderState.CANCEL_REQUESTED) \
            else OrderState.NOT_PLACED
        msg = (f"left {prev} by session {session or 'an earlier session'}: the broker no longer reports it; "
               "outcome unknown")
        with store.tx():
            _terminal(store, order, t, state, msg, now_mono, now_wall, "SESSION_ENDED", from_broker=False)
        t.trades_due = None
        fx.unresolved = True
        fx.commands.append(Alert("CRITICAL", "STALE_STRUCTURE", f"{order.book} {order.symbol} order "
                                 f"{order.order_pk}: {msg}; recorded {state}, P&L UNRESOLVED: check it in Kite",
                                 f"stale_order:{order.order_pk}"))
    return _finish(order, prev, OrderEvent.SESSION_ENDED, to, fx)


def on_timer(store: Store, order: Order, t: OrderTimers, now_mono: float, now_wall: datetime, *,
             token_ok: bool = True) -> Effects:
    """Run the order's timers (§5.2):
    - SUBMITTING with no answer for PLACE_ANSWER_WAIT_S: tag lookups and the late window (a lost PlaceResult must
      not block the leg, and the order may still reach the book);
    - SUBMITTING / UNKNOWN: due LookupTags (LOOKUP_SCHEDULE_S, then the late window's); after FATAL, lookups once
      token_ok; ORDER_STUCK once no lookup answered for 30 s (API errors or lost answers) since they began;
    - WORKING: PollOrder from submit + 3 s every 1 s; Cancel at submit + 6 s;
    - CANCEL_REQUESTED: PollOrder every 1 s; ORDER_STUCK at cancel + 10 s, then every 2 s;
    - terminal: a pending FetchTrades retry.
    """
    to = _require(order, OrderEvent.TIMER)
    prev = order.state
    now = now_mono
    s = order.state
    fx = Effects()
    with store.tx():
        if s in (OrderState.SUBMITTING, OrderState.UNKNOWN):
            sub = _submitted(order, now)
            if (s is OrderState.SUBMITTING and t.lookup_since is None and not t.lookups_due and not t.await_token
                    and now - sub >= PLACE_ANSWER_WAIT_S - _TIME_EPS):
                _start_lookups(t, now)
                _late_window(t, now, now + LATE_ACCEPT_S)
                _event(store, "ORDER_UPDATE", order, now, now_wall,
                       {"warning": f"no PlaceResult after {now - sub:.1f}s: looking the tag up"}, "WARN")
            if t.await_token and token_ok:
                _start_lookups(t, now)
            if t.lookups_due and t.lookups_due[0] <= now + _TIME_EPS:
                t.lookups_due = [d for d in t.lookups_due if d > now + _TIME_EPS]
                t.lookup_sent = now
                fx.commands.append(LookupTag(order.account, order.order_pk))
            # stuck when the lookups stop answering (API errors, lost answers); clean misses inside the late window
            #   are the window doing its work, not a stuck order
            quiet = t.lookup_since
            if quiet is not None and t.lookup_ok is not None:
                quiet = max(quiet, t.lookup_ok)
            if not order.stuck and quiet is not None and now - quiet >= LOOKUP_STUCK_AFTER_S - _TIME_EPS:
                fx.add(_stuck(store, order, f"unresolved {now - t.lookup_since:.0f}s after tag lookups began, none "
                                            f"answered for {now - quiet:.0f}s", now, now_wall))
        elif s is OrderState.WORKING:
            sub = _submitted(order, now)
            if now - sub >= ORDER_CANCEL_AFTER_S - _TIME_EPS:
                fx.add(request_cancel(store, order, t, now, now_wall, reason="TIMEOUT"))
            else:
                due = t.next_poll if t.next_poll is not None else sub + ORDER_POLL_AFTER_S
                if now >= due - _TIME_EPS:
                    fx.commands.append(_poll(order))
                    t.next_poll = now + ORDER_POLL_EVERY_S
        elif s is OrderState.CANCEL_REQUESTED:
            if t.cancel_sent is None:
                t.cancel_sent = now
            if not order.stuck and now - t.cancel_sent >= ORDER_STUCK_AFTER_S - _TIME_EPS:
                fx.add(_stuck(store, order, f"no terminal state {now - t.cancel_sent:.0f}s after the cancel",
                              now, now_wall))
            due = t.next_poll if t.next_poll is not None else t.cancel_sent + ORDER_POLL_EVERY_S
            if now >= due - _TIME_EPS:
                fx.commands.append(_poll(order))
                t.next_poll = now + (STUCK_POLL_EVERY_S if order.stuck else ORDER_POLL_EVERY_S)
        elif order.is_terminal and t.trades_due is not None and now >= t.trades_due - _TIME_EPS:
            t.trades_due = None
            if order.broker_order_id:
                t.trades_tries += 1
                fx.commands.append(FetchTrades(order.account, order.order_pk, order.broker_order_id))
    return _finish(order, prev, OrderEvent.TIMER, to, fx)


def timers_after_reload(order: Order, now_mono: float, *, restart: bool = True,
                        late_until: float | None = None) -> tuple[OrderTimers, list[Command]]:
    """Fresh timers for an order loaded by Engine.load (its submitted_mono re-anchored to now), and the re-poll
    of §3.3: SUBMITTING/UNKNOWN look the tag up at +1/+3/+7/+15 s (not found every time -> NOT_PLACED, §5.10);
    WORKING polls now (cancel at +6 s); CANCEL_REQUESTED polls now and re-sends the Cancel (the first may never have
    left). Every SUBMITTING and UNKNOWN order gets a late-accept window (decision B2): its Place may have reached
    Kite, so no NOT_PLACED and no second attempt on its leg before the window ends.

    restart=False is the core loop's rollback-and-reload inside one process (submitted_mono kept): the window ends
    at late_until, the deadline the order's timers held before the reload (the engine keeps it for the process,
    EngineContext.late_until), so a reload never cuts short the LATE_ACCEPT_S that follow an AMBIGUOUS answer
    (review round 3: rebuilt from the submit, it ended early when the answer came late, after a wait in the FIFO
    order lane). Without one (a Place still in flight, no answer yet) it counts from the submit: LATE_ACCEPT_S after
    PLACE_TIMEOUT_S, the answer's latest normal time.

    A process restart (restart=True) cannot tell how long ago such an order went out, and its Place may have
    reached Kite just before the crash: an UNKNOWN order, and a SUBMITTING one (the Place may have been sent, its
    answer lost with the process), gets a full late-accept window from now (decision B2, review round 2), so
    NOT_PLACED comes no earlier than LATE_ACCEPT_S after the restart and no second SELL goes out while the first
    may still be accepted."""
    t = OrderTimers()
    _submitted(order, now_mono)
    s = order.state
    if s in (OrderState.SUBMITTING, OrderState.UNKNOWN):
        _start_lookups(t, now_mono)
        if restart:
            until = now_mono + LATE_ACCEPT_S
        elif _finite(late_until):
            until = float(late_until)
        else:
            until = order.submitted_mono + PLACE_TIMEOUT_S + LATE_ACCEPT_S
        _late_window(t, now_mono, until)
        return t, []
    if s is OrderState.WORKING:
        t.next_poll = now_mono + ORDER_POLL_EVERY_S
        return t, [_poll(order)]
    if s is OrderState.CANCEL_REQUESTED:
        t.cancel_sent = now_mono
        t.next_poll = now_mono + ORDER_POLL_EVERY_S
        return t, [Cancel(order.account, order.order_pk, order.broker_order_id), _poll(order)]
    return t, []


def handle(store: Store, order: Order, leg: Leg, ev: object, t: OrderTimers, now_mono: float, now_wall: datetime,
           *, token_ok: bool = True) -> Effects:
    """Route one engine event about this order to its handler."""
    if isinstance(ev, PlaceResult):
        return on_place_result(store, order, leg, ev, t, now_mono, now_wall)
    if isinstance(ev, OrderSnapshot):
        return on_snapshot(store, order, leg, ev, t, now_mono, now_wall)
    if isinstance(ev, LookupResult):
        return on_lookup_result(store, order, leg, ev, t, now_mono, now_wall)
    if isinstance(ev, CancelResult):
        return on_cancel_result(store, order, ev, t, now_mono, now_wall)
    if isinstance(ev, TradesResult):
        return on_trades_result(store, order, ev, t, now_mono, now_wall)
    if isinstance(ev, Timer):
        return on_timer(store, order, t, now_mono, now_wall, token_ok=token_ok)
    raise TypeError(f"{type(ev).__name__} is not an order event")


def adopted_report(o: Order, tag: str, broker_id: str | None) -> bool:
    """A report of a broker order the engine adopted under a new tag (Engine._adopt_late_fill: the fills of a bot
    order it had recorded NOT_PLACED): its broker id is the row's, its tag a bot tag of the same role and account."""
    p = parse_tag(tag)
    return (bool(broker_id) and broker_id == o.broker_order_id and p is not None and p.role == o.role
            and p.account is Account(o.account))


def find_order(orders: Mapping[str, Order], account: Account | str, *, tag: str | None = None,
               broker_order_id: str | None = None) -> Order | None:
    """The DB order a broker report refers to: by tag (== order_pk) first, else by broker id; same account only.
    A tag whose row has no broker id yields to the row holding the report's broker id: an order adopted under a new
    tag (Engine._adopt_late_fill). None for a report the bot does not own (recon decides what that is)."""
    acct = Account(account)
    if tag:
        o = orders.get(tag)
        if o is not None and Account(o.account) is acct:
            if o.broker_order_id or not broker_order_id:
                return o
            return _by_broker_id(orders, acct, broker_order_id) or o
    if broker_order_id:
        return _by_broker_id(orders, acct, broker_order_id)
    return None


def _by_broker_id(orders: Mapping[str, Order], acct: Account, broker_order_id: str) -> Order | None:
    for o in orders.values():
        if o.broker_order_id == broker_order_id and Account(o.account) is acct:
            return o
    return None
