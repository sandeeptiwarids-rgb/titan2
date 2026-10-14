"""The titan2-bot process: startup (spec §9.5), threads and queues (§3.1-3.2), the core loop (§3.3), command dispatch,
the instance lock, sd_notify and SIGTERM (DETAIL §3, §9.5, §13.2-13.3).

Threads
- core (the main thread): core_loop() below. One store transaction per event; commands are dispatched only after
  the commit. It is the only thread that writes titan2.db (I1: store.CORE_TID is set to it).
- lane-live-orders, lane-reads and lane-reads-quotes (broker/lanes.py), paper-sim (broker/paper.py), alerts
  (alerts.py) and kite-ws (the KiteTicker reactor, market/feed.py) are the other modules' threads; this module builds
  and starts them.
- signals (SignalsHost): signal_q -> 5-min Bars -> strategies -> IntentReceived / BarsClosed on core_q.
- prep (PrepWorker): Prep commands. They run here, not on lane-reads: PREV_BARS is ~200 historical calls (about 70 s
  at Kite's 3/s) and a ban-list fetch may retry for half a minute, and EXIT-priority reads must never queue behind
  them. Prep's Kite reads (KiteData) still take the shared, blocking rate buckets.
- selfcheck: one short-lived thread per RunSelfcheck (the prestart phase runs subprocesses for minutes).

Startup (§9.5): logging; the flock (else exit 73); Python >= 3.10 and an unpatched KiteConnect (else exit 78); a
clean cwd: a module there that shadows an import exits 78, or, when structures may be open, starts SAFE mode (exits
only) with a CRITICAL alert, since exit 78 is never restarted; settings read once (invalid -> code defaults +
HALT(SETTINGS_INVALID)); the store (pre-migration backup, migrations, integrity_check; failure -> exit 78 with a direct
CRITICAL alert); the alerts thread; the calendar: outside a session, after the session's last duty + 5 min (15:45
with the 15:40 close), or after today's EOD DONE is recorded, exit 0 (on a weekday without a session only after
keeping the dead-man check fed until its last tick) unless non-terminal structures exist, which then get recovery
and recon only (a start after BROKER_SQUAREOFF_AT also waits, bounded, for a square-off to be booked: the engine's
late recon). Then the token (profile();
failure -> HALT(TOKEN), which the engine re-checks every 30 s), the components, RecoveryStart, and READY=1 once the
engine's recovery is done; only then the WS feed and the signals thread start. The engine drives prep, the prestart
self-check, StartSignals, the LIVE self-test and the EOD timeline; its Exit(code) ends the loop.

SIGTERM / SIGINT only set a flag; the core loop posts Shutdown, and the engine stops entries, cancels entry orders,
lets in-flight exits finish (<= 20 s) and answers Exit(0). It never flattens: open positions are left for recovery.

Exit codes: 0 done; 3 exception storm (or a crash: systemd restarts, recovery resumes); 73 lock held; 75 controlled
restart (token rotation, dead WS); 78 fatal config or DB (systemd does not restart).

Unclean exits (review F-16): every run ends with a runtime SHUTDOWN event (phase EXITED, IDLE, or CRASHED on the
exception path). A start that finds the newest BOOT without one knows the previous run died (a watchdog SIGABRT, an
OOM kill or SIGKILL, a host failure). Last alive inside that day's session hours: CRITICAL BOT_KILLED and a CRITICAL
UNCLEAN_EXIT event; outside them a WARN UNCLEAN_EXIT event only.

Laptop profile (titan2.laptop, BotProcess(laptop=True)): PAPER only: allow_live is False whatever ALLOW_LIVE says (a
WARNING and a LIVE_REFUSED event name the LIVE-mode strategies, which trade only their PAPER twins), and no LIVE
order of any role leaves the process: the order lane's KiteBroker refuses every place and cancel
(orders_allowed=False), and recovery never imports today's titan2 LIVE orders that this titan2.db lacks (another
titan2's, e.g. EC2's, in the shared Kite account): HALT(UNKNOWN_POSITION, ACCOUNT:LIVE) and a CRITICAL alert instead
(EngineContext.live_orders=False). The runner's Ctrl+C FLATTEN_ALL (requested_by laptop.REQUESTED_BY) is a stop's
(EngineContext.stop_flatten_by): its FLATTEN halt keeps entries off until the run ends flat, then clears; the in-bot
self-check runs phase "laptop" instead of "prestart"; the laptop runner's self-check halts are raised at boot; the
Twisted reactor outlives the run (it cannot be restarted, and the runner restarts the bot in-process on exit 75 or
3); the runner, not this module, installs the signal handlers (two-stage Ctrl+C). On Windows the log file is a plain
FileHandler (there is no logrotate to rename it under a WatchedFileHandler).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import queue
import signal
import socket
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from titan2 import __version__, alerts, plugins, selfcheck, strategies
from titan2 import instruments as ins
from titan2 import laptop as laptop_mod
from titan2 import settings as settings_mod
from titan2 import store as store_mod
from titan2 import token as token_mod
from titan2.broker import lanes
from titan2.broker.kite import KiteBroker, KiteData, assert_kite_unpatched
from titan2.broker.paper import PaperExchange, PaperSim
from titan2.broker.port import LOCAL, REJECTED
from titan2.clock import CalendarError, Clock, RealClock, SessionCalendar, iso, ist_datetime, parse_wall, session_of
from titan2.core.engine import EOD_AFTER_SQUAREOFF_S, Engine
from titan2.core.events import (
    Alert, BarsClosed, Command, Event, Exit, IntentReceived, Prep, PrepResult, RecoveryStart, RunSelfcheck,
    SelfcheckResult, Shutdown, StartSignals, Subscribe, Timer, Unsubscribe,
)
from titan2.core.state import ACCOUNTS, EngineContext, eod_phase_rows
from titan2.market.bars import Bars, prev_session_window
from titan2.market.feed import Feed, FeedLink, SignalQueue, SignalsLost
from titan2.market.quotes import QuoteCache
from titan2.model import TERMINAL_STRUCT, Bar, FeedHealth, InvariantViolation, UnderlyingTick
from titan2.settings import EXC_LIMIT, EXC_WINDOW_S, INTENT_MAX_PER_MIN, RECON_CONFIRM_GAP_S, TIMER_DT

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_EXCEPTION_STORM = 3
EXIT_LOCKED = 73
EXIT_RESTART = 75
EXIT_FATAL = 78

DEFAULT_VAR_DIR = "/var/lib/titan2"
DEFAULT_LOG_DIR = "/var/log/titan2"
LOCK_NAME = "titan2.lock"                   # <var>/run/titan2.lock
LATE_START_GRACE_S = 300.0                  # §9.5 step 6: "after 15:45" = the session's last duty + 5 min
WATCHDOG_FRESH_S = 2.0                      # WATCHDOG=1 only while the last Timer finished < 2 s ago (§3.3)
WATCHDOG_DEFAULT_EVERY_S = 5.0
SHUTDOWN_HARD_S = 25.0                      # the engine answers Shutdown within 20 s; 25 s + thread joins (12 s)
#                                             + the alerts flush (6 s) < TimeoutStopSec (45 s): never SIGKILLed
RECOVERY_ONLY_MAX_S = 60.0                  # recovery-only run: exit even if the second recon never completes
SIGNALS_STALL_S = 5.0                       # signals thread loop older than this -> signals_degraded()
PREV_BARS_MIN = 20                          # opening_drive reads the previous session's last 20 HIST bars
PREV_BARS_ABORT_AFTER = 5                   # consecutive historical failures (token dead, API down): stop the run
ALERTS_FLUSH_S = 5.0
DEADMAN_KEEP_TICK_S = 5.0                   # weekday without a session: heartbeat + WATCHDOG=1 this often
RUN_MODES = frozenset({"SHADOW", "PAPER", "LIVE"})     # OFF strategies are not run (§10.5)
# the runtime's own last event of a run (the engine's SHUTDOWN phases STOPPING and EXIT come before it)
FINAL_SHUTDOWN_PHASES = frozenset({"EXITED", "IDLE", "CRASHED"})
_TERMINAL_SQL = "(" + ",".join(f"'{s.value}'" for s in sorted(TERMINAL_STRUCT)) + ")"


def _no_feed() -> FeedHealth:
    return FeedHealth(False, None, None, 0, False)


# ---------------------------------------------------------------------------------------------------------------
# Small process pieces: exception window, sd_notify, watchdog, the instance lock, the stop flag
# ---------------------------------------------------------------------------------------------------------------
class ExcWindow:
    """Exceptions in a rolling window (§3.3: more than EXC_LIMIT in EXC_WINDOW_S -> exit 3)."""

    def __init__(self) -> None:
        self._hits: deque[float] = deque()

    def hit_and_count(self, now: float, window_s: float) -> int:
        self._hits.append(now)
        while self._hits and self._hits[0] <= now - window_s:
            self._hits.popleft()
        return len(self._hits)


class Notifier:
    """sd_notify to $NOTIFY_SOCKET (an AF_UNIX datagram socket; '@' = abstract namespace). Without a socket (not under
    systemd, or Windows) notify() does nothing and returns False. A failed send is logged, never raised: systemd
    then sees no READY or WATCHDOG and acts on its own (start timeout, watchdog kill)."""

    def __init__(self, address: str | None = None, *, send: Callable[[bytes], None] | None = None) -> None:
        self.address = address or None
        self._send = send
        self.sent: deque[str] = deque(maxlen=500)      # every state notify() was asked to send, oldest first
        self._failures = 0

    def notify(self, state: str) -> bool:
        self.sent.append(state)
        send = self._send if self._send is not None else (self._send_unix if self.address else None)
        if send is None:
            return False
        try:
            send(state.encode("utf-8"))
        except OSError as exc:
            self._failures += 1
            if self._failures <= 3 or self._failures % 100 == 0:
                log.warning("sd_notify %r failed (%d so far): %s", state.split("\n", 1)[0], self._failures, exc)
            return False
        return True

    def _send_unix(self, payload: bytes) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise OSError("AF_UNIX sockets are not available on this platform")
        addr = self.address
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(payload)


def watchdog_interval(env: Mapping[str, str]) -> float:
    """A third of systemd's WatchdogSec (WATCHDOG_USEC), clamped to 1-10 s; 5 s when it is not set."""
    try:
        usec = int(env.get("WATCHDOG_USEC", ""))
    except ValueError:
        return WATCHDOG_DEFAULT_EVERY_S
    return min(10.0, max(1.0, usec / 3e6)) if usec > 0 else WATCHDOG_DEFAULT_EVERY_S


