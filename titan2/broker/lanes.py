"""Lanes: the only threads that call Kite REST (spec §3.2-§3.4).

- OrderLane (thread lane-live-orders): LIVE Place / Cancel / PollOrder / LookupTag / FetchTrades through
  KiteBroker, one call at a time, FIFO.
- ReadsLane (threads lane-reads and lane-reads-quotes): market data, the contract note (any account: Kite's
  charges calculator prices the orders' own parameters, synthetic paper ids included) and LIVE account reads
  through KiteData (no order methods). Quote requests run on their own thread, so a slow account read never
  holds back an EXIT quote refresh. They are batched, up to 500 symbols per call, collected in priority order
  (EXIT first); a quote request that has waited 3 s is answered QuoteResult(ok=False, error="TIMEOUT"). Other
  reads run in priority order. The quote thread also keeps Kite's connection warm (ReadsLane.keep_warm): while
  active() (the runtime's: 09:00-15:30 on a trading day), one ltp() of NIFTY 50 once no quote call went out for
  60 s, its answer thrown away. Every call it makes is measured (QuoteCall: how long it took, Kite's own share,
  the symbols, the seconds since the previous call, a retry, the outcome), logged to the log file only (one INFO
  "quote call:" line, kept off the laptop's console) and kept for the core, which takes them once a minute
  (take_quote_calls) into a LATENCY event for the daily report. Nothing decides on them.
- TokenBucket: shared, thread-safe, blocking pacing that never fails open.
- execute_broker(): the pure mapping command -> port call -> events. Both lanes, the inline test harness and
  paper-sim can use it, so LIVE and PAPER answer the engine with identical events. req_mono stamps a read with
  the time it was asked: paper-sim answers a positions or orders read after its seeded delay, but stamps it with
  its receipt. Decision B1 makes every exit wait for a positions read requested after the exit decision, and LIVE
  pays that round trip on lane-reads, so paper must pay it too.

Every command gets an answer event (except PollOrder, whose failure the engine's poll timer covers): a failed or
refused command is answered with the conservative event from failure_events(), never with silence.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from titan2.broker.port import (
    AMBIGUOUS, LOCAL, ORDER_LANE_THREAD, READS_LANE_THREAD, REJECTED, ReadFailed, format_error, snapshot_of,
)
from titan2.core.events import (
    Cancel, CancelResult, Command, ContractNote, ContractNoteResult, Event, FetchHistorical, FetchMargins,
    FetchOrders, FetchPositions, FetchQuotes, FetchSquareoffs, FetchTradebook, FetchTrades, HistoricalResult,
    LookupResult, LookupTag, MarginsResult, OrdersSnapshot, Place, PlaceResult, PollOrder, PositionsSnapshot, Prep,
    PrepResult, Priority, QuoteResult, SquareoffsResult, TradebookSnapshot, TradesResult,
)
from titan2.model import Account, InvariantViolation, Quote
from titan2.settings import QUOTE_REQUEST_TIMEOUT_S, REST_BATCH      # code constants (DETAIL §8.4)

log = logging.getLogger(__name__)

QUOTES_LANE_THREAD = f"{READS_LANE_THREAD}-quotes"                               # ReadsLane's quote() worker
# The keep-warm call (ReadsLane.keep_warm). Kite, or a router on the way, drops a connection that has been idle for a
#   few minutes, and the next quote() then waits for a new one: on 2026-10-13/14 a quote call made after 5+ idle
#   minutes took a median 0.98 s (33 calls, 16 over 1 s), one on a warm connection 0.11 s (52 calls); 2 in 5 entry
#   quotes were cold, the 09:20 batch among them (2.0 s late). One call a minute is about 1% of the quote budget.
KEEP_WARM_IDLE_S = 60.0                  # a throwaway ltp() once no quote call went out for this long ...
KEEP_WARM_SYMBOLS = ("NSE:NIFTY 50",)    # ... of this symbol; its answer reaches no cache and no decision
KEEP_WARM_RECHECK_S = 5.0                # while active() says no (or fails), it is asked again this often
# The measurement of every quote-client call (QuoteCall, 2026-10-14 night: the self-audit had to rebuild every REST
#   latency by hand from receipt stamps). The core takes them once a minute; past this many untaken the oldest go.
QUOTE_CALLS_KEPT = 5000
QUOTE, WARM = "QUOTE", "WARM"            # QuoteCall.kind: a quote() batch, or the keep-warm ltp()
OFF_CONSOLE = {"titan2_console": False}  # logging extra: the file has the line, the laptop's console does not
#                                          (laptop.ON_CONSOLE is the attribute's name)

ORDER_COMMANDS = (Place, Cancel, PollOrder, LookupTag, FetchTrades)              # LIVE -> lane-live-orders
ACCOUNT_READ_COMMANDS = (FetchPositions, FetchOrders, FetchTradebook, FetchMargins)  # LIVE -> lane-reads
# always lane-reads. ContractNote too (§4.2, §8.1): it reads no account state, and the PAPER sample must be priced
# by Zerodha's calculator, never by the paper exchange (which could only bill it with titan2.charges itself).
MARKET_READ_COMMANDS = (FetchQuotes, FetchHistorical, Prep, ContractNote)
# PAPER and SHADOW only: the paper exchange's own records of earlier days (the §9.6 step 7 backstop, review round 2).
#   Kite has nothing to answer them with; the engine never asks them for LIVE.
PAPER_READ_COMMANDS = (FetchSquareoffs,)
PAPER_ACCOUNTS = frozenset({Account.PAPER, Account.SHADOW})

Post = Callable[[Event], None]
PrepHandler = Callable[[], "tuple[bool, str]"]


def lane_for(cmd: Command) -> str | None:
    """Where a command is executed: "orders", "reads", "paper" (paper-sim) or None (not a broker/market command)."""
    if isinstance(cmd, PAPER_READ_COMMANDS):
        return "paper" if cmd.account in PAPER_ACCOUNTS else None
    if isinstance(cmd, ORDER_COMMANDS + ACCOUNT_READ_COMMANDS):
        if cmd.account in PAPER_ACCOUNTS:
            return "paper"
        return "orders" if isinstance(cmd, ORDER_COMMANDS) else "reads"
    if isinstance(cmd, MARKET_READ_COMMANDS):
        return "reads"
    return None


# ---------------------------------------------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------------------------------------------
_TOKEN_EPS = 1e-9
_MIN_WAIT_S = 0.001


class TokenBucket:
    """Blocking rate limiter shared by threads. acquire() returns only once a token is taken; it never fails open.

    rate tokens/s refill a bucket of `burst`. window_cap optionally also bounds grants in any rolling window_s
    (used for orders: the SEBI retail-algo limit is 10 orders/s). A clock that stands still or runs backwards adds
    no tokens. If sleep() raises, the exception propagates and no token is granted.
    """

    def __init__(self, name: str, rate: float, burst: int = 1, *, window_cap: int | None = None,
                 window_s: float = 1.0, mono: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not (isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate) and rate > 0):
            raise ValueError(f"bucket {name}: rate must be a finite number > 0, got {rate!r}")
        if not (isinstance(burst, int) and not isinstance(burst, bool) and burst >= 1):
            raise ValueError(f"bucket {name}: burst must be an int >= 1, got {burst!r}")
        if window_cap is not None and not (isinstance(window_cap, int) and not isinstance(window_cap, bool)
                                           and window_cap >= 1):
            raise ValueError(f"bucket {name}: window_cap must be None or an int >= 1, got {window_cap!r}")
        if not (isinstance(window_s, (int, float)) and math.isfinite(window_s) and window_s > 0):
            raise ValueError(f"bucket {name}: window_s must be a finite number > 0, got {window_s!r}")
        self.name = name
        self.rate = float(rate)
        self.burst = burst
        self.window_cap = window_cap
        self.window_s = float(window_s)
        self._mono = mono
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._stamp = mono()
        self._grants: deque[float] = deque()

    def _take_or_wait(self, now: float) -> float:
        """Take a token and return 0.0, or return how long to wait first. The caller holds the lock."""
        if now > self._stamp:
            self._tokens = min(float(self.burst), self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
        wait = 0.0
        if self._tokens < 1.0 - _TOKEN_EPS:
            wait = max((1.0 - self._tokens) / self.rate, _MIN_WAIT_S)
        if self.window_cap is not None:
            while self._grants and self._grants[0] < now - self.window_s:
                self._grants.popleft()
            if len(self._grants) >= self.window_cap:
                wait = max(wait, self._grants[0] + self.window_s - now, _MIN_WAIT_S)
        if wait > 0.0:
            return wait
        self._tokens -= 1.0
        if self.window_cap is not None:
            self._grants.append(now)
        return 0.0

    def acquire(self, timeout: float | None = None) -> bool:
        """Block until a token is taken and return True. With a timeout, return False once it cannot be met.

        False means "do not make the call" -- a caller never proceeds without a token.
        """
        deadline = None if timeout is None else self._mono() + max(0.0, float(timeout))
        while True:
            with self._lock:
                now = self._mono()
                wait = self._take_or_wait(now)
            if wait <= 0.0:
                return True
            if deadline is not None and now + wait > deadline:
                return False
            self._sleep(wait)


# ---------------------------------------------------------------------------------------------------------------
# Command -> events (pure mapping over a port)
# ---------------------------------------------------------------------------------------------------------------
def read_error_text(exc: BaseException) -> str:
    """The event error text of a failed read: the adapter's classified text, else AMBIGUOUS:<type>."""
    if isinstance(exc, ReadFailed):
        return exc.text
    return format_error(AMBIGUOUS, type(exc).__name__, str(exc))