class Watchdog:
    """WATCHDOG=1 only while the core makes progress: the last Timer finished less than WATCHDOG_FRESH_S ago."""

    def __init__(self, notifier: Notifier, heartbeat: alerts.Heartbeat, *, every_s: float = WATCHDOG_DEFAULT_EVERY_S,
                 fresh_s: float = WATCHDOG_FRESH_S) -> None:
        self.notifier = notifier
        self.heartbeat = heartbeat
        self.every_s = every_s
        self.fresh_s = fresh_s
        self.last_ping: float | None = None
        self.pings = 0

    def tick(self, now_mono: float) -> bool:
        age = self.heartbeat.age(now_mono)
        if age is None or age >= self.fresh_s:
            return False
        if self.last_ping is not None and now_mono - self.last_ping < self.every_s:
            return False
        self.last_ping = now_mono
        self.pings += 1
        return self.notifier.notify("WATCHDOG=1")


def _try_lock(fh: Any) -> bool:
    """flock(LOCK_EX | LOCK_NB) on POSIX; a 1-byte msvcrt lock on Windows (developer runs only)."""
    try:
        import fcntl                                   # POSIX only: imported here so the module imports on Windows
    except ImportError:
        import msvcrt
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


class InstanceLock:
    """The single-instance lock (§3.1): <var>/run/titan2.lock. acquire() is False when another process holds it."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh: Any = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        """Take the lock without waiting. Raises OSError when the lock file cannot be opened at all."""
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="ascii")
        try:
            locked = _try_lock(fh)
        except BaseException:
            fh.close()
            raise
        if not locked:
            fh.close()
            return False
        self._fh = fh
        if os.name == "posix":                         # the holder's pid, for operators (flock is per descriptor)
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name != "posix":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()                                 # closing the descriptor drops the flock


class StopRequest:
    """Set from a signal handler (only an attribute store: nothing that could deadlock); the core loop polls it."""

    def __init__(self) -> None:
        self.reason: str | None = None

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason


def install_signal_handlers(stop: StopRequest) -> None:
    """SIGTERM and SIGINT request a graceful stop (§9.5 step 17). Main thread only."""
    if threading.current_thread() is not threading.main_thread():
        log.warning("signal handlers not installed: not on the main thread")
        return

    def handler(signum: int, _frame: Any) -> None:
        stop.request(signal.Signals(signum).name)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


# ---------------------------------------------------------------------------------------------------------------
# Today's prep products, shared between threads
# ---------------------------------------------------------------------------------------------------------------
class PrepState:
    """What prep produced for `session` (§9.5 step 13): the InstrumentStore, the ban list and the previous-session
    bar coverage. The prep thread writes; any thread reads (immutable values swapped under a lock)."""

    def __init__(self, session: date, settings: settings_mod.Settings) -> None:
        self.session = session
        self.settings = settings
        self._lock = threading.Lock()
        self._inst: Any = None
        self._ban: ins.BanList | None = None
        self._coverage: tuple[int, int] | None = None
        self._universe: dict[str, tuple[Any, Any, frozenset[str]]] = {}    # kind -> (inst, ban, universe)

    def instruments(self) -> ins.InstrumentStore | None:
        with self._lock:
            return self._inst

    def set_instruments(self, inst: ins.InstrumentStore) -> None:
        if inst.session != self.session:
            raise ValueError(f"instrument dump for {inst.session}, not today's session {self.session}")
        with self._lock:
            self._inst = inst

    def ban(self) -> ins.BanList | None:
        with self._lock:
            return self._ban

    def set_ban(self, ban: ins.BanList) -> None:
        with self._lock:
            self._ban = ban

    def ban_symbols(self) -> frozenset[str] | None:
        """EngineContext.ban_list: today's banned NSE symbols, or None (missing: no stock is eligible)."""
        ban = self.ban()
        return ban.symbols if ban is not None and ban.session == self.session else None

    def coverage(self) -> tuple[int, int] | None:
        with self._lock:
            return self._coverage

    def set_coverage(self, have: int, total: int) -> None:
        with self._lock:
            self._coverage = (have, total)

    def contract(self, symbol: str) -> Any:
        inst = self.instruments()
        return inst.by_symbol(symbol) if inst is not None else None

    def symbol_for_token(self, token: int) -> str | None:
        inst = self.instruments()
        return inst.symbol_for_token(token) if inst is not None else None

    def universe(self, kind: str) -> frozenset[str]:
        """A strategy's MarketView universe: eligible stocks today ("STOCKS"), or the index underlyings."""
        with self._lock:
            inst, ban = self._inst, self._ban
            hit = self._universe.get(kind)
            if hit is not None and hit[0] is inst and hit[1] is ban:    # cached until either object is replaced
                return hit[2]
        if inst is None:
            value: frozenset[str] = frozenset()
        elif kind == "INDICES":
            value = frozenset(u.symbol for u in inst.underlyings(ins.INDEX))
        else:
            symbols = ban.symbols if ban is not None and ban.session == self.session else None
            value = inst.universe(symbols, self.settings.STOCK_ALLOWLIST, self.settings.STOCK_DENYLIST)
        with self._lock:
            self._universe[kind] = (inst, ban, value)
        return value


# ---------------------------------------------------------------------------------------------------------------
# The signals thread: ticks -> bars -> strategies (§3.2, §9.5 step 14, §10.1)
# ---------------------------------------------------------------------------------------------------------------
class _View:
    """One strategy's read-only MarketView (§10.1). Only the signals thread calls it."""

    __slots__ = ("_host", "_kind")

    def __init__(self, host: SignalsHost, kind: str) -> None:
        self._host = host
        self._kind = kind

    def ltp(self, underlying: str) -> tuple[float, float] | None:
        q = self._host.quotes.last(underlying)
        if q is None or not isinstance(q.ltp, (int, float)) or not q.ltp > 0:
            return None
        return float(q.ltp), q.age(self._host.hook_mono)

    def last_tick(self, underlying: str) -> UnderlyingTick | None:
        return self._host.last_ticks.get(underlying)

    def bars(self, underlying: str, n: int) -> tuple[Bar, ...]:
        b = self._host.bars
        return () if b is None else b.bars(underlying, n)

    def prev_session_bars(self, underlying: str, n: int) -> tuple[Bar, ...]:
        b = self._host.bars
        return () if b is None else b.prev_session_bars(underlying, n)

    def universe(self) -> frozenset[str]:
        return self._host.universe(self._kind)

    def feed_ok(self) -> bool:
        return self._host.feed_ok()

    def now_mono(self) -> float:
        return self._host.hook_mono


def _received_mono(item: Any) -> float | None:
    """When a signal_q item was received (monotonic); None for a SignalsLost marker (about batches already dropped)
    and for anything unknown: those are handled before a Timer."""
    if isinstance(item, tuple) and item:
        return min(t.recv_mono for t in item)
    if isinstance(item, FeedLink):
        return item.recv_mono
    return None