def failure_events(cmd: Command, error: str, now_mono: float, *, outcome: str = AMBIGUOUS,
                   error_class: str | None = None) -> list[Event]:
    """The conservative answer to a command that failed or was refused: nothing may be inferred from it.

    outcome applies to Place / Cancel: REJECTED when it certainly was not sent (a local refusal), AMBIGUOUS when
    it might have been. PollOrder has no failure event (the engine's poll timer retries).
    """
    if isinstance(cmd, Place):
        cls = error_class or (LOCAL if outcome == REJECTED else None)
        return [PlaceResult(cmd.account, cmd.order_pk, outcome, None, cls, error)]
    if isinstance(cmd, Cancel):
        return [CancelResult(cmd.account, cmd.order_pk, outcome, error)]
    if isinstance(cmd, PollOrder):
        return []
    if isinstance(cmd, LookupTag):
        return [LookupResult(cmd.account, cmd.order_pk, False, None)]
    if isinstance(cmd, FetchTrades):
        return [TradesResult(cmd.account, cmd.order_pk, False, ())]
    if isinstance(cmd, FetchPositions):
        return [PositionsSnapshot(cmd.account, False, (), now_mono, now_mono, error)]
    if isinstance(cmd, FetchOrders):
        return [OrdersSnapshot(cmd.account, False, (), now_mono, now_mono, error)]
    if isinstance(cmd, FetchTradebook):
        return [TradebookSnapshot(cmd.account, False, (), now_mono)]
    if isinstance(cmd, FetchMargins):
        return [MarginsResult(cmd.account, False, None, now_mono)]
    if isinstance(cmd, FetchSquareoffs):
        return [SquareoffsResult(cmd.account, cmd.request_id, False)]
    if isinstance(cmd, ContractNote):
        return [ContractNoteResult(cmd.account, False, ())]
    if isinstance(cmd, FetchQuotes):
        missing = tuple(cmd.symbols) if isinstance(cmd.symbols, (tuple, list)) else ()
        return [QuoteResult(cmd.request_id, False, (), missing, error)]
    if isinstance(cmd, FetchHistorical):
        return [HistoricalResult(cmd.request_id, cmd.symbol, False, ())]
    if isinstance(cmd, Prep):
        return [PrepResult(cmd.kind, False, error)]
    log.error("no failure event for %r (%s)", cmd, error)
    return []


def _find_by_tag(orders: Iterable, tag: str):
    """(found, ok): the one broker order carrying tag; two orders with one tag is an anomaly (ok=False)."""
    hits = [o for o in orders if o.tag == tag]
    if len(hits) > 1:
        log.error("tag %s matches %d broker orders %s; refusing to pick one", tag, len(hits),
                  [o.broker_order_id for o in hits])
        return None, False
    return (hits[0] if hits else None), True


def execute_broker(cmd: Command, port, mono: Callable[[], float], *, poll_source: str = "POLL",
                   req_mono: float | None = None) -> list[Event]:
    """Run one broker command against a port and return its events. No queues, no threads.

    port is a BrokerPort (KiteBroker, PaperExchange) or, for the account reads only, KiteData. place()/cancel()
    return classified results themselves; a read that raises becomes its ok=False event. An exception escaping
    here is a bug -- an InvariantViolation (e.g. I2) is never turned into a broker answer: threaded callers
    convert it with failure_events(), inline callers see it.
    poll_source labels PollOrder snapshots ("POLL" for Kite; paper-sim passes "SIM"). req_mono: when the read was
    asked, if earlier than now (paper-sim's delayed positions and orders reads pass their receipt time).
    """
    if cmd.account not in getattr(port, "accounts", frozenset()):
        msg = f"{LOCAL}: {type(port).__name__} does not serve account {cmd.account}"
        log.error("%s (%r)", msg, cmd)
        return failure_events(cmd, msg, mono(), outcome=REJECTED)
    if isinstance(cmd, Place):
        return [port.place(cmd.req)]
    if isinstance(cmd, Cancel):
        return [port.cancel(cmd.account, cmd.order_pk, cmd.broker_order_id)]
    # req_mono is taken before the call (so before any bucket wait inside it): it can only be earlier than the
    # real send, which is the conservative side of the post-fill rule (§9.2) and of a tag lookup's late window (a
    # miss makes its order NOT_PLACED only when its read was asked after the window ended).
    if req_mono is None:
        req_mono = mono()
    try:
        return _read_events(cmd, port, mono, req_mono, poll_source)
    except InvariantViolation:
        raise
    except Exception as exc:
        error = read_error_text(exc)
        log.warning("%s %s failed: %s", type(cmd).__name__, getattr(cmd, "order_pk", cmd.account), error)
        acct, now = cmd.account, mono()
        if isinstance(cmd, FetchPositions):
            return [PositionsSnapshot(acct, False, (), req_mono, now, error)]
        if isinstance(cmd, FetchOrders):
            return [OrdersSnapshot(acct, False, (), req_mono, now, error)]
        if isinstance(cmd, FetchTradebook):
            return [TradebookSnapshot(acct, False, (), req_mono)]
        return failure_events(cmd, error, now)


def _read_events(cmd: Command, port, mono: Callable[[], float], req_mono: float, poll_source: str) -> list[Event]:
    acct = cmd.account
    if isinstance(cmd, PollOrder):
        if cmd.broker_order_id:
            order = port.order_history(acct, cmd.broker_order_id)
        else:
            order, _ = _find_by_tag(port.orders_today(acct), cmd.order_pk)
        if order is None:
            log.warning("PollOrder %s: no broker state for %s", cmd.order_pk, cmd.broker_order_id)
            return []
        return [snapshot_of(order, poll_source, mono())]
    if isinstance(cmd, LookupTag):
        found, ok = _find_by_tag(port.orders_today(acct), cmd.order_pk)
        return [LookupResult(acct, cmd.order_pk, ok, found, req_mono)]
    if isinstance(cmd, FetchTrades):
        return [TradesResult(acct, cmd.order_pk, True, tuple(port.order_trades(acct, cmd.broker_order_id)))]
    if isinstance(cmd, FetchPositions):
        return [PositionsSnapshot(acct, True, tuple(port.positions(acct)), req_mono, mono())]
    if isinstance(cmd, FetchOrders):
        return [OrdersSnapshot(acct, True, tuple(port.orders_today(acct)), req_mono, mono())]
    if isinstance(cmd, FetchTradebook):
        return [TradebookSnapshot(acct, True, tuple(port.trades_today(acct)), req_mono)]
    if isinstance(cmd, FetchMargins):
        return [MarginsResult(acct, True, port.margins(acct), mono())]
    if isinstance(cmd, FetchSquareoffs):
        orders, carried = port.squareoffs(acct, cmd.symbols, cmd.since)
        return [SquareoffsResult(acct, cmd.request_id, True, tuple(orders), tuple(sorted(carried.items())))]
    if isinstance(cmd, ContractNote):
        return [ContractNoteResult(acct, True, tuple(port.contract_note(acct, cmd.orders)))]
    raise TypeError(f"execute_broker cannot run {type(cmd).__name__}")