class SignalsHost:
    """signal_q -> Bars -> strategies (thread "signals").

    Bars are built from the first tick, so the 09:15 bar is whole even when StartSignals comes later. Strategy hooks
    run only after enable() (StartSignals) and only for strategies whose current mode (mode_of, published by the core
    thread) is SHADOW, PAPER or LIVE. Every closed bar (and invalid correction) goes to core_q as BarsClosed for
    titan2.db; a strategy that raises is disabled by its StrategyRunner and alerted here (CRITICAL)."""

    def __init__(self, *, signal_q: SignalQueue, runners: Mapping[str, Any], quotes: QuoteCache, clock: Clock,
                 calendar: SessionCalendar, settings: settings_mod.Settings, post: Callable[[Event], None],
                 alert: Callable[[str, str, str, str | None], Any], prep_state: PrepState,
                 feed_health: Callable[[], FeedHealth], mode_of: Callable[[str], str | None],
                 timer_dt: float = TIMER_DT) -> None:
        self.signal_q = signal_q
        self.runners = dict(runners)
        self.quotes = quotes
        self.clock = clock
        self.calendar = calendar
        self.settings = settings
        self._post = post
        self._alert = alert
        self._state = prep_state
        self._feed_health = feed_health
        self._mode_of = mode_of
        self._timer_dt = timer_dt
        self._views = {name: _View(self, getattr(type(r.strategy), "UNIVERSE", "STOCKS"))
                       for name, r in self.runners.items()}
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()     # (symbol, prev-session bars) from the prep thread
        self._enabled = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._beat: float | None = None
        self._next_timer: float | None = None
        self._held: Any = None                                   # received after a Timer's instant: handled next
        self._day: date | None = None
        self.bars: Bars | None = None
        self.last_ticks: dict[str, UnderlyingTick] = {}
        self.hook_mono = clock.mono()
        self.errors = 0

    # -- any thread ------------------------------------------------------------------------------------------
    def enable(self) -> None:
        """StartSignals (§9.5 step 14): strategies receive hooks from now on."""
        if not self._enabled.is_set():
            log.info("signals: strategies enabled (%s)", ", ".join(sorted(self.runners)) or "none loaded")
        self._enabled.set()

    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    def set_prev_session(self, symbol: str, bars: Iterable[Bar]) -> None:
        self._inbox.put((symbol, tuple(bars)))

    def degraded(self) -> bool:
        """EngineContext.signals_degraded: the thread died or its loop stalled (entries refuse, exits go on)."""
        t = self._thread
        if t is None:
            return False
        beat = self._beat
        return not t.is_alive() or beat is None or self.clock.mono() - beat > SIGNALS_STALL_S

    def start(self) -> threading.Thread:
        if self._thread is not None:
            raise RuntimeError("signals thread already started")
        self._beat = self.clock.mono()
        self._thread = threading.Thread(target=self.run, name="signals", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, join_s: float | None = None) -> None:
        self._stopping.set()
        if self._thread is not None and join_s is not None:
            self._thread.join(join_s)

    # -- the signals thread ----------------------------------------------------------------------------------
    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.step(self._timer_dt)
            except Exception:
                self.errors += 1
                log.exception("signals: step failed (%d so far)", self.errors)
                self._stopping.wait(self._timer_dt)

    def step(self, wait_s: float = 0.0) -> None:
        """One unit of work: a Timer (every timer_dt) or one signal_q item.

        A Timer first takes every queued item received up to its own instant (market/bars.py: "the signals thread
        drains signal_q first and calls close_due(now) before the strategies' on_timer(now)"), so the trade that
        ended the 09:15 bar at 09:19:59.99 is in the bar opening_drive evaluates at 09:20, and never arrives late to
        invalidate a bar a strategy already acted on. The first item received after that instant waits for the
        Timer."""
        now = self.clock.mono()
        self._beat = now
        day = self.clock.today()
        if day != self._day:
            self._new_day(day)
        self._drain_inbox()
        if self._next_timer is None or now >= self._next_timer:
            now_wall = self.clock.now_wall()
            while True:
                item = self._take(0.0)
                if item is None:
                    break
                received = _received_mono(item)
                if received is not None and received > now:
                    self._held = item
                    break
                self._on_item(item)
            self._next_timer = now + self._timer_dt
            self._on_timer(now_wall, now)
            return
        item = self._take(min(wait_s, self._next_timer - now))
        if item is not None:
            self._on_item(item)

    def _take(self, timeout: float) -> Any:
        """The item held back by the last Timer, else the next signal_q item (None after timeout seconds)."""
        if self._held is not None:
            item, self._held = self._held, None
            return item
        try:
            return self.signal_q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def _on_item(self, item: Any) -> None:
        if isinstance(item, tuple):
            for t in item:
                self._on_tick(t)
        elif isinstance(item, FeedLink):
            if self.bars is not None:
                self._closed(self.bars.on_link(item.connected, item.recv_wall))
            if item.connected and item.reconnected:
                self._reset("WS reconnect")
        elif isinstance(item, SignalsLost):
            log.warning("signals: %d tick batch(es) lost between %s and %s", item.batches, item.first_wall,
                        item.last_wall)
            if self.bars is not None:
                self._closed(self.bars.on_gap(item.first_wall, item.last_wall))
            self._reset("ticks lost")
        else:
            log.error("signals: unknown signal_q item %r dropped", item)

    def _new_day(self, day: date) -> None:
        self._day = day
        self.bars = Bars.for_session(self.calendar, day)
        self.last_ticks = {}
        log.info("signals: session %s, bars %s", day, "on" if self.bars is not None else "off (no session)")

    def _drain_inbox(self) -> None:
        while True:
            try:
                symbol, bars = self._inbox.get_nowait()
            except queue.Empty:
                return
            if self.bars is None:
                continue
            try:
                self.bars.set_prev_session(symbol, bars)
            except ValueError as exc:
                log.error("signals: previous-session bars for %s refused: %s", symbol, exc)

    def _on_tick(self, t: UnderlyingTick) -> None:
        prev = self.last_ticks.get(t.symbol)
        if prev is None or t.recv_mono >= prev.recv_mono:
            self.last_ticks[t.symbol] = t
        if self.bars is not None:
            self._closed(self.bars.on_tick(t))
        self.hook_mono = t.recv_mono
        self._hooks("on_tick", t)

    def _on_timer(self, now_wall: datetime, now_mono: float) -> None:
        if self.bars is not None:
            self._closed(self.bars.close_due(now_wall))
        self.hook_mono = now_mono
        self._hooks("on_timer", now_wall)

    def _closed(self, bars: list[Bar]) -> None:
        if not bars:
            return
        self._post(BarsClosed(tuple(bars)))
        self.hook_mono = self.clock.mono()
        for b in bars:
            self._hooks("on_bar_close", b)

    def _hooks(self, hook: str, arg: Any) -> None:
        if not self._enabled.is_set():
            return
        for name, runner in self.runners.items():
            if self._mode_of(name) not in RUN_MODES:
                continue
            self._result(name, getattr(runner, hook)(arg, self._views[name]))

    def _reset(self, why: str) -> None:
        log.warning("signals: %s: every strategy resets its baselines", why)
        for name, runner in self.runners.items():
            self._result(name, runner.on_feed_reset())

    def _result(self, name: str, res: Any) -> None:
        for intent in res.intents:
            self._post(IntentReceived(intent))
        for intent_id, why in res.dropped:
            log.warning("signals: %s intent %s dropped (%s)", name, intent_id, why)
        if res.error:
            log.critical("signals: %s disabled for the session: %s", name, res.error)
            self._alert("CRITICAL", "STRATEGY_DISABLED", f"{res.error}; {name} is disabled for the rest of the "
                                                         "session", f"strategy_disabled:{name}")

    # -- MarketView sources ----------------------------------------------------------------------------------
    def universe(self, kind: str) -> frozenset[str]:
        return self._state.universe(kind)

    def feed_ok(self) -> bool:
        h = self._feed_health()
        last = h.last_underlying_tick_mono
        return bool(h.connected and not h.degraded and last is not None
                    and self.clock.mono() - last <= self.settings.FEED_MAX_TICK_GAP_S)


# ---------------------------------------------------------------------------------------------------------------
# Prep (§9.5 step 13) and the in-bot self-check
# ---------------------------------------------------------------------------------------------------------------
PrepHandler = Callable[[], "tuple[bool, str]"]


class PrepWorker:
    """Prep commands, one at a time on thread "prep", each answered with a PrepResult (never with silence)."""

    def __init__(self, handlers: Mapping[str, PrepHandler], post: Callable[[Event], None], *,
                 name: str = "prep") -> None:
        self.handlers = dict(handlers)
        self._post = post
        self._name = name
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def submit(self, cmd: Prep) -> None:
        if not isinstance(cmd, Prep):
            raise TypeError(f"the prep worker runs Prep commands, not {type(cmd).__name__}")
        self._q.put(cmd)

    def execute(self, cmd: Prep) -> PrepResult:
        handler = self.handlers.get(cmd.kind)
        if handler is None:
            return PrepResult(cmd.kind, False, f"{LOCAL}: no prep handler for {cmd.kind}")
        try:
            ok, detail = handler()
        except Exception as exc:
            log.error("prep %s failed: %r", cmd.kind, exc, exc_info=True)
            return PrepResult(cmd.kind, False, lanes.read_error_text(exc))
        return PrepResult(cmd.kind, ok is True, str(detail)[:500])

    def drain(self) -> list[PrepResult]:
        """Inline (tests): run every queued command now, post and return the results."""
        out = []
        while True:
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                return out
            if cmd is None:
                continue
            res = self.execute(cmd)
            self._post(res)
            out.append(res)

    def run(self) -> None:
        while True:
            cmd = self._q.get()
            if cmd is None:
                return
            self._post(self.execute(cmd))

    def start(self) -> threading.Thread:
        if self._thread is not None:
            raise RuntimeError("prep worker already started")
        self._thread = threading.Thread(target=self.run, name=self._name, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, join_s: float | None = None) -> None:
        self._q.put(None)
        if self._thread is not None and join_s is not None:
            self._thread.join(join_s)


class Prepper:
    """The Prep handlers: INSTRUMENTS, BAN_LIST, PREV_BARS and the §9.7 TOKEN re-check."""

    def __init__(self, *, state: PrepState, data: Any, var_dir: Path, calendar: SessionCalendar,
                 settings: settings_mod.Settings, post: Callable[[Event], None],
                 on_instruments: Callable[[Any], None] | None = None,
                 on_prev_bars: Callable[[str, tuple[Bar, ...]], None] | None = None,
                 ban_fetch: Callable[[], str] | None = None, token_check: PrepHandler | None = None) -> None:
        self.state = state
        self.data = data
        self.var = Path(var_dir)
        self.calendar = calendar
        self.settings = settings
        self._post = post
        self._on_instruments = on_instruments
        self._on_prev_bars = on_prev_bars
        self._ban_fetch = ban_fetch
        self._token_check = token_check

    @property
    def session(self) -> date:
        return self.state.session

    def handlers(self) -> dict[str, PrepHandler]:
        out: dict[str, PrepHandler] = {"INSTRUMENTS": self.instruments, "BAN_LIST": self.ban_list,
                                       "PREV_BARS": self.prev_bars}
        if self._token_check is not None:
            out["TOKEN"] = self._token_check
        return out

    def instruments(self) -> tuple[bool, str]:
        """Today's dump (the cache when present and sane, else Kite), then the feed subscribes the underlyings."""
        s = self.settings
        cache = self.var / "cache"
        inst = ins.prep_instruments(self.data, cache, self.session, self.calendar,
                                    min_td_index=s.MIN_TD_TO_EXPIRY_INDEX, min_td_stock=s.MIN_TD_TO_EXPIRY_STOCK)
        self.state.set_instruments(inst)
        try:
            ins.prune_cache(cache, self.session)
        except OSError as exc:
            log.warning("prep: pruning old cached dumps in %s failed: %s", cache, exc)
        if self._on_instruments is not None:
            self._on_instruments(inst)
        return True, f"{inst.summary['contracts']} contracts, {inst.summary['stocks']} stocks"

    def ban_list(self) -> tuple[bool, str]:
        ban = ins.load_ban_list(self.session, cache_dir=self.var / "cache", config_dir=self.var / "config",
                                fetch=self._ban_fetch)
        self.state.set_ban(ban)
        return ban.ok, f"{ban.source}: {ban.detail}"

    def prev_bars(self) -> tuple[bool, str]:
        """The previous session's 5-min HIST bars for every stock in the universe: from titan2.db when already
        stored (a restart), else from Kite (persisted through BarsClosed). The signals thread gets them all."""
        inst = self.state.instruments()
        if inst is None:
            return False, "today's instruments are not loaded"
        window = prev_session_window(self.calendar, self.session)
        if window is None:
            return False, f"no trading session before {self.session} in the calendar"
        s = self.settings
        symbols = sorted(inst.universe((), s.STOCK_ALLOWLIST, s.STOCK_DENYLIST))
        have = fetched = failed = in_a_row = 0
        last_error = ""
        db = self._open_db()
        try:
            for sym in symbols:
                bars = self._stored(db, sym, window)
                if not _complete(bars, window):
                    u = inst.underlying(sym)
                    try:
                        bars = [b for b in self.data.historical(sym, u.token, window[0], window[1], "5minute")
                                if window[0] <= b.start < window[1]]
                    except Exception as exc:
                        failed += 1
                        in_a_row += 1
                        last_error = f"{sym}: {lanes.read_error_text(exc)}"
                        log.warning("prep PREV_BARS %s", last_error)
                        if in_a_row >= PREV_BARS_ABORT_AFTER:
                            break
                        continue
                    in_a_row = 0
                    fetched += 1
                    if bars:
                        self._post(BarsClosed(tuple(bars)))
                if self._on_prev_bars is not None:
                    self._on_prev_bars(sym, tuple(bars))
                if sum(1 for b in bars if b.valid) >= PREV_BARS_MIN:
                    have += 1
        finally:
            if db is not None:
                db.close()
        self.state.set_coverage(have, len(symbols))
        detail = (f"{have}/{len(symbols)} stocks have >= {PREV_BARS_MIN} bars of {window[0]:%Y-%m-%d}; "
                  f"{fetched} fetched, {failed} failed")
        if last_error:
            detail += f"; last error {last_error}"
        return failed == 0, detail

    def _open_db(self) -> store_mod.Store | None:
        path = self.var / "db" / store_mod.DB_FILENAME
        if not path.is_file():
            return None
        try:
            return store_mod.open_readonly(path)
        except Exception as exc:                       # StoreError, sqlite3.Error: fall back to fetching everything
            log.warning("prep PREV_BARS: titan2.db not readable (%s); fetching every symbol", exc)
            return None

    @staticmethod
    def _stored(db: store_mod.Store | None, symbol: str, window: tuple[datetime, datetime]) -> list[Bar]:
        if db is None:
            return []
        bars = db.bars(symbol, source="HIST", start_from=window[0], start_to=window[1])
        return [b for b in bars if window[0] <= b.start < window[1]]


def _complete(bars: list[Bar], window: tuple[datetime, datetime]) -> bool:
    """Stored bars are reused only when they hold the session's close (the last 20 bars are what opening_drive
    reads): >= PREV_BARS_MIN valid bars and one starting within the last two intervals."""
    if sum(1 for b in bars if b.valid) < PREV_BARS_MIN:
        return False
    return max(b.start for b in bars) >= window[1] - timedelta(minutes=10)


class SelfcheckRunner:
    """RunSelfcheck: selfcheck.run_phase on a thread of its own, answered with SelfcheckResult. phases maps a
    requested phase to the one run (the laptop profile runs "laptop" for the engine's "prestart")."""

    def __init__(self, make_context: Callable[[str], Any], post: Callable[[Event], None],
                 phases: Mapping[str, str] | None = None) -> None:
        self._make_context = make_context
        self._post = post
        self._phases = dict(phases or {})
        self._thread: threading.Thread | None = None

    def run(self, phase: str) -> None:
        phase = self._phases.get(phase, phase)
        if self._thread is not None and self._thread.is_alive():
            log.warning("self-check %s requested while one is running; skipped", phase)
            return
        self._thread = threading.Thread(target=self.work, args=(phase,), name="selfcheck", daemon=True)
        self._thread.start()

    def work(self, phase: str) -> SelfcheckResult:
        try:
            report = selfcheck.run_phase(phase, self._make_context(phase))
            res = SelfcheckResult(phase, report.ok, report, None)
        except Exception as exc:
            log.error("self-check %s did not run: %r", phase, exc, exc_info=True)
            res = SelfcheckResult(phase, False, None, repr(exc)[:300])
        self._post(res)
        return res


# ---------------------------------------------------------------------------------------------------------------
# The runtime (rt in §3.3)
# ---------------------------------------------------------------------------------------------------------------
_BROKER_OR_DATA = (lanes.ORDER_COMMANDS + lanes.ACCOUNT_READ_COMMANDS + lanes.MARKET_READ_COMMANDS
                  + lanes.PAPER_READ_COMMANDS)