# ---------------------------------------------------------------------------------------------------------------
# OrderLane (lane-live-orders)
# ---------------------------------------------------------------------------------------------------------------
_STOP = object()


class OrderLane:
    """LIVE order commands through KiteBroker, one call at a time, in submission order."""

    def __init__(self, broker, post: Post, *, mono: Callable[[], float] = time.monotonic) -> None:
        self.broker = broker
        self._post = post
        self._mono = mono
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def submit(self, cmd: Command) -> None:
        """Queue a command (any thread). A mis-routed command is answered at once with a refusal, never dropped."""
        problem = None
        if not isinstance(cmd, ORDER_COMMANDS):
            problem = f"{type(cmd).__name__} is not an order-lane command"
        elif cmd.account not in getattr(self.broker, "accounts", frozenset()):
            problem = f"order lane serves {sorted(getattr(self.broker, 'accounts', ()))}, not {cmd.account}"
        if problem:
            msg = f"{LOCAL}: {problem}"
            log.error("%s (%r)", msg, cmd)
            self._post_all(failure_events(cmd, msg, self._mono(), outcome=REJECTED))
            return
        self._q.put(cmd)

    def execute(self, cmd: Command) -> list[Event]:
        """Run one command now and return its events (inline in tests; on the lane thread in production)."""
        return execute_broker(cmd, self.broker, self._mono)

    def _execute_safely(self, cmd: Command) -> list[Event]:
        try:
            return self.execute(cmd)
        except Exception as exc:
            error = format_error(AMBIGUOUS, type(exc).__name__, str(exc))
            log.critical("order lane: %s %s raised; answering AMBIGUOUS: %s", type(cmd).__name__,
                         getattr(cmd, "order_pk", ""), error, exc_info=True)
            return failure_events(cmd, error, self._mono(), error_class=type(exc).__name__)

    def _post_all(self, events: Iterable[Event]) -> None:
        for ev in events:
            self._post(ev)

    def drain(self) -> list[Event]:
        """Inline mode: run every queued command now, post and return the events. Not for use with start()."""
        out: list[Event] = []
        while True:
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                return out
            if cmd is _STOP:
                continue
            events = self.execute(cmd)
            self._post_all(events)
            out.extend(events)

    def run(self) -> None:
        while True:
            cmd = self._q.get()
            if cmd is _STOP:
                return
            self._post_all(self._execute_safely(cmd))

    def start(self) -> threading.Thread:
        if self._thread is not None:
            raise RuntimeError("order lane already started")
        self._thread = threading.Thread(target=self.run, name=ORDER_LANE_THREAD, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, join_s: float | None = None) -> None:
        self._q.put(_STOP)
        if self._thread is not None and join_s is not None:
            self._thread.join(join_s)


# ---------------------------------------------------------------------------------------------------------------
# ReadsLane (lane-reads)
# ---------------------------------------------------------------------------------------------------------------
def read_priority(cmd: Command) -> int:
    """§3.2 priority of a read (lowest served first). Commands without a priority field use their fixed class."""
    if isinstance(cmd, (FetchQuotes, FetchPositions, FetchOrders)):
        return cmd.priority
    if isinstance(cmd, (FetchTradebook, FetchMargins)):
        return int(Priority.RECON)
    return int(Priority.BACKGROUND)          # FetchHistorical, Prep, ContractNote


@dataclass(frozen=True, slots=True)
class QuoteCall:
    """One measured call of the quote client. mono: when it went out. ms: the whole call as the lane saw it, the wait
    for the 1-a-second token included (what an entry or an exit refresh waited). kite_ms: Kite's own share
    (KiteData.last_quote; None from a client that does not say). symbols: how many it asked. idle_s: seconds since
    the previous quote or keep-warm call went out (None: the first of this process); a long one means a cold
    connection. retried: asked again once after a dropped connection (None: not known). outcome: OK, MISSING <k>
    (Kite left k symbols out) or the failure's class, e.g. AMBIGUOUS:ConnectionError. kind: QUOTE or WARM."""

    mono: float
    ms: float
    kite_ms: float | None
    symbols: int
    idle_s: float | None
    retried: bool | None
    outcome: str
    kind: str = QUOTE