class Runtime:
    """What the core loop needs: the store, the engine context, core_q, the clock, the heartbeat and the exception
    window; dispatch() routes every command after its commit; after_event() does the after-commit duties (READY,
    the feed and signals start, the watchdog, the snapshots other threads read). Components may be None (tests,
    the recovery-only run): a command with no component to run it is answered conservatively, never dropped."""

    def __init__(self, *, store: Any, ctx: EngineContext | None, clock: Clock, core_q: queue.Queue | None = None,
                 heartbeat: alerts.Heartbeat | None = None, notifier: Notifier | None = None, alerter: Any = None,
                 order_lane: Any = None, reads_lane: Any = None, paper_sim: Any = None, prep: Any = None,
                 selfcheck_runner: Any = None, feed: Any = None, signals: Any = None,
                 prep_state: PrepState | None = None, engine_cls: Any = None, recovery_only: bool = False,
                 stop: StopRequest | None = None, watchdog_every_s: float = WATCHDOG_DEFAULT_EVERY_S) -> None:
        self.store = store
        self.ctx = ctx
        self.clock = clock
        self.core_q: queue.Queue = core_q if core_q is not None else queue.Queue()
        self.heartbeat = heartbeat if heartbeat is not None else alerts.Heartbeat()
        self.notifier = notifier if notifier is not None else Notifier(None)
        self.alerter = alerter
        self.order_lane = order_lane
        self.reads_lane = reads_lane
        self.paper_sim = paper_sim
        self.prep = prep
        self.selfcheck = selfcheck_runner
        self.feed = feed
        self.signals = signals
        self.prep_state = prep_state
        self.engine_cls = engine_cls if engine_cls is not None else Engine
        self.recovery_only = recovery_only
        self.stop = stop if stop is not None else StopRequest()
        self.exc_window = ExcWindow()
        self.iv_window = ExcWindow()                  # invariant violations: a reload storm alerts, never exits
        self.watchdog = Watchdog(self.notifier, self.heartbeat, every_s=watchdog_every_s)
        self.boot_halts: list[tuple[str, str, str, str]] = []     # (kind, scope, clears, reason), raised at boot
        self.boot_alerts: list[Alert] = []
        self.stopping = False
        self.exit_code: int | None = None
        self.exit_reason = ""
        self.ready = False
        self.ready_mono: float | None = None
        self.feed_started = False
        self.dispatch_errors = 0
        self.modes: dict[str, str] = {}               # strategy -> mode, published on every Timer (signals reads)
        self.held_symbols: dict[int, str] = {}        # token -> symbol of legs/tails, for the feed resolver
        self.keep_reactor = False                     # laptop: the Twisted reactor outlives this run (shutdown())
        self._stop_posted: float | None = None
        self._stopping_sent = False

    # -- the loop's hooks -------------------------------------------------------------------------------------
    def load_engine(self) -> Any:
        return self.engine_cls.load(self.store, self.ctx)

    def boot(self, engine: Any) -> None:
        """Boot halts (HALT(TOKEN) when profile() failed) in their own transaction, then the boot alerts."""
        cmds: list[Command] = []
        if self.boot_halts:
            with self.store.tx():
                for kind, scope, clears, reason in self.boot_halts:
                    cmds += engine.raise_halt(kind, scope, clears, reason)
        for c in list(self.boot_alerts) + cmds:
            self.dispatch(c)

    def next_event(self, timeout: float) -> Event:
        """The next core_q event; raises queue.Empty after timeout seconds."""
        return self.core_q.get(timeout=timeout)

    def poll_stop(self) -> None:
        """A SIGTERM / SIGINT flag becomes one Shutdown event (§9.5 step 17)."""
        reason = self.stop.reason
        if reason is not None and self._stop_posted is None:
            self._stop_posted = self.clock.mono()
            log.warning("%s received: stopping (entries stop, entry orders cancel, exits finish; never flattens)",
                        reason)
            self.core_q.put(Shutdown(reason))

    def after_event(self, engine: Any, ev: Event) -> None:
        now = self.clock.mono()
        if not self.ready and engine.recovery_done:
            self._on_ready(engine, now)
        if isinstance(ev, Timer):
            self._snapshot(engine)
            if self.recovery_only and self.ready and not self.stopping and self._recon_done(engine, now):
                self.exit(EXIT_OK, "recovery and recon done (recovery-only run)")
            if self._stop_posted is not None and not self.stopping and now - self._stop_posted > SHUTDOWN_HARD_S:
                log.critical("the engine did not finish the shutdown within %.0f s; exiting", SHUTDOWN_HARD_S)
                self.exit(EXIT_OK, "shutdown deadline")
        self.watchdog.tick(now)

    def exit(self, code: int, reason: str = "") -> None:
        """End the core loop with this exit code (the first one requested wins)."""
        if self.exit_code is None:
            self.exit_code, self.exit_reason = int(code), reason
            level = logging.CRITICAL if code in (EXIT_EXCEPTION_STORM, EXIT_FATAL) else logging.WARNING
            log.log(level if code else logging.INFO, "exit %d requested: %s", code, reason or "-")
        self.stopping = True
        if not self._stopping_sent:
            self._stopping_sent = True
            self.notifier.notify("STOPPING=1")

    # -- dispatch (only after the commit) ----------------------------------------------------------------------
    def dispatch(self, c: Command) -> None:
        try:
            self._route(c)
        except Exception as exc:
            self.dispatch_errors += 1
            log.critical("dispatch of %r failed", c, exc_info=True)
            self._alert("CRITICAL", "DISPATCH_FAILED", f"{type(c).__name__} could not be dispatched: {exc!r}"[:500],
                        f"dispatch:{type(c).__name__}")
            for ev in self._refusal(c, f"{LOCAL}: dispatch failed: {exc!r}"[:300]):
                self.core_q.put(ev)

    def _route(self, c: Command) -> None:
        if isinstance(c, Alert):
            if self.alerter is not None:
                self.alerter.dispatch(c)
            else:
                log.warning("alert %s %s [%s]: %s", c.severity, c.code, c.dedup_key or c.code, c.text)
            return
        if isinstance(c, Exit):
            self.exit(c.code, "engine Exit")
            return
        if isinstance(c, (Subscribe, Unsubscribe)):
            if self.feed is None:
                log.info("no feed in this run: %r ignored", c)
                return
            self.feed.dispatch(c)
            return
        if isinstance(c, StartSignals):
            if self.signals is None:
                log.warning("StartSignals: no signals thread in this run")
                return
            self.signals.enable()
            return
        if isinstance(c, RunSelfcheck):
            if self.selfcheck is None:
                raise RuntimeError("no self-check runner in this run")
            self.selfcheck.run(c.phase)
            return
        if isinstance(c, Prep):
            target, where = self.prep, "prep worker"
        else:
            where = lanes.lane_for(c)
            if where is None:
                raise TypeError(f"no route for {type(c).__name__}")
            target = {"orders": self.order_lane, "reads": self.reads_lane, "paper": self.paper_sim}[where]
        if target is None:
            raise RuntimeError(f"no {where} in this run")
        target.submit(c)

    def _refusal(self, c: Command, text: str) -> list[Event]:
        """The conservative answer to a command that could not be dispatched (it certainly was not sent)."""
        if isinstance(c, _BROKER_OR_DATA):
            return lanes.failure_events(c, text, self.clock.mono(), outcome=REJECTED)
        if isinstance(c, RunSelfcheck):
            return [SelfcheckResult(c.phase, False, None, text)]
        return []

    def _alert(self, severity: str, code: str, text: str, key: str | None = None) -> None:
        """The runtime's own alerts (core thread). A failing alerter is logged, never allowed into the core loop."""
        try:
            self._route(Alert(severity, code, text, key))
        except Exception:
            log.exception("alert %s %s could not be queued: %s", severity, code, text)

    def alert_reload_storm(self, iv: InvariantViolation) -> None:
        """More than EXC_LIMIT invariant violations in EXC_WINDOW_S: the rollback-and-reload is not converging (e.g. a
        broker report that keeps contradicting titan2.db on every poll). One CRITICAL, repeating until acked."""
        self._alert("CRITICAL", "INVARIANT", f"reload storm: more than {EXC_LIMIT} invariant violations in "
                    f"{EXC_WINDOW_S:.0f} s, the last {str(iv)[:300]}; the engine keeps reloading and exits continue. "
                    "Find the contradicting report in the log and compare it with the Kite order history",
                    "invariant:reload_storm")

    def alert(self, severity: str, code: str, text: str, key: str | None = None) -> None:
        """Any thread: straight to the alerts queue (never blocks)."""
        if self.alerter is not None:
            self.alerter.submit(severity, code, text, key)
        else:
            log.warning("alert %s %s [%s]: %s", severity, code, key or code, text)

    # -- READY and what follows it (§9.5 steps 11-12) -------------------------------------------------------------
    def _on_ready(self, engine: Any, now: float) -> None:
        self.ready, self.ready_mono = True, now
        phase = getattr(engine, "phase", "?")
        self.notifier.notify(f"READY=1\nSTATUS=titan2 {__version__}: recovered, phase {phase}")
        if self.alerter is not None:
            self.alerter.ping_ready()
        log.info("READY (phase %s)%s", phase, " - recovery-only run" if self.recovery_only else "")
        if self.recovery_only:
            return
        self.start_feed(engine)
        if self.signals is not None:
            try:
                self.signals.start()
            except Exception as exc:
                log.critical("the signals thread did not start", exc_info=True)
                self._alert("CRITICAL", "SIGNALS_START_FAILED", f"the signals thread did not start: {exc!r}; no "
                                                                "strategy runs (exits are unaffected)", "signals:start")

    def start_feed(self, engine: Any) -> None:
        """Underlyings in quote mode, held or pending options in full mode, then connect (§9.5 step 12)."""
        if self.feed is None or self.feed_started:
            return
        try:
            held = sorted(engine.state.held_tokens())
            if held:
                self.feed.request(held, "full")
            self.subscribe_underlyings()
            self.feed.start()
            self.feed_started = True
        except Exception as exc:
            log.critical("the WS feed did not start", exc_info=True)
            self._alert("CRITICAL", "FEED_START_FAILED", f"the WS feed did not start: {exc!r}; exits use REST "
                                                         "quotes and entries are refused", "feed:start")

    def subscribe_underlyings(self, inst: Any = None) -> None:
        """Any thread (the prep thread after INSTRUMENTS): every underlying of today's dump in quote mode."""
        if self.feed is None:
            return
        if inst is None:
            inst = self.prep_state.instruments() if self.prep_state is not None else None
        if inst is None:
            return
        tokens = [u.token for u in inst.underlyings()]
        if tokens:
            self.feed.request(tokens, "quote")

    def symbol_of(self, token: int) -> str | None:
        """The feed's token resolver: today's dump, else a held leg's own instrument (a leg from before the dump)."""
        sym = self.prep_state.symbol_for_token(token) if self.prep_state is not None else None
        return sym if sym is not None else self.held_symbols.get(token)

    def mode_of(self, strategy: str) -> str | None:
        return self.modes.get(strategy)

    def _snapshot(self, engine: Any) -> None:
        """Facts other threads read, published once per Timer (whole-dict swaps)."""
        st = engine.state
        self.modes = {name: str(row["mode"]) for name, row in st.modes.items()}
        held = {leg.inst.token: leg.inst.symbol for leg in st.legs.values() if not leg.is_terminal}
        held.update({t.token: t.symbol for t in st.tails.values()})
        self.held_symbols = held

    def _recon_done(self, engine: Any, now: float) -> bool:
        """Recovery-only run: both post-recovery reconciles (>= 3 s apart, §9.6) ran on every account. After
        BROKER_SQUAREOFF_AT on a trading day, also no square-off still waiting to be booked (the engine's late recon,
        bounded by its own end: engine.LATE_RECON_UNTIL, or a minute after READY when that is later), and no EOD step
        from TRUTH on still due (the engine's own DONE ends the run then; an exit before it would lose the truth
        check's answers and the day's files, each wait bounded by the engine: engine.EOD_WAITS_FOR)."""
        if getattr(engine, "late_recon_holding", False) or getattr(engine, "eod_finishing", False):
            return False
        if now - self.ready_mono >= RECOVERY_ONLY_MAX_S:
            log.warning("recovery-only run: recon not complete after %.0f s; exiting anyway", RECOVERY_ONLY_MAX_S)
            return True
        for acct in ACCOUNTS:
            bv = engine.state.broker[acct]
            if (bv.extra_round_at is not None or bv.last_recon_mono is None
                    or bv.last_recon_mono < self.ready_mono + RECON_CONFIRM_GAP_S):
                return False
        return True

    # -- threads -----------------------------------------------------------------------------------------------
    def start_threads(self) -> None:
        """Step 9: the lanes, paper-sim and the prep worker (the feed and signals start at READY)."""
        for comp in (self.order_lane, self.reads_lane, self.paper_sim, self.prep):
            if comp is not None:
                comp.start()

    def shutdown(self) -> None:
        """Stop every thread this runtime started (each step logged on failure; nothing here places an order)."""
        steps: list[tuple[str, Callable[[], Any]]] = []
        if self.feed is not None and self.feed_started:
            # a stopped reactor cannot run again: the laptop runner's next in-process run reuses this one
            steps.append(("feed", (lambda: self.feed.stop(stop_reactor=False)) if self.keep_reactor
                          else self.feed.stop))
        if self.signals is not None:
            steps.append(("signals", lambda: self.signals.stop(2.0)))
        if self.prep is not None:
            steps.append(("prep", lambda: self.prep.stop(1.0)))
        if self.order_lane is not None:
            steps.append(("order lane", lambda: self.order_lane.stop(5.0)))
        if self.reads_lane is not None:
            steps.append(("reads lane", lambda: self.reads_lane.stop(2.0)))
        if self.paper_sim is not None:
            steps.append(("paper-sim", lambda: self.paper_sim.stop(2.0)))
            steps.append(("paper exchange", self.paper_sim.exchange.close))
        for name, fn in steps:
            try:
                fn()
            except Exception:
                log.exception("stopping the %s failed", name)


# ---------------------------------------------------------------------------------------------------------------
# The core loop (§3.3)
# ---------------------------------------------------------------------------------------------------------------
def core_loop(rt: Runtime) -> None:
    """Write-ahead, commit before dispatch. Runs on the core thread until rt.stopping (an Exit command, the exception
    storm, the recovery-only end or the shutdown deadline).

    Timers run even under load, but never starve core_q: when a Timer overran its 250 ms slot (a slow fsync, the
    15:38 G1 run, many legs), the events already queued get up to one more slot (at least one event) before the next
    Timer, so PlaceResults, order snapshots and positions reads are never held back by back-to-back Timers."""
    engine = rt.load_engine()
    rt.boot(engine)
    next_timer = rt.clock.mono()
    drain_until: float | None = None              # a Timer overran: queued events go first until then
    while not rt.stopping:
        rt.poll_stop()
        now = rt.clock.mono()
        if now >= next_timer and (drain_until is None or now >= drain_until):
            drain_until = None
            ev, next_timer = Timer(rt.clock.now_wall(), now), now + TIMER_DT
        else:
            try:
                ev = rt.next_event(max(0.0, next_timer - now))
            except queue.Empty:
                drain_until = None
                continue
        try:
            with rt.store.tx():                           # BEGIN IMMEDIATE ... COMMIT (synchronous=FULL)
                cmds = engine.handle(ev)                  # state + Store DAO writes inside the transaction
                engine.check_invariants()                 # InvariantViolation -> rollback
        except InvariantViolation as iv:
            log.error("invariant %s on %s: rolled back; reloading the engine", iv, type(ev).__name__)
            engine = rt.load_engine()
            with rt.store.tx():
                cmds = engine.on_invariant_violation(iv)
            if rt.iv_window.hit_and_count(now, EXC_WINDOW_S) > EXC_LIMIT:
                # Not an exit: a restart re-reads the same broker report and storms again, and inside this process
                # the reloads keep every other leg's exit timing (store.load_state), so exits go on.
                rt.alert_reload_storm(iv)
        except Exception as exc:
            log.critical("core: %s raised on %s: rolled back; reloading the engine", type(exc).__name__,
                         type(ev).__name__, exc_info=True)
            engine = rt.load_engine()
            with rt.store.tx():
                cmds = engine.raise_halt("EXCEPTION", "GLOBAL", "ACK", repr(exc)[:500])
            if rt.exc_window.hit_and_count(now, EXC_WINDOW_S) > EXC_LIMIT:
                rt.exit(EXIT_EXCEPTION_STORM, f"more than {EXC_LIMIT} exceptions in {EXC_WINDOW_S:.0f} s")
        for c in cmds:
            rt.dispatch(c)                                # only after commit: order rows exist before any HTTP
        if isinstance(ev, Timer):
            done = rt.clock.mono()
            rt.heartbeat.core_progress(done)
            if done >= next_timer:                        # it overran its slot
                drain_until = done + TIMER_DT
        rt.after_event(engine, ev)


# ---------------------------------------------------------------------------------------------------------------
# Startup (§9.5)
# ---------------------------------------------------------------------------------------------------------------
_LOG_MARK = "_titan2_handler"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"


def setup_logging(log_dir: str | Path | None, level: int = logging.INFO, *, filename: str = "bot.log",
                  console: bool = True) -> Path | None:
    """stdout (journald; console=False leaves it out) plus <log_dir>/<filename>: a WatchedFileHandler for logrotate
    on POSIX, a plain FileHandler on Windows (an open file there cannot be renamed under it). An unwritable log dir
    leaves stdout only (logged). Returns the file's path or None. Idempotent: earlier titan2 handlers are replaced."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _LOG_MARK, False):
            root.removeHandler(h)
            h.close()
    fmt = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)] if console else []
    path = problem = None
    if log_dir is not None:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            path = Path(log_dir) / filename
            file_handler = logging.handlers.WatchedFileHandler if os.name == "posix" else logging.FileHandler
            handlers.append(file_handler(path, encoding="utf-8"))
        except OSError as exc:
            path, problem = None, exc
    for h in handlers:
        h.setFormatter(fmt)
        setattr(h, _LOG_MARK, True)
        root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if problem is not None:
        log.warning("cannot write %s/bot.log (%s): logging to stdout only", log_dir, problem)
    return path


def late_cutoff(calendar: SessionCalendar, day: date, settings: settings_mod.Settings) -> datetime | None:
    """The time after which a start does nothing but recovery (§9.5 step 6, "after 15:45"): the later of the
    session close and the EOD timeline's last step, plus 5 minutes. None outside a session."""
    bounds = calendar.session_bounds(day)
    if bounds is None:
        return None
    done = ist_datetime(day, settings.BROKER_SQUAREOFF_AT) + timedelta(
        seconds=max(off for _, off in EOD_AFTER_SQUAREOFF_S))
    return max(bounds[1], done) + timedelta(seconds=LATE_START_GRACE_S)


def nonterminal_structures(store: Any) -> int:
    return int(store.scalar(f"SELECT COUNT(*) FROM structures WHERE state NOT IN {_TERMINAL_SQL}"))


def _kite_ticker(api_key: str, access_token: str | None) -> Any:
    from kiteconnect import KiteTicker              # lazy: the kiteconnect package imports twisted

    return KiteTicker(api_key, access_token or "")