def failure_kind(error: str | None) -> str:
    """The class of a failed read's text (format_error: '<OUTCOME>:<name>: <message>') without its message:
    'AMBIGUOUS:ConnectionError: ...' -> 'AMBIGUOUS:ConnectionError'."""
    head = ":".join(str(error or "").split(":", 2)[:2]).strip()
    return head or "FAILED"


@dataclass(slots=True, eq=False)
class _QuoteJob:
    request_id: str
    priority: int
    seq: int
    submitted: float
    reply: Post
    pending: list[str]                      # not yet asked
    asking: list[str] = field(default_factory=list)   # in the call being made
    got: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def result(self, ok: bool, error: str | None = None) -> QuoteResult:
        not_got = self.missing + self.asking + self.pending
        return QuoteResult(self.request_id, ok, tuple(self.got), tuple(not_got), error)


class ReadsLane:
    """Market data and LIVE account reads in priority order, with quote batching and the 3 s quote timeout.

    Two workers share the queues: lane-reads-quotes makes the quote() calls and lane-reads every other read. A thin
    option's EXIT refresh has about 2 s before decide() closes the leg on its stale quote (§7.4), while positions()
    or orders() can stall for Kite's whole 5 s client timeout, so neither may queue behind the other. step() serves
    both inline (tests): the most urgent work of either, and the time a read takes is not counted against the quote
    requests waiting meanwhile, as it is not on the threads.

    data is KiteData (or a stand-in): it must not have order methods. quote_data, if given, is the KiteData the quote
    worker calls (e.g. on a Kite client of its own); else data. quote_sink receives every REST Quote before its
    QuoteResult is posted (the runtime binds it to the QuoteCache). prep maps a Prep kind to a handler returning
    (ok, detail). reply callables let paper-sim receive its own QuoteResults; the default is post. active, if
    given, says when the quote worker keeps Kite's connection warm (keep_warm); without it, it never does.
    """

    def __init__(self, data, post: Post, quote_sink: Callable[[Quote], None], *,
                 prep: Mapping[str, PrepHandler] | None = None, mono: Callable[[], float] = time.monotonic,
                 quote_timeout_s: float = QUOTE_REQUEST_TIMEOUT_S, max_batch: int = REST_BATCH,
                 quote_data=None, active: Callable[[], bool] | None = None,
                 keep_warm_s: float = KEEP_WARM_IDLE_S) -> None:
        for source in (data, quote_data):
            for name in ("place", "cancel", "order_history", "order_trades"):
                if callable(getattr(source, name, None)):
                    raise TypeError(f"the reads lane must get KiteData, not an object with order method {name}()")
        if not (isinstance(max_batch, int) and 1 <= max_batch <= REST_BATCH):
            raise ValueError(f"max_batch must be 1..{REST_BATCH}, got {max_batch!r}")
        if not (isinstance(quote_timeout_s, (int, float)) and math.isfinite(quote_timeout_s) and quote_timeout_s > 0):
            raise ValueError(f"quote_timeout_s must be a finite number > 0, got {quote_timeout_s!r}")
        if not (isinstance(keep_warm_s, (int, float)) and math.isfinite(keep_warm_s) and keep_warm_s > 0):
            raise ValueError(f"keep_warm_s must be a finite number > 0, got {keep_warm_s!r}")
        self.data = data
        self.quote_data = data if quote_data is None else quote_data
        self._post = post
        self._sink = quote_sink
        self._prep = dict(prep or {})
        self._mono = mono
        self._timeout = float(quote_timeout_s)
        self._max_batch = max_batch
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._qjobs: list[_QuoteJob] = []
        self._rjobs: list[tuple[int, int, Command, Post]] = []      # heap of (priority, seq, cmd, reply)
        self._stopping = False
        self._thread: threading.Thread | None = None                # lane-reads
        self._quote_thread: threading.Thread | None = None          # lane-reads-quotes
        self.active = active                                        # keep_warm's hours (None: never)
        self._keep_warm_s = float(keep_warm_s)
        self._last_quote_call: float | None = None                  # when the last quote() or keep-warm ltp() went out
        self._warm_at = mono()                                      # keep_warm's next look (nothing asked yet: now)
        self.keep_warm_calls = 0
        self.keep_warm_failures = 0
        self._quote_calls: deque[QuoteCall] = deque(maxlen=QUOTE_CALLS_KEPT)   # measured, not yet taken by the core

    # -- submission (any thread) -------------------------------------------------------------------------------
    def submit(self, cmd: Command, reply: Post | None = None) -> None:
        reply = reply or self._post
        now = self._mono()
        problem = self._problem(cmd)
        if problem:
            msg = f"{LOCAL}: {problem}"
            log.error("%s (%r)", msg, cmd)
            for ev in failure_events(cmd, msg, now, outcome=REJECTED):
                reply(ev)
            return
        prio = read_priority(cmd)
        with self._cv:
            seq = next(self._seq)
            if isinstance(cmd, FetchQuotes):
                symbols = list(dict.fromkeys(cmd.symbols))
                self._qjobs.append(_QuoteJob(cmd.request_id, prio, seq, now, reply, symbols))
            else:
                heapq.heappush(self._rjobs, (prio, seq, cmd, reply))
            self._cv.notify_all()                        # two workers wait on this condition

    def _problem(self, cmd: Command) -> str | None:
        if not isinstance(cmd, ACCOUNT_READ_COMMANDS + MARKET_READ_COMMANDS):
            return f"{type(cmd).__name__} is not a reads-lane command"
        if isinstance(cmd, ACCOUNT_READ_COMMANDS) and cmd.account not in getattr(self.data, "accounts", frozenset()):
            return f"reads lane does not serve account {cmd.account}"
        prio = read_priority(cmd)
        if isinstance(prio, bool) or not isinstance(prio, int) or prio not in {int(p) for p in Priority}:
            return f"bad priority {prio!r}"
        if isinstance(cmd, FetchQuotes):
            syms = cmd.symbols
            if not isinstance(syms, (tuple, list)) or not all(isinstance(s, str) and s for s in syms):
                return f"bad symbols {syms!r}"
        return None

    def backlog(self) -> int:
        """Requests not yet answered."""
        with self._cv:
            return len(self._qjobs) + len(self._rjobs)

    # -- one unit of work --------------------------------------------------------------------------------------
    def step(self) -> list[Event]:
        """Inline: answer expired/empty quote requests, then make at most one broker call (the most urgent work of
        either worker). The time a read takes is then taken off the wait of the quote requests queued during it: on
        the threads a quote request never waits for a read.

        Delivers every event through its reply callable and returns them. A taken request is always answered.
        """
        with self._cv:
            out = self._expire(self._mono())
            kind, work = self._take_next()
        if kind == "quotes":
            out.extend(self._run_quote_batch(*work))
        elif kind == "read":
            started = self._mono()
            out.extend(self._answer_read(*work))
            self._exclude_read_time(started, self._mono())
        return self._deliver(out)

    def _step_quotes(self) -> list[Event]:
        """lane-reads-quotes: answer expired/empty quote requests, then make at most one quote() call; with none to
        make, the keep-warm call when it is due."""
        with self._cv:
            out = self._expire(self._mono())
            work = self._take_quotes()
        if work is not None:
            out.extend(self._run_quote_batch(*work))
        else:
            self.keep_warm()
        return self._deliver(out)

    def keep_warm(self) -> bool:
        """lane-reads-quotes, when it has no quote to ask: while active() says so, one ltp() of KEEP_WARM_SYMBOLS on
        the quote client once no quote call went out for keep_warm_s, so the next real quote (an entry's, an exit
        refresh's) finds an open connection instead of waiting for a new one (KEEP_WARM_IDLE_S says why). It takes a
        token of the same quote bucket as every quote() (KiteData.ltp), and its answer is thrown away: it never
        reaches the quote sink, so no entry or exit decision sees it. Only a failure is logged (it is measured too,
        as a WARM QuoteCall, for the daily report). Returns whether a call was made."""
        now = self._mono()
        with self._cv:
            if self.active is None or now < self._warm_at:
                return False
            self._warm_at = now + KEEP_WARM_RECHECK_S              # whatever happens below: no hot loop
        try:
            active = bool(self.active())
        except Exception:
            log.exception("quote keep-warm: active() failed; no keep-warm call")
            return False
        if not active:
            return False
        with self._cv:
            last = self._last_quote_call
            if last is not None and now - last < self._keep_warm_s:     # a quote call went out meanwhile
                self._warm_at = last + self._keep_warm_s
                return False
            self._last_quote_call, self._warm_at = now, now + self._keep_warm_s
        self.keep_warm_calls += 1
        outcome, went = "OK", self._mono()
        try:
            self.quote_data.ltp(list(KEEP_WARM_SYMBOLS))
        except Exception as exc:
            self.keep_warm_failures += 1
            error = read_error_text(exc)
            outcome = failure_kind(error)
            log.warning("quote keep-warm call failed (%d so far; the next quote may wait for a new connection): %s",
                        self.keep_warm_failures, error)
        self._keep(QuoteCall(went, (self._mono() - went) * 1000.0, None, len(KEEP_WARM_SYMBOLS),
                             None if last is None else now - last, None, outcome, WARM))
        return True

    def take_quote_calls(self) -> list[QuoteCall]:
        """The calls measured since the last take, oldest first (any thread; EngineContext.take_quote_calls: the core
        writes them into its once-a-minute LATENCY event)."""
        with self._cv:
            out = list(self._quote_calls)
            self._quote_calls.clear()
        return out

    def _keep(self, call: QuoteCall) -> None:
        with self._cv:
            self._quote_calls.append(call)

    def _measured(self, went: float, idle_s: float | None, symbols: int, outcome: str | None,
                  batch: Iterable[str] = (), quotes: Mapping | None = None) -> QuoteCall | None:
        """The QuoteCall of the quote() that went out at `went` (now it is over), kept for the core and logged to the
        file only: one INFO line a call, e.g. 'quote call: 3 symbols in 1984 ms (Kite 1978 ms), 412.3 s after the
        previous call (cold): OK'. outcome None: Kite answered `quotes` for `batch` (OK, or MISSING <k>). Information
        only, so it never raises: a quote is never lost to its own measurement."""
        try:
            ms = (self._mono() - went) * 1000.0
            if outcome is None:
                missing = sum(1 for sym in batch if sym not in (quotes or {}))
                outcome = f"MISSING {missing}" if missing else "OK"
            timing = getattr(self.quote_data, "last_quote", None)
            kite_ms = retried = None
            if timing is not None and getattr(timing, "tries", 0):
                kite_ms, retried = float(timing.kite_s) * 1000.0, timing.tries > 1
            call = QuoteCall(went, ms, kite_ms, symbols, idle_s, retried, outcome)
            self._keep(call)
            parts = [f"{symbols} symbol{'' if symbols == 1 else 's'} in {ms:.0f} ms"]
            if kite_ms is not None:
                wait = ms - kite_ms
                parts[0] += (f" (Kite {kite_ms:.0f} ms, {wait:.0f} ms waiting for the 1-a-second token)"
                             if wait >= 50.0 else f" (Kite {kite_ms:.0f} ms)")
            if idle_s is None:
                parts.append("the first of this run")
            else:
                parts.append(f"{idle_s:.1f} s after the previous call"
                             + (" (cold)" if idle_s >= self._keep_warm_s else ""))
            if retried:
                parts.append("retried once after a dropped connection")
            log.info("quote call: %s: %s", ", ".join(parts), outcome, extra=OFF_CONSOLE)
            return call
        except Exception:
            log.exception("quote call measurement failed (the quote itself is unaffected)")
            return None

    def _warm_due(self) -> bool:
        """Caller holds the lock: the quote worker has a keep-warm look to make."""
        return self.active is not None and self._mono() >= self._warm_at

    def _quote_call_out(self) -> tuple[float, float | None]:
        """A quote() call is going out: the keep-warm call waits keep_warm_s from now. Returns (now, the seconds since
        the previous quote or keep-warm call went out; None for the first)."""
        now = self._mono()
        with self._cv:
            last = self._last_quote_call
            self._last_quote_call, self._warm_at = now, now + self._keep_warm_s
        return now, None if last is None else now - last

    def _step_reads(self) -> list[Event]:
        """lane-reads: the most urgent read that is not a quote request."""
        with self._cv:
            if not self._rjobs:
                return []
            work = self._pop_read()
        return self._deliver(self._answer_read(*work))

    def _answer_read(self, cmd: Command, reply: Post) -> list[tuple[Post, Event]]:
        try:
            events = self._run_read(cmd)
        except Exception as exc:
            error = format_error(AMBIGUOUS, type(exc).__name__, str(exc))
            log.critical("reads lane: %s raised; answering ok=False: %s", type(cmd).__name__, error, exc_info=True)
            events = failure_events(cmd, error, self._mono())
        return [(reply, ev) for ev in events]

    def _exclude_read_time(self, started: float, ended: float) -> None:
        """Inline only: move each waiting quote request's start past the read that ran [started, ended]."""
        with self._cv:
            for job in self._qjobs:
                job.submitted += max(0.0, ended - max(job.submitted, started))

    @staticmethod
    def _deliver(out: Iterable[tuple[Post, Event]]) -> list[Event]:
        delivered = []
        for reply, ev in out:
            try:
                reply(ev)
            except Exception:
                log.exception("reads lane: delivering %s failed", type(ev).__name__)
            delivered.append(ev)
        return delivered

    def drain(self, max_steps: int = 10_000) -> list[Event]:
        """Inline mode: step until no work is left. Not for use with start()."""
        events: list[Event] = []
        for _ in range(max_steps):
            if not self.backlog():
                return events
            events.extend(self.step())
        raise RuntimeError(f"reads lane still busy after {max_steps} steps")

    def execute(self, cmd: Command) -> list[Event]:
        """Run one read now, outside the queue, and return its events (quotes in batches of max_batch)."""
        problem = self._problem(cmd)
        if problem:
            return failure_events(cmd, f"{LOCAL}: {problem}", self._mono(), outcome=REJECTED)
        if not isinstance(cmd, FetchQuotes):
            return self._run_read(cmd)
        job = _QuoteJob(cmd.request_id, read_priority(cmd), -1, self._mono(), self._post,
                        list(dict.fromkeys(cmd.symbols)))
        while True:
            if not job.pending:
                return [job.result(True)]
            job.asking, job.pending = job.pending[:self._max_batch], job.pending[self._max_batch:]
            done = self._run_quote_batch([job], list(job.asking), registered=False)
            if done:
                return [ev for _, ev in done]

    def _expire(self, now: float) -> list[tuple[Post, Event]]:
        out = []
        for job in list(self._qjobs):
            if not job.pending:                          # an empty request: nothing to ask
                out.append((job.reply, job.result(True)))
                self._qjobs.remove(job)
            elif now - job.submitted >= self._timeout:
                out.append((job.reply, job.result(False, "TIMEOUT")))
                self._qjobs.remove(job)
        return out

    def _take_next(self):
        """Inline: the most urgent work of either worker, a quote batch (members, symbols) or one read (cmd, reply).
        Caller holds the lock."""
        best_q = min((j for j in self._qjobs if j.pending), key=lambda j: (j.priority, j.seq), default=None)
        best_r = self._rjobs[0] if self._rjobs else None
        if best_q is None and best_r is None:
            return None, None
        if best_q is None or (best_r is not None and best_r[:2] < (best_q.priority, best_q.seq)):
            return "read", self._pop_read()
        return "quotes", self._take_quotes()

    def _pop_read(self) -> tuple[Command, Post]:
        _, _, cmd, reply = heapq.heappop(self._rjobs)
        return cmd, reply

    def _take_quotes(self) -> tuple[list[_QuoteJob], list[str]] | None:
        """The next quote batch, symbols collected in priority order up to max_batch; None when nothing is pending.
        Caller holds the lock."""
        batch: dict[str, None] = {}
        members = []
        for job in sorted(self._qjobs, key=lambda j: (j.priority, j.seq)):
            taken = 0
            for sym in job.pending:
                if sym not in batch:
                    if len(batch) >= self._max_batch:
                        break
                    batch[sym] = None
                taken += 1
            if taken:
                job.asking, job.pending = job.pending[:taken], job.pending[taken:]
                members.append(job)
            if len(batch) >= self._max_batch:
                break
        return (members, list(batch)) if members else None

    def _run_quote_batch(self, members: list[_QuoteJob], batch: list[str], *,
                         registered: bool = True) -> list[tuple[Post, Event]]:
        """One quote() call for batch, measured (_measured); feed the sink; finish the members that are complete or
        failed."""
        went, idle = self._quote_call_out()
        answered = False
        try:
            quotes = self.quote_data.quote(batch)
            answered = True
            self._measured(went, idle, len(batch), None, batch, quotes)
            for sym in batch:
                q = quotes.get(sym)
                if q is not None:
                    self._sink(q)
        except Exception as exc:
            error = read_error_text(exc)
            if not answered:
                self._measured(went, idle, len(batch), failure_kind(error))
            log.warning("quote call for %d symbols failed: %s", len(batch), error)
            with self._cv:
                out = [(job.reply, job.result(False, error)) for job in members]
                if registered:
                    for job in members:
                        self._qjobs.remove(job)
            return out
        out = []
        with self._cv:
            for job in members:
                for sym in job.asking:
                    (job.got if sym in quotes else job.missing).append(sym)
                job.asking = []
                if not job.pending:
                    out.append((job.reply, job.result(True)))
                    if registered:
                        self._qjobs.remove(job)
        return out

    def _run_read(self, cmd: Command) -> list[Event]:
        if isinstance(cmd, ACCOUNT_READ_COMMANDS):
            return execute_broker(cmd, self.data, self._mono)
        if isinstance(cmd, ContractNote):
            try:
                rows = tuple(self.data.contract_note(cmd.account, cmd.orders))
            except Exception as exc:                 # e.g. Kite refusing synthetic ids: VCN_PAPER_UNSUPPORTED
                log.warning("contract note %s failed: %s", cmd.account, read_error_text(exc))
                return [ContractNoteResult(cmd.account, False, ())]
            return [ContractNoteResult(cmd.account, True, rows)]
        if isinstance(cmd, FetchHistorical):
            try:
                bars = self.data.historical(cmd.symbol, cmd.token, cmd.frm, cmd.to, cmd.interval)
            except Exception as exc:
                log.warning("historical %s failed: %s", cmd.symbol, read_error_text(exc))
                return [HistoricalResult(cmd.request_id, cmd.symbol, False, ())]
            return [HistoricalResult(cmd.request_id, cmd.symbol, True, tuple(bars))]
        if isinstance(cmd, Prep):
            handler = self._prep.get(cmd.kind)
            if handler is None:
                return [PrepResult(cmd.kind, False, f"{LOCAL}: no prep handler for {cmd.kind}")]
            try:
                ok, detail = handler()
            except Exception as exc:
                error = read_error_text(exc)
                log.error("prep %s failed: %s", cmd.kind, error, exc_info=True)
                return [PrepResult(cmd.kind, False, error)]
            return [PrepResult(cmd.kind, ok is True, str(detail))]
        raise TypeError(f"reads lane cannot run {type(cmd).__name__}")

    # -- threads -----------------------------------------------------------------------------------------------
    def run(self) -> None:
        """The lane-reads loop: every read except quote requests."""
        self._work(self._step_reads, lambda: self._rjobs, "reads")

    def run_quotes(self) -> None:
        """The lane-reads-quotes loop: quote requests, and the keep-warm call when it is due."""
        self._work(self._step_quotes, lambda: self._qjobs or self._warm_due(), "quotes")

    def _work(self, step: Callable[[], list[Event]], has_work: Callable[[], object], name: str) -> None:
        while True:
            with self._cv:
                while not self._stopping and not has_work():
                    self._cv.wait(0.25)
                if self._stopping:
                    return
            try:
                step()
            except Exception:
                log.exception("reads lane %s step failed", name)
                with self._cv:
                    self._cv.wait(0.05)                  # no hot loop on a persistent bug

    def start(self) -> threading.Thread:
        """Start lane-reads and lane-reads-quotes; returns the lane-reads thread."""
        if self._thread is not None:
            raise RuntimeError("reads lane already started")
        self._thread = threading.Thread(target=self.run, name=READS_LANE_THREAD, daemon=True)
        self._quote_thread = threading.Thread(target=self.run_quotes, name=QUOTES_LANE_THREAD, daemon=True)
        self._thread.start()
        self._quote_thread.start()
        return self._thread

    def stop(self, join_s: float | None = None) -> None:
        """Stop both workers; join_s bounds the whole join, not each thread's."""
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if join_s is None:
            return
        deadline = time.monotonic() + join_s
        for t in (self._thread, self._quote_thread):
            if t is not None:
                t.join(max(0.0, deadline - time.monotonic()))