class BotProcess:
    """One run of `python -m titan2 bot` (§9.5), or of the laptop runner's session (laptop=True; None: the profile in
    env decides). boot_halts are (kind, scope, clears, reason) raised at boot (the laptop self-check's). Every
    outside dependency is injectable for tests."""

    def __init__(self, *, safe: bool = False, env: Mapping[str, str] | None = None, clock: Clock | None = None,
                 calendar: SessionCalendar | None = None, notifier: Notifier | None = None,
                 kite_factory: Callable[..., Any] | None = None, ticker_factory: Callable[..., Any] | None = None,
                 reactor: Any = None, ban_fetch: Callable[[], str] | None = None, cwd: str | Path | None = None,
                 check_kite: Callable[..., None] | None = None, install_signals: bool = True,
                 on_runtime: Callable[[Runtime], None] | None = None,
                 sleep: Callable[[float], Any] | None = None, laptop: bool | None = None,
                 boot_halts: Iterable[tuple[str, str, Any, str]] = ()) -> None:
        self.env = dict(os.environ if env is None else env)
        self.laptop = laptop_mod.is_laptop(self.env) if laptop is None else bool(laptop)
        if self.laptop:
            self.env[laptop_mod.PROFILE_ENV] = laptop_mod.PROFILE      # what the laptop self-check rows read
        self.var = laptop_mod.default_var_dir(self.env, DEFAULT_VAR_DIR)
        self.clock = clock if clock is not None else RealClock()
        self.start_mono = self.clock.mono()
        self._calendar_in = calendar
        self.notifier = notifier if notifier is not None else Notifier(self.env.get("NOTIFY_SOCKET"))
        self.safe = bool(safe) or self.env.get("TITAN2_SAFE", "").strip() == "1"
        self.allow_live_env = self.env.get("ALLOW_LIVE", "").strip() == "1"
        self.allow_live = self.allow_live_env and not self.laptop       # the laptop is PAPER only
        self.boot_halts = list(boot_halts)
        self.kite_factory = kite_factory if kite_factory is not None else token_mod.kite_client
        self.ticker_factory = ticker_factory if ticker_factory is not None else _kite_ticker
        self.reactor = reactor
        self.ban_fetch = ban_fetch
        self.cwd = Path(cwd) if cwd is not None else Path.cwd()
        self.check_kite = check_kite if check_kite is not None else assert_kite_unpatched
        self.install_signals = install_signals
        self.on_runtime = on_runtime
        self.sleep = sleep if sleep is not None else time.sleep
        self.stop = StopRequest()
        self.store: Any = None
        self.alerter: Any = None
        self.rt: Runtime | None = None
        self.unsafe_cwd: str | None = None             # set when a shadowing module forced SAFE mode at start

    # -- steps 2-5 ---------------------------------------------------------------------------------------------
    def run(self) -> int:
        lock = InstanceLock(self.var / "run" / LOCK_NAME)
        try:
            if not lock.acquire():
                log.critical("another titan2-bot holds %s: exit %d", lock.path, EXIT_LOCKED)
                return EXIT_LOCKED
        except OSError as exc:
            return self._fatal(f"cannot open the instance lock {lock.path}: {exc}")
        try:
            return self._run_locked()
        finally:
            lock.release()

    def _open_structures(self) -> int | None:
        """Non-terminal structures in titan2.db, read without creating or migrating anything: 0 when there is no
        database yet, None when it cannot be read (then they may be open)."""
        path = self.var / "db" / store_mod.DB_FILENAME
        if not path.is_file():
            return 0
        try:
            con = store_mod.connect_readonly(path)
            try:
                return int(con.execute(f"SELECT COUNT(*) FROM structures WHERE state NOT IN {_TERMINAL_SQL}")
                           .fetchone()[0])
            finally:
                con.close()
        except Exception as exc:
            log.error("cannot count the open structures in %s: %r", path, exc)
            return None

    def _fatal(self, text: str) -> int:
        """Exit 78 (systemd does not restart) with a CRITICAL alert sent directly (no alerts thread yet)."""
        log.critical("FATAL: %s (exit %d)", text, EXIT_FATAL)
        try:
            alerts.critical_now(f"titan2-bot will not start: {text}", code="BOT_FATAL", env=self.env)
        except Exception:
            log.exception("the fatal-start alert could not be sent")
        self.notifier.notify(f"STATUS=fatal: {text[:200]}")
        return EXIT_FATAL

    def _run_locked(self) -> int:
        store_mod.set_core_thread()                         # this thread is the core thread (I1)
        if self.install_signals:
            install_signal_handlers(self.stop)
        try:
            if sys.version_info < (3, 10):
                raise RuntimeError(f"Python {platform.python_version()} is older than 3.10")
            self.check_kite()
        except Exception as exc:                            # old Python, a patched or missing Kite
            return self._fatal(f"unsafe start: {exc}")
        try:
            stray = selfcheck.cwd_problems(self.cwd)
        except Exception as exc:                            # cannot tell: treated as unclean (fails closed)
            stray = [f"the check failed: {exc!r}"]
        if stray:                                           # a module there shadows one the bot imports
            shown = ", ".join(stray[:10]) + (f" (+{len(stray) - 10} more)" if len(stray) > 10 else "")
            what = f"working directory {self.cwd} is not clean: {shown}"
            held = self._open_structures()
            if held == 0:
                return self._fatal(f"unsafe start: {what}")
            # exit 78 is never restarted: with positions possibly open, run exits only and alert instead
            self.safe = True
            self.unsafe_cwd = (f"{what}; {held if held is not None else 'possibly some'} structure(s) open, so "
                               "titan2 runs in SAFE mode (exits only, no entries). Remove the file(s), then restart")
            log.critical("%s", self.unsafe_cwd)
        loaded = settings_mod.load_once(self.var / "config" / "settings.json")
        (log.info if loaded.valid else log.critical)("%s", loaded.summary())
        try:
            store = store_mod.Store(self.var / "db" / store_mod.DB_FILENAME, backup_dir=self.var / "backups")
        except Exception as exc:
            return self._fatal(f"titan2.db cannot be opened or migrated: {exc}")
        try:
            problems = store.integrity_check()
            if problems:
                return self._fatal(f"titan2.db integrity_check: {'; '.join(problems[:3])}")
            if store.migrated:
                log.warning("titan2.db migrated %s (backup %s)", store.migrated, store.backup_path)
            self.store = store
            return self._run_with_alerts(loaded)
        finally:
            store.close()

    def _run_with_alerts(self, loaded: settings_mod.LoadResult) -> int:
        heartbeat = alerts.Heartbeat()
        self.alerter = alerts.Alerter.from_env(self.env, heartbeat, clock=self.clock)
        self.alerter.start()
        code = EXIT_EXCEPTION_STORM
        try:
            code = self._serve(loaded, heartbeat)
        except Exception as exc:
            log.critical("titan2-bot crashed: %r", exc, exc_info=True)
            self.alerter.submit("CRITICAL", "BOT_CRASHED", f"titan2-bot crashed ({exc!r}); systemd restarts it and "
                                                           "recovery resumes the exits", "bot:crash")
            code = EXIT_EXCEPTION_STORM
            # already reported: the next start must not report this run again as killed (BOT_KILLED)
            self._event("SHUTDOWN", {"phase": "CRASHED", "exit": code, "reason": repr(exc)[:300]}, "CRITICAL")
        finally:
            if code in (EXIT_EXCEPTION_STORM, EXIT_FATAL):
                self.alerter.fail(f"titan2-bot exit {code}")
            elif code in (EXIT_OK, EXIT_RESTART):
                self.alerter.ping_now()                     # the day's last ping comes after the check's last tick
            self.alerter.stop(ALERTS_FLUSH_S)
        return code

    def _event(self, kind: str, payload: Mapping[str, Any], severity: str = "INFO") -> None:
        try:
            with self.store.tx():
                self.store.append_event(kind, wall=self.clock.now_wall(), mono=self.clock.mono(),
                                        payload=dict(payload), severity=severity)
        except Exception:
            log.exception("the %s event could not be recorded", kind)

    def _calendar(self) -> SessionCalendar:
        if self._calendar_in is not None:
            return self._calendar_in
        try:
            return SessionCalendar.load()
        except CalendarError as exc:
            log.critical("session calendar unusable (%s): no day is a trading session", exc)
            self.alerter.submit("CRITICAL", "CALENDAR", f"the session calendar is unusable: {exc}; titan2 treats "
                                                        "every day as closed", "calendar")
            return SessionCalendar.empty()

    # -- steps 6-16 --------------------------------------------------------------------------------------------
    def _check_last_run(self, calendar: SessionCalendar) -> None:
        """How the previous run ended (review F-16), read before this run's BOOT: its BOOT without a later runtime
        SHUTDOWN (FINAL_SHUTDOWN_PHASES) means it died without a clean exit. Its last sign of life is the newest
        event after that BOOT. Inside that day's session hours: CRITICAL BOT_KILLED (key bot:killed) and a CRITICAL
        UNCLEAN_EXIT event; outside them a WARN UNCLEAN_EXIT event only. Never blocks the start."""
        try:
            boot = self.store.query_one("SELECT seq, ts FROM events WHERE kind = 'BOOT' ORDER BY seq DESC LIMIT 1")
            if boot is None:
                return
            seq = int(boot["seq"])
            if any((e["payload"] or {}).get("phase") in FINAL_SHUTDOWN_PHASES
                   for e in self.store.events(since_seq=seq, kind="SHUTDOWN")):
                return
            last = parse_wall(self.store.scalar("SELECT ts FROM events WHERE seq > ? ORDER BY seq DESC LIMIT 1",
                                                (seq,)) or boot["ts"])
            bounds = calendar.session_bounds(session_of(last))
        except Exception:
            log.exception("could not check how the previous run ended")
            return
        in_session = bounds is not None and bounds[0] <= last < bounds[1]
        when = f"{last:%Y-%m-%d %H:%M:%S} IST"
        self._event("UNCLEAN_EXIT", {"last_alive": iso(last), "in_session": in_session, "boot_seq": seq},
                    "CRITICAL" if in_session else "WARN")
        log.log(logging.CRITICAL if in_session else logging.WARNING,
                "the previous titan2-bot run died without a clean exit; last alive %s", when)
        if in_session and self.laptop:
            self.alerter.submit("CRITICAL", "BOT_KILLED",
                                f"titan2 (laptop) died without a clean exit during the session (the laptop slept, lost "
                                f"power or rebooted, or its window was closed); it was last alive at {when}. This "
                                "start's recovery resumes the exits. Its log is in the laptop's logs folder",
                                "bot:killed")
        elif in_session:
            self.alerter.submit("CRITICAL", "BOT_KILLED",
                                f"titan2-bot died without a clean exit during the session (watchdog, OOM kill, SIGKILL "
                                f"or host failure); it was last alive at {when}. This start's recovery resumes the "
                                "exits. Check `journalctl -u titan2-bot` and the kernel log for an OOM kill "
                                "(`journalctl -k | grep -i oom`)", "bot:killed")

    def _serve(self, loaded: settings_mod.LoadResult, heartbeat: alerts.Heartbeat) -> int:
        settings = loaded.settings
        calendar = self._calendar()
        self._check_last_run(calendar)
        boot = {"version": __version__, "pid": os.getpid(), "python": platform.python_version(), "safe": self.safe,
                "allow_live": self.allow_live, "var": str(self.var), "unsafe_cwd": self.unsafe_cwd}
        if self.laptop:
            boot["profile"] = laptop_mod.PROFILE
        self._event("BOOT", boot, "INFO" if self.unsafe_cwd is None else "CRITICAL")
        self._event("SETTINGS_LOADED" if loaded.valid else "SETTINGS_INVALID",
                    {"summary": loaded.summary(), "sha256": loaded.sha256, "errors": list(loaded.errors)[:10]},
                    "INFO" if loaded.valid else "CRITICAL")
        if self.laptop:
            self._refuse_live()
        today, now = self.clock.today(), self.clock.now_wall()
        trading = calendar.is_trading_day(today)
        cutoff = late_cutoff(calendar, today, settings) if trading else None
        # a start after today's EOD DONE (15:40) but before the cutoff (15:45): the engine restores DONE with the other
        #   EOD steps, so it would never send Exit(0) again and the process would run on into the next day (review
        #   round 2). The laptop's run_day starts this same BotProcess
        done = trading and self._done_today(today)
        recovery_only = False
        if not trading or (cutoff is not None and now >= cutoff) or done:
            why = ("not a trading session" if not trading
                   else f"after {cutoff:%H:%M}" if cutoff is not None and now >= cutoff
                   else "today's EOD timeline is done")
            n = nonterminal_structures(self.store)
            if n == 0:
                log.info("%s: %s and nothing to recover: READY + STOPPING, exit 0", today, why)
                self.notifier.notify("READY=1\nSTATUS=nothing to do today")
                if not trading:
                    self.keep_deadman(heartbeat, today)
                self.notifier.notify("STOPPING=1")
                self._event("SHUTDOWN", {"phase": "IDLE", "reason": why, "exit": EXIT_OK})
                return EXIT_OK
            log.warning("%s: %s, %d structure(s) not terminal: recovery and recon only, then exit 0", today, why, n)
            recovery_only = True
        rt = self.rt = self._build(settings, loaded, calendar, heartbeat, recovery_only)
        if self.unsafe_cwd is not None:
            rt.boot_alerts.append(Alert("CRITICAL", "INVARIANT", self.unsafe_cwd[:500], "invariant:unsafe_cwd"))
        if self.on_runtime is not None:
            self.on_runtime(rt)
        try:
            rt.start_threads()
            rt.core_q.put(RecoveryStart())                  # step 10: recovery before READY (exits resume first)
            core_loop(rt)
        finally:
            rt.shutdown()
        code = rt.exit_code if rt.exit_code is not None else EXIT_OK
        self._event("SHUTDOWN", {"phase": "EXITED", "exit": code, "reason": rt.exit_reason[:300]},
                    "INFO" if code in (EXIT_OK, EXIT_RESTART) else "CRITICAL")
        return code

    def _done_today(self, today: date) -> bool:
        """Today's EOD DONE step is recorded (a PHASE event of this session with step DONE): the day is over. A DONE
        row stamped later than now or than a later start was not written by a run on this day (an old `titan2 laptop
        --date` rehearsal in the real state folder: state.eod_phase_rows) and is ignored with a WARN, so it never
        turns the real day's start into a recovery-only exit (review round 3 follow-up)."""
        try:
            kept, ignored = eod_phase_rows(self.store, today, self.clock.now_wall())
        except Exception:
            log.exception("could not read today's EOD steps; starting as usual")
            return False
        for e in ignored:
            if e["payload"].get("step") == "DONE":
                log.warning("%s: ignoring the EOD DONE row stamped %s (seq %s): later than now or than a later "
                            "start, so not written by a run on this day (an old `titan2 laptop --date` rehearsal "
                            "in the real state folder?); starting as usual", today, e["ts"], e["seq"])
        return any(e["payload"].get("step") == "DONE" for e in kept)

    def _refuse_live(self) -> None:
        """Laptop: PAPER only. Every LIVE-mode strategy (mode or pending mode) runs as PAPER this session: with
        allow_live False its LIVE book refuses every entry (I14, G13) and its PAPER twin trades. Modes are kept."""
        try:
            live = sorted(r["strategy"] for r in self.store.strategy_modes()
                          if "LIVE" in (r.get("mode"), r.get("pending_mode")))
        except Exception:
            log.exception("cannot list the LIVE-mode strategies")
            live = ["?"]
        why = "; ".join(laptop_mod.LIVE_REFUSED_REASONS)
        if self.allow_live_env:
            log.warning("laptop profile: ALLOW_LIVE=1 is ignored, LIVE is refused (%s)", why)
        for name in live:
            log.warning("laptop profile: strategy %s is in LIVE mode; it runs as PAPER this session (%s)", name, why)
        self._event("LIVE_REFUSED", {"profile": laptop_mod.PROFILE, "allow_live_env": self.allow_live_env,
                                     "live_strategies": live, "reasons": list(laptop_mod.LIVE_REFUSED_REASONS)},
                    "WARN" if live or self.allow_live_env else "INFO")

    def keep_deadman(self, heartbeat: alerts.Heartbeat, today: date) -> None:
        """A weekday without an NSE session (a holiday): the dead-man check (alerts.DEADMAN_SCHEDULE) expects pings
        every weekday and healthchecks cannot know NSE holidays, so the process stays up as its pinger until the
        day's last tick, then returns (exit 0). Nothing else runs: no broker call, no order, no store write. An INFO
        alert says so, which also shows a calendar that wrongly closes a trading day. SIGTERM ends it at once.
        Called between READY=1 and STOPPING=1 only: after STOPPING=1 systemd allows TimeoutStopSec and then kills
        (so a recovery-only run on a holiday, which ends with STOPPING=1, does not keep the check fed)."""
        alerter = self.alerter
        until = ist_datetime(today, alerts.DEADMAN_LAST_TICK) + timedelta(minutes=1)
        if today.weekday() >= 5 or alerter is None or alerter.healthcheck is None or self.clock.now_wall() >= until:
            return
        log.info("%s: no session; feeding the dead-man check until %s", today, f"{until:%H:%M}")
        alerter.submit("INFO", "NO_SESSION", f"{today}: no NSE session in titan2's calendar; titan2 trades nothing "
                       f"today and only keeps the dead-man check fed until {until:%H:%M}", "no_session")
        self.notifier.notify(f"STATUS=no session today: dead-man pings until {until:%H:%M}")
        every = min(DEADMAN_KEEP_TICK_S, watchdog_interval(self.env))
        heartbeat.core_progress(self.clock.mono())
        alerter.ping_now()
        while self.stop.reason is None and self.clock.now_wall() < until:
            heartbeat.core_progress(self.clock.mono())
            self.notifier.notify("WATCHDOG=1")
            before = self.clock.mono()
            self.sleep(every)
            if self.clock.mono() <= before:                 # a clock that stands still could never reach `until`
                log.warning("the clock did not advance during a %.0f s sleep: dead-man keeping stops", every)
                break
        heartbeat.core_progress(self.clock.mono())

    def _token(self, clients: Mapping[str, Any], sess: Any, problem: str | None, api_key: str) -> str | None:
        """§9.5 step 7: the session token works (profile() as its own user), else the reason (-> HALT(TOKEN))."""
        if problem is not None:
            return problem
        if not api_key:
            return "ZERODHA_API_KEY is not set"
        try:
            token_mod.verify_session(sess, api_key, expected_user=self.env.get("ZERODHA_USER_ID", "").strip() or None,
                                     kite_factory=lambda *_a, **_k: clients["reads"])
        except token_mod.TokenError as exc:
            return str(exc)
        log.info("token: session %s works", sess.fingerprint)
        return None

    def _build(self, settings: settings_mod.Settings, loaded: settings_mod.LoadResult, calendar: SessionCalendar,
               heartbeat: alerts.Heartbeat, recovery_only: bool) -> Runtime:
        env, var, clock = self.env, self.var, self.clock
        today = clock.today()
        # step 7: the token and one Kite client per thread that calls Kite (requests sessions are not shared)
        api_key = env.get("ZERODHA_API_KEY", "").strip()
        sess, problem = None, None
        session_file = token_mod.session_path(var)
        try:
            sess = token_mod.read_session(session_file)
        except token_mod.SessionError as exc:
            problem = str(exc)
        access = sess.access_token if sess is not None else None
        clients = {name: self.kite_factory(api_key, access) for name in ("orders", "reads", "quotes", "prep")}
        for client in clients.values():
            self.check_kite(instance=client)
        token_problem = self._token(clients, sess, problem, api_key)
        # step 8: quotes, instruments (today's cache), strategies, handlers, lanes, paper, feed, engine context
        quotes = QuoteCache(mono=clock.mono)
        core_q: queue.Queue = queue.Queue()
        prep_state = PrepState(today, settings)
        try:
            cached = ins.InstrumentStore.from_cache(var / "cache", today, calendar,
                                                    min_td_index=settings.MIN_TD_TO_EXPIRY_INDEX,
                                                    min_td_stock=settings.MIN_TD_TO_EXPIRY_STOCK)
        except (ins.InstrumentsError, ValueError) as exc:
            log.warning("today's cached instruments dump is unusable (%s); prep fetches it", exc)
            cached = None
        if cached is not None:
            prep_state.set_instruments(cached)
        rt = Runtime(store=self.store, ctx=None, clock=clock, core_q=core_q, heartbeat=heartbeat,
                     notifier=self.notifier, alerter=self.alerter, prep_state=prep_state,
                     recovery_only=recovery_only, stop=self.stop, watchdog_every_s=watchdog_interval(env))
        rt.keep_reactor = self.laptop
        rt.boot_halts.extend(self.boot_halts)
        runners, failed = strategies.load(settings, settings_mod.exit_profile_problems, max_per_min=INTENT_MAX_PER_MIN)
        for name, problems in sorted(failed.items()):
            rt.boot_alerts.append(Alert("CRITICAL", "STRATEGY_REFUSED", f"{name} refused at registry load and "
                                        f"disabled: {'; '.join(problems)[:400]}", f"strategy_refused:{name}"))
        try:
            handlers = plugins.load_handlers({"settings": settings, "calendar": calendar, "clock": clock,
                                              "quotes": quotes})
        except plugins.PluginError as exc:
            handlers = plugins.phase1_handlers()
            rt.boot_alerts.append(Alert("CRITICAL", "PLUGIN_FAILED", f"{exc}; phase-1 handlers only", "plugins"))
        # the laptop sends no LIVE order of any kind: a titan2 LIVE position in the shared Kite account is EC2's
        broker = KiteBroker(clients["orders"], entries_allowed=self.allow_live, orders_allowed=not self.laptop,
                            clock=clock)
        rt.order_lane = lanes.OrderLane(broker, core_q.put, mono=clock.mono)
        rt.reads_lane = lanes.ReadsLane(KiteData(clients["reads"], clock=clock), core_q.put, quotes.put,
                                        mono=clock.mono, quote_data=KiteData(clients["quotes"], clock=clock))
        exchange = PaperExchange(var / "db" / "paper_exchange.db", settings=settings, clock=clock, quotes=quotes,
                                 calendar=calendar, instruments=prep_state.contract,
                                 request_depth=lambda cmd: rt.reads_lane.submit(cmd, reply=rt.paper_sim.wake))
        rt.paper_sim = PaperSim(exchange, core_q.put)
        token_check = token_mod.make_token_prep(session_file, api_key, sess.fingerprint if sess else None,
                                                kite_factory=self.kite_factory)
        if recovery_only:                                   # only the §9.7 token re-check (HALT(TOKEN) may be up)
            rt.prep = PrepWorker({"TOKEN": token_check}, core_q.put)
        else:
            signal_q = SignalQueue()
            rt.feed = Feed(self.ticker_factory(api_key, access), quotes, core_q.put, signal_q, rt.symbol_of,
                           clock=clock, reactor=self.reactor)
            rt.signals = SignalsHost(signal_q=signal_q, runners=runners, quotes=quotes, clock=clock,
                                     calendar=calendar, settings=settings, post=core_q.put, alert=rt.alert,
                                     prep_state=prep_state, feed_health=rt.feed.health, mode_of=rt.mode_of)
            data = KiteData(clients["prep"], clock=clock)
            prepper = Prepper(state=prep_state, data=data, var_dir=var, calendar=calendar, settings=settings,
                              post=core_q.put, on_instruments=rt.subscribe_underlyings,
                              on_prev_bars=rt.signals.set_prev_session,
                              ban_fetch=self.ban_fetch or ins.nse_fetcher(ins.ban_list_url(env)),
                              token_check=token_check)
            rt.prep = PrepWorker(prepper.handlers(), core_q.put)
            rt.selfcheck = SelfcheckRunner(
                lambda phase: self._selfcheck_context(phase, loaded, calendar, prep_state, api_key, access),
                core_q.put, phases={"prestart": laptop_mod.SELFCHECK_PHASE} if self.laptop else None)
        feed, signals = rt.feed, rt.signals
        rt.ctx = EngineContext(
            settings=settings, calendar=calendar, clock=clock, quotes=quotes,
            versions={name: r.version for name, r in runners.items()},
            version_inputs={name: strategies.version_inputs(strategies.REGISTRY[name], settings) for name in runners},
            exit_profiles={name: strategies.REGISTRY[name].EXIT_PROFILE for name in runners},
            disabled=frozenset(failed), handlers=handlers, allow_live=self.allow_live, live_orders=not self.laptop,
            stop_flatten_by=f"CLI:{laptop_mod.REQUESTED_BY}" if self.laptop else None, safe_mode=self.safe,
            settings_errors=tuple(loaded.errors), instruments=prep_state.instruments,
            ban_list=prep_state.ban_symbols, feed_health=feed.health if feed is not None else _no_feed,
            take_dirty=feed.take_dirty if feed is not None else frozenset,
            signals_degraded=signals.degraded if signals is not None else (lambda: False),
            unacked_critical=self.alerter.unacked_critical, pub_dir=var / "pub", reports_dir=var / "reports",
            exports_dir=var / "exports",
            command_dbs={"DASH": var / "dash" / "commands_dash.db", "CLI": var / "db" / "commands_cli.db"},
            start_mono=self.start_mono)
        if token_problem is not None:
            log.critical("token: %s: HALT(TOKEN); exits are impossible until a working session exists", token_problem)
            rt.boot_halts.append(("TOKEN", "GLOBAL", "AUTO", f"token check at start failed: {token_problem}"[:500]))
        return rt

    def _selfcheck_context(self, phase: str, loaded: settings_mod.LoadResult, calendar: SessionCalendar,
                           prep_state: PrepState, api_key: str, access: str | None) -> selfcheck.Context:
        """The prestart self-check reads what prep already loaded (no second download) and its own Kite client."""
        inst = prep_state.instruments()
        missing = ins.InstrumentsError("today's instruments are not loaded (prep failed)")

        def no_instruments() -> Any:
            raise missing

        more = {"env_file": laptop_mod.default_env_file(self.env)} if self.laptop else {}
        return selfcheck.Context(
            phase, env=self.env, now=self.clock.now_wall(), var_dir=self.var, app_dir=self.cwd, settings=loaded,
            calendar=calendar, instruments=inst if inst is not None else no_instruments,
            ban_list=prep_state.ban(), bars=prep_state.coverage(),
            kite=lambda: KiteData(self.kite_factory(api_key, access), clock=self.clock), **more)


def run_bot(**kwargs: Any) -> int:
    """`titan2 bot`: see BotProcess for the injectable dependencies."""
    return BotProcess(**kwargs).run()


def main(safe: bool = False, env: Mapping[str, str] | None = None) -> int:
    """The systemd entrypoint (`python -m titan2 bot [--safe]`): logging, then the startup sequence."""
    e = dict(os.environ if env is None else env)
    setup_logging(laptop_mod.default_log_dir(e, DEFAULT_LOG_DIR))
    threading.current_thread().name = "core"
    log.info("titan2 %s bot starting: pid %d, python %s, %s", __version__, os.getpid(), platform.python_version(),
             iso(RealClock().now_wall()))
    started = time.monotonic()
    code = run_bot(safe=safe, env=e)
    log.info("titan2 bot exits %d after %.1f s", code, time.monotonic() - started)
    return code
