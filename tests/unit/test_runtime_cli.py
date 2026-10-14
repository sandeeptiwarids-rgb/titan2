"""U17: the runtime (core loop, dispatch, READY, watchdog, flock, startup gates), the CLI and the plugin loader.

The core loop runs on the test thread against a FakeClock: SimRuntime.next_event() serves core_q at once and, when it
is empty, lets the paper simulator answer and then advances the clock by the wait the loop asked for, so time moves
only while the core is idle (deterministic and fast). The real Engine and Store are used where the seam matters
(READY after recovery, SIGTERM, the recovery-only run); small stand-in engines where the loop's own rules are tested.
"""

from __future__ import annotations

import gzip
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import titan2.__main__ as cli
from titan2 import alerts, plugins, runtime
from titan2 import settings as settings_mod
from titan2 import store as store_mod
from titan2 import token as token_mod
from titan2.broker import lanes
from titan2.broker.paper import PaperExchange, PaperSim
from titan2.clock import IST, Clock, FakeClock, SessionCalendar, ist_datetime, parse_wall
from titan2.core import operator, positions
from titan2.core.events import (
    Alert, BarsClosed, Event, Exit, FetchMargins, FetchOrders, FetchPositions, FetchQuotes, FetchTradebook,
    IntentReceived, MarginsResult, OrdersSnapshot, Place, PlaceResult, PositionsSnapshot, Prep, PrepResult,
    QuoteResult, RecoveryStart, RunSelfcheck, SelfcheckResult, StartSignals, Subscribe, Timer, TradebookSnapshot,
)
from titan2.core.single_leg import SingleLegHandler
from titan2.core.state import EngineContext
from titan2.instruments import InstrumentStore
from titan2.market.feed import FeedLink, SignalQueue
from titan2.market.quotes import QuoteCache
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, Bar, Direction, FeedHealth, Instrument, Intent, InvariantViolation, Margins,
    OrderRequest, Policy, Recheck, Role, Side, StrikePref, StructState, StructureType, Underlying, UnderlyingTick,
)
from titan2.settings import DEFAULTS
from titan2.store import Store
from titan2.strategies import OpeningDrive, StrategyRunner
from titan2.strategies.base import RunResult

APP = Path(__file__).resolve().parents[2]                # agentic_trader/: `python -m titan2` runs from here
S = date(2026, 10, 12)                                   # a Monday session
FRI, SAT = date(2026, 10, 9), date(2026, 10, 10)
HOLIDAY = date(2026, 10, 20)                             # a Tuesday holiday in CAL
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]})


@pytest.fixture(autouse=True)
def _fresh_settings():
    """run_bot reads settings once per process (load_once): every test starts and ends without a cached read."""
    settings_mod._reset_load_once()
    yield
    settings_mod._reset_load_once()


# ---------------------------------------------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------------------------------------------
def recorder() -> runtime.Notifier:
    return runtime.Notifier(send=lambda payload: None)


def states(n: runtime.Notifier, *, watchdog: bool = False) -> list[str]:
    """The first line of every sd_notify message, optionally without the WATCHDOG pings."""
    return [s.split("\n", 1)[0] for s in n.sent if watchdog or s != "WATCHDOG=1"]


class FakeAlerter:
    def __init__(self, on_dispatch: Callable[[Alert], None] | None = None) -> None:
        self.dispatched: list[Alert] = []
        self.submitted: list[tuple] = []
        self.ready_pings = 0
        self.on_dispatch = on_dispatch

    def dispatch(self, cmd: Alert) -> bool:
        if self.on_dispatch is not None:
            self.on_dispatch(cmd)
        self.dispatched.append(cmd)
        return True

    def submit(self, severity: str, code: str, text: str, dedup_key: str | None = None) -> bool:
        self.submitted.append((severity, code, text, dedup_key))
        return True

    def ping_ready(self) -> None:
        self.ready_pings += 1

    def unacked_critical(self) -> int:
        return 0


class SimRuntime(runtime.Runtime):
    """A Runtime whose idle waits advance the FakeClock instead of blocking. at(t, fn) runs fn on the core thread
    once the clock reaches start + t; the loop stops once the clock passes start + run_for."""

    def __init__(self, *args: Any, run_for: float = 30.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.t0 = self.clock.mono()
        self.deadline = self.t0 + run_for
        self.hooks: list[tuple[float, Callable[[], Any]]] = []

    def at(self, after_s: float, fn: Callable[[], Any]) -> None:
        self.hooks.append((self.t0 + after_s, fn))
        self.hooks.sort(key=lambda h: h[0])

    def next_event(self, timeout: float) -> Event:
        while self.hooks and self.clock.mono() >= self.hooks[0][0]:
            self.hooks.pop(0)[1]()
        try:
            return self.core_q.get_nowait()
        except queue.Empty:
            pass
        if self.paper_sim is not None:
            self.paper_sim.drain()                    # the simulator's answers go to core_q
            try:
                return self.core_q.get_nowait()
            except queue.Empty:
                pass
        if self.clock.mono() >= self.deadline:
            self.stopping = True
        self.clock.advance(timeout)
        raise queue.Empty


@dataclass(frozen=True)
class Ping(Event):
    n: int
    breaks: bool = False                              # check_invariants raises after this event
    raises: bool = False                              # handle raises


class World:
    """Shared by every StubEngine the loop loads (it is passed as the engine context)."""

    def __init__(self, clock: FakeClock, slow_s: float = 0.0) -> None:
        self.clock = clock
        self.slow_s = slow_s
        self.loads = 0
        self.timers = 0
        self.halts: list[tuple] = []
        self.pending_break = False


class StubEngine:
    """The loop's contract only (load, handle, check_invariants, on_invariant_violation, raise_halt, recovery_done,
    state). Every Ping writes one events row and returns one Alert naming that row's seq."""

    recovery_done = False

    def __init__(self, store: Store, world: World) -> None:
        self.store = store
        self.world = world
        self.state = SimpleNamespace(modes={}, legs={}, tails={})

    @classmethod
    def load(cls, store: Store, world: World) -> StubEngine:
        world.loads += 1
        return cls(store, world)

    def _row(self, kind: str, payload: dict) -> int:
        return self.store.append_event(kind, wall=self.world.clock.now_wall(), mono=self.world.clock.mono(),
                                       payload=payload)

    def handle(self, ev: Event) -> list:
        if isinstance(ev, Timer):
            self.world.timers += 1
            return []
        seq = self._row("TEST", {"n": ev.n})
        self.world.pending_break = ev.breaks
        if ev.raises:
            raise RuntimeError(f"boom {ev.n}")
        if self.world.slow_s:
            self.world.clock.advance(self.world.slow_s)
        return [Alert("INFO", "T", f"seq {seq}", f"t{seq}")]

    def check_invariants(self) -> None:
        if self.world.pending_break:
            self.world.pending_break = False
            raise InvariantViolation("I3", "test break", "GLOBAL")

    def on_invariant_violation(self, iv: InvariantViolation) -> list:
        seq = self._row("IV", {"code": iv.code})
        return [Alert("CRITICAL", "IV", f"seq {seq}", "iv")]

    def raise_halt(self, kind: str, scope: str, clears: str, reason: str) -> list:
        self.world.halts.append((kind, scope, clears, reason))
        seq = self._row("HALTED", {"kind": kind})
        return [Alert("CRITICAL", f"HALT_{kind}", f"seq {seq}", f"halt:{kind}")]


def stub_runtime(tmp_path: Path, *, world: World, run_for: float, **kw: Any) -> SimRuntime:
    store = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    return SimRuntime(store=store, ctx=world, clock=world.clock, engine_cls=StubEngine, run_for=run_for, **kw)


def answer(cmd: Any, mono: float) -> list[Event]:
    """What a healthy broker would answer to a read: ok and empty."""
    if isinstance(cmd, FetchOrders):
        return [OrdersSnapshot(cmd.account, True, (), mono, mono)]
    if isinstance(cmd, FetchPositions):
        return [PositionsSnapshot(cmd.account, True, (), mono, mono)]
    if isinstance(cmd, FetchTradebook):
        return [TradebookSnapshot(cmd.account, True, (), mono)]
    if isinstance(cmd, FetchMargins):
        return [MarginsResult(cmd.account, True, Margins(cmd.account, 500_000.0, {}), mono)]
    if isinstance(cmd, FetchQuotes):
        return [QuoteResult(cmd.request_id, True, (), tuple(cmd.symbols))]
    return lanes.failure_events(cmd, "test: not served", mono)


class Lane:
    """lane-reads / lane-live-orders stand-in. Answers at once, or holds every command while gated."""

    def __init__(self, post: Callable[[Event], None], clock: Clock, gated: bool = False) -> None:
        self.post, self.clock, self.gated = post, clock, gated
        self.seen: list = []
        self.held: list = []
        self.started = self.stopped = False

    def submit(self, cmd: Any, reply: Any = None) -> None:
        self.seen.append(cmd)
        if self.gated:
            self.held.append(cmd)
        else:
            for ev in answer(cmd, self.clock.mono()):
                self.post(ev)

    def release(self) -> None:
        self.gated = False
        held, self.held = self.held, []
        for cmd in held:
            for ev in answer(cmd, self.clock.mono()):
                self.post(ev)

    def start(self) -> None:
        self.started = True

    def stop(self, join_s: float | None = None) -> None:
        self.stopped = True


class FakeFeed:
    def __init__(self) -> None:
        self.requests: list[tuple] = []
        self.dispatched: list = []
        self.started = self.stopped = False

    def request(self, tokens: Any, mode: str | None) -> None:
        self.requests.append((tuple(tokens), mode))

    def dispatch(self, cmd: Any) -> None:
        self.dispatched.append(cmd)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def health(self) -> FeedHealth:
        return FeedHealth(False, None, None, 0, False)

    def take_dirty(self) -> frozenset:
        return frozenset()


class FakeSignals:
    def __init__(self) -> None:
        self.started = self.enabled = self.stopped = False

    def start(self) -> None:
        self.started = True

    def enable(self) -> None:
        self.enabled = True

    def stop(self, join_s: float | None = None) -> None:
        self.stopped = True

    def degraded(self) -> bool:
        return False


class FakePrep:
    """Answers every Prep at once with a failure (no network in tests)."""

    def __init__(self, post: Callable[[Event], None]) -> None:
        self.post = post
        self.kinds: list[str] = []

    def submit(self, cmd: Prep) -> None:
        self.kinds.append(cmd.kind)
        self.post(PrepResult(cmd.kind, False, "test: no prep"))

    def start(self) -> None:
        pass

    def stop(self, join_s: float | None = None) -> None:
        pass


# ---------------------------------------------------------------------------------------------------------------
# The core loop (§3.3)
# ---------------------------------------------------------------------------------------------------------------
def test_commit_before_dispatch(tmp_path):
    """Every command is dispatched after its transaction committed (another connection already sees the row and the
    store is outside any transaction). An invariant violation rolls the event back: its commands are never
    dispatched; the reloaded engine's on_invariant_violation commands are, after their own commit."""
    with store_mod.core_thread():
        clock = FakeClock(ist_datetime(S, "10:00:00"))
        world = World(clock)
        seen: list[tuple] = []
        rt = stub_runtime(tmp_path, world=world, run_for=2.0)

        def check(cmd: Alert) -> None:
            seq = int(cmd.text.split()[1])
            con = store_mod.connect_readonly(tmp_path / "db" / "titan2.db")
            try:
                row = con.execute("SELECT kind FROM events WHERE seq = ?", (seq,)).fetchone()
            finally:
                con.close()
            seen.append((cmd.code, row["kind"] if row is not None else None, rt.store.in_tx))

        rt.alerter = FakeAlerter(on_dispatch=check)
        for n, breaks in ((1, False), (2, True), (3, False)):
            rt.core_q.put(Ping(n, breaks=breaks))
        runtime.core_loop(rt)
        kinds = [(e["kind"], e["payload"].get("n", e["payload"].get("code"))) for e in rt.store.events()]
        rt.store.close()
    assert seen == [("T", "TEST", False), ("IV", "IV", False), ("T", "TEST", False)]
    assert kinds == [("TEST", 1), ("IV", "I3"), ("TEST", 3)]            # Ping 2's row was rolled back
    assert world.loads == 2                                             # the start, and the reload after I3
    assert world.timers > 0 and rt.exit_code is None


def test_exception_storm_exit3(tmp_path):
    """Each exception rolls back, reloads the engine and raises HALT(EXCEPTION, GLOBAL, ACK); 20 inside 60 s keep
    running, the 21st exits 3 (STOPPING=1) and nothing after it is handled."""
    with store_mod.core_thread():
        clock = FakeClock(ist_datetime(S, "10:00:00"))
        world = World(clock)
        notifier = recorder()
        rt = stub_runtime(tmp_path, world=world, run_for=200.0, notifier=notifier, alerter=FakeAlerter())
        running: list[bool] = []
        for n in range(20):
            rt.core_q.put(Ping(n, raises=True))
        rt.at(61.0, lambda: [rt.core_q.put(Ping(100 + n, raises=True)) for n in range(20)])
        rt.at(70.0, lambda: running.append(not rt.stopping))
        rt.at(80.0, lambda: [rt.core_q.put(Ping(200 + n, raises=True)) for n in range(3)])
        runtime.core_loop(rt)
        rows = [e["payload"]["n"] for e in rt.store.events(kind="TEST")]
        rt.store.close()
    assert running == [True]                       # 20 at t0, 20 at t0+61: never more than 20 inside 60 s
    assert rt.exit_code == runtime.EXIT_EXCEPTION_STORM and rt.stopping
    assert len(world.halts) == 41 and world.loads == 42
    assert world.halts[0] == ("EXCEPTION", "GLOBAL", "ACK", "RuntimeError('boom 0')")
    assert world.halts[-1][3] == "RuntimeError('boom 200')"
    assert rows == []                              # every failing event's rows were rolled back
    assert [a.code for a in rt.alerter.dispatched] == ["HALT_EXCEPTION"] * 41
    assert states(notifier) == ["STOPPING=1"]
    assert rt.core_q.qsize() == 2                  # Ping 201 and 202 were never handled


class SlowTimerEngine(StubEngine):
    """Every Timer takes 300 ms to handle (a slow fsync under synchronous=FULL, the 15:38 G1 run, many legs)."""

    def handle(self, ev: Event) -> list:
        if isinstance(ev, Timer):
            self.world.timers += 1
            self.world.clock.advance(0.30)
            return []
        return super().handle(ev)


def test_slow_timers_never_starve_core_q(tmp_path):
    """A Timer that overruns its 250 ms slot must not be followed by another Timer while events wait: the events
    queued meanwhile (a PlaceResult, an order's terminal snapshot, a positions read) get the next slot."""
    with store_mod.core_thread():
        clock = FakeClock(ist_datetime(S, "10:00:00"))
        world = World(clock)
        store = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        rt = SimRuntime(store=store, ctx=world, clock=clock, engine_cls=SlowTimerEngine, run_for=10.0,
                        alerter=FakeAlerter())
        trace: list[str] = []
        inner = rt.after_event

        def after(engine: Any, ev: Event) -> None:
            inner(engine, ev)
            trace.append(type(ev).__name__)
            if clock.mono() >= rt.t0 + 10.0:
                rt.stopping = True

        rt.after_event = after
        for n in (1, 2, 3):
            rt.core_q.put(Ping(n))
        rt.at(4.0, lambda: rt.core_q.put(Ping(4)))
        runtime.core_loop(rt)
        handled = [e["payload"]["n"] for e in rt.store.events(kind="TEST")]
        rt.store.close()
    assert handled == [1, 2, 3, 4], f"after {world.timers} Timers only {handled} of the queued events were handled"
    assert trace[:5] == ["Timer", "Ping", "Ping", "Ping", "Timer"]          # the backlog runs between two Timers
    assert world.timers >= 25                                               # and the Timers keep running


def test_reload_storm_alerts_and_keeps_running(tmp_path):
    """Invariant violations roll back and reload like exceptions, but a storm of them (more than EXC_LIMIT in
    EXC_WINDOW_S: a broker report that contradicts the DB on every poll) raises one CRITICAL INVARIANT alert and does
    not exit 3: a restart would only re-read the same report, and inside the process exits continue."""
    with store_mod.core_thread():
        clock = FakeClock(ist_datetime(S, "10:00:00"))
        world = World(clock)
        rt = stub_runtime(tmp_path, world=world, run_for=30.0, notifier=recorder(), alerter=FakeAlerter())
        for n in range(25):
            rt.core_q.put(Ping(n, breaks=True))
        rt.at(5.0, lambda: rt.core_q.put(Ping(100)))
        runtime.core_loop(rt)
        handled = [e["payload"]["n"] for e in rt.store.events(kind="TEST")]
        rt.store.close()
    storm = [a for a in rt.alerter.dispatched if a.dedup_key == "invariant:reload_storm"]
    assert storm and storm[0].severity == "CRITICAL" and storm[0].code == "INVARIANT"
    codes = [a.code for a in rt.alerter.dispatched]
    assert codes.index("INVARIANT") == runtime.EXC_LIMIT                     # with the 21st violation, not before
    assert rt.exit_code is None and world.loads == 26 and handled == [100]   # still running, still handling


def test_watchdog_progress(tmp_path):
    """WATCHDOG=1 only while the last Timer finished < 2 s ago: never before core progress, never after a slow
    event, again once the next Timer completes; rate-limited to every_s."""
    hb = alerts.Heartbeat()
    notifier = recorder()
    wd = runtime.Watchdog(notifier, hb, every_s=5.0)
    assert not wd.tick(100.0)                                   # no Timer has completed yet
    hb.core_progress(100.0)
    assert wd.tick(100.5)
    assert not wd.tick(101.0)                                   # rate limit
    assert not wd.tick(106.0)                                   # last progress 6 s ago: stale
    hb.core_progress(106.0)
    assert wd.tick(106.1)
    assert states(notifier, watchdog=True) == ["WATCHDOG=1", "WATCHDOG=1"]
    assert runtime.watchdog_interval({"WATCHDOG_USEC": "30000000"}) == 10.0
    assert runtime.watchdog_interval({"WATCHDOG_USEC": "1500000"}) == 1.0
    assert runtime.watchdog_interval({}) == runtime.WATCHDOG_DEFAULT_EVERY_S

    with store_mod.core_thread():
        clock = FakeClock(ist_datetime(S, "10:00:00"))
        world = World(clock, slow_s=3.0)                         # every Ping takes 3 s to handle
        rt = stub_runtime(tmp_path, world=world, run_for=1.0, notifier=recorder(), alerter=FakeAlerter(),
                          watchdog_every_s=0.0)
        trace: list[tuple[str, int]] = []
        inner = rt.after_event

        def spy(engine: Any, ev: Event) -> None:
            before = rt.watchdog.pings
            inner(engine, ev)
            trace.append((type(ev).__name__, rt.watchdog.pings - before))

        rt.after_event = spy
        rt.core_q.put(Ping(1))
        runtime.core_loop(rt)
        rt.store.close()
    assert trace[:3] == [("Timer", 1), ("Ping", 0), ("Timer", 1)]
    assert all(pinged == 1 for kind, pinged in trace if kind == "Timer")


# ---------------------------------------------------------------------------------------------------------------
# READY after recovery, SIGTERM, dispatch
# ---------------------------------------------------------------------------------------------------------------
def real_runtime(tmp_path: Path, clock: FakeClock, *, gate_live: bool) -> tuple[SimRuntime, dict]:
    """The real Engine, Store and PaperExchange; LIVE reads on a gated stand-in lane; fake feed/signals/prep."""
    core_q: queue.Queue = queue.Queue()
    quotes = QuoteCache(mono=clock.mono)
    feed, signals, alerter = FakeFeed(), FakeSignals(), FakeAlerter()
    reads, orders = Lane(core_q.put, clock, gated=gate_live), Lane(core_q.put, clock)
    px = PaperExchange(tmp_path / "db" / "paper_exchange.db", settings=DEFAULTS, clock=clock, quotes=quotes,
                       calendar=CAL, thread_check=False)
    ctx = EngineContext(settings=DEFAULTS, calendar=CAL, clock=clock, quotes=quotes, handlers=plugins.load_handlers(),
                        feed_health=feed.health, take_dirty=feed.take_dirty, unacked_critical=alerter.unacked_critical,
                        start_mono=clock.mono())
    store = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    rt = SimRuntime(store=store, ctx=ctx, clock=clock, core_q=core_q, notifier=recorder(), alerter=alerter,
                    order_lane=orders, reads_lane=reads, paper_sim=PaperSim(px, core_q.put), prep=FakePrep(core_q.put),
                    feed=feed, signals=signals, run_for=60.0)
    return rt, {"feed": feed, "signals": signals, "alerter": alerter, "reads": reads, "px": px}


def test_ready_after_recovery(tmp_path):
    """READY=1 (plus the dead-man ping) only once the engine's recovery is done: while the LIVE recovery reads are
    unanswered there is no READY, no feed, no signals thread and no prep. Then the WS feed and the signals thread
    start. A SIGTERM afterwards becomes one Shutdown; the engine answers Exit(0) and the loop ends (STOPPING=1)."""
    with store_mod.core_thread():
        clock = FakeClock(ist_datetime(S, "08:50:00"))
        rt, parts = real_runtime(tmp_path, clock, gate_live=True)
        before: dict = {}

        def open_gate() -> None:
            before.update(ready=rt.ready, sent=states(rt.notifier), feed=parts["feed"].started,
                          signals=parts["signals"].started, pings=parts["alerter"].ready_pings,
                          prep=list(rt.prep.kinds), held=[type(c).__name__ for c in parts["reads"].held],
                          recovering=rt.ctx.recovery_started and not rt.ctx.recovered)
            parts["reads"].release()

        rt.at(5.0, open_gate)
        rt.at(15.0, lambda: rt.stop.request("SIGTERM"))
        rt.core_q.put(RecoveryStart())
        runtime.core_loop(rt)
        events = rt.store.events()
        rt.store.close()
        parts["px"].close()
    assert before["recovering"] and not before["ready"] and before["sent"] == []
    assert not before["feed"] and not before["signals"] and before["pings"] == 0 and before["prep"] == []
    assert {"FetchOrders", "FetchPositions", "FetchTradebook"} <= set(before["held"])
    assert states(rt.notifier) == ["READY=1", "STOPPING=1"]
    assert rt.ready and parts["alerter"].ready_pings == 1
    assert parts["feed"].started and parts["signals"].started
    assert rt.prep.kinds and set(rt.prep.kinds) <= {"INSTRUMENTS", "BAN_LIST", "PREV_BARS"}
    kinds = [e["kind"] for e in events]
    assert kinds.count("READY") == 1 and kinds.count("RECOVERY") == 3
    assert kinds.index("READY") > max(i for i, k in enumerate(kinds) if k == "RECOVERY")
    shutdown = [e["payload"] for e in events if e["kind"] == "SHUTDOWN"]
    assert shutdown[0]["reason"] == "SIGTERM" and shutdown[-1]["phase"] == "EXIT"
    assert rt.exit_code == 0 and rt.stopping


def test_dispatch_routes_and_refusals():
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    got: dict[str, list] = {k: [] for k in ("orders", "reads", "paper", "prep", "selfcheck")}
    comp = {k: SimpleNamespace(submit=got[k].append) for k in ("orders", "reads", "paper", "prep")}
    feed, signals, alerter, notifier = FakeFeed(), FakeSignals(), FakeAlerter(), recorder()
    rt = runtime.Runtime(store=None, ctx=None, clock=clock, notifier=notifier, alerter=alerter,
                         order_lane=comp["orders"], reads_lane=comp["reads"], paper_sim=comp["paper"],
                         prep=comp["prep"], selfcheck_runner=SimpleNamespace(run=got["selfcheck"].append),
                         feed=feed, signals=signals)
    req = OrderRequest(Account.LIVE, "T2ELL26101200101", Role.ENTRY, "NFO:X26OCT100CE", "X26OCT100CE", "NFO", 7,
                       Side.BUY, 65, 10.0, 65, 0.05)
    live, paper = Place(Account.LIVE, req.tag, req), Place(Account.PAPER, "T2EPL26101200101", req)
    cmds = [live, paper, FetchPositions(Account.LIVE, 0), FetchPositions(Account.SHADOW, 2),
            FetchQuotes("q:1", ("NFO:X26OCT100CE",), 0), Prep("BAN_LIST"), Subscribe((7,), "full"), StartSignals(),
            RunSelfcheck("prestart"), Alert("WARN", "X", "text", "k")]
    for c in cmds:
        rt.dispatch(c)
    assert got["orders"] == [live] and got["paper"] == [paper, cmds[3]] and got["reads"] == [cmds[2], cmds[4]]
    assert got["prep"] == [cmds[5]] and got["selfcheck"] == ["prestart"] and feed.dispatched == [cmds[6]]
    assert signals.enabled and alerter.dispatched == [cmds[9]] and rt.core_q.empty() and rt.exit_code is None
    rt.dispatch(Exit(75))
    assert rt.exit_code == 75 and rt.stopping and states(notifier) == ["STOPPING=1"]

    # nothing to run a command: answered conservatively (never sent), CRITICAL alert, never silence
    bare = runtime.Runtime(store=None, ctx=None, clock=clock, alerter=FakeAlerter())
    for c in (live, FetchQuotes("q:2", ("NFO:A",), 0), Prep("PREV_BARS"), RunSelfcheck("prestart"),
              Subscribe((7,), "full"), StartSignals()):
        bare.dispatch(c)
    answers = []
    while not bare.core_q.empty():
        answers.append(bare.core_q.get_nowait())
    assert isinstance(answers[0], PlaceResult) and answers[0].outcome == "REJECTED"
    assert answers[0].error_class == "LOCAL" and answers[0].order_pk == live.order_pk
    assert isinstance(answers[1], QuoteResult) and not answers[1].ok and answers[1].missing == ("NFO:A",)
    assert isinstance(answers[2], PrepResult) and not answers[2].ok
    assert isinstance(answers[3], SelfcheckResult) and not answers[3].ok and len(answers) == 4
    assert [a.code for a in bare.alerter.dispatched] == ["DISPATCH_FAILED"] * 4

    # a component that raises is treated the same way
    boom = runtime.Runtime(store=None, ctx=None, clock=clock, alerter=FakeAlerter(),
                           order_lane=SimpleNamespace(submit=lambda c: (_ for _ in ()).throw(OSError("queue gone"))))
    boom.dispatch(live)
    (ev,) = [boom.core_q.get_nowait()]
    assert isinstance(ev, PlaceResult) and ev.outcome == "REJECTED" and "queue gone" in ev.message


def test_sigterm_handler_sets_flag_and_posts_one_shutdown():
    """The signal handler only sets a flag (nothing that could deadlock); the core loop turns it into exactly one
    Shutdown event, however many signals arrive."""
    import signal

    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    stop = runtime.StopRequest()
    try:
        runtime.install_signal_handlers(stop)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is signal.getsignal(signal.SIGINT)
        handler(signal.SIGTERM, None)
        handler(signal.SIGINT, None)                       # a second signal changes nothing
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    assert stop.reason == "SIGTERM"
    rt = runtime.Runtime(store=None, ctx=None, clock=FakeClock(ist_datetime(S, "10:00")), stop=stop)
    rt.poll_stop()
    rt.poll_stop()
    (ev,) = [rt.core_q.get_nowait() for _ in range(rt.core_q.qsize())]
    assert ev.reason == "SIGTERM"


# ---------------------------------------------------------------------------------------------------------------
# The instance lock and the startup gates (§9.5)
# ---------------------------------------------------------------------------------------------------------------
def _clean_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "titan2").mkdir(parents=True, exist_ok=True)
    return app


def _no_kite(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("no Kite client may be built here")


def test_flock(tmp_path):
    """One titan2-bot at a time: a second lock attempt fails while the first is held, a second bot process exits
    73 at once, and the lock is free again after release."""
    path = tmp_path / "run" / runtime.LOCK_NAME
    first, second = runtime.InstanceLock(path), runtime.InstanceLock(path)
    assert first.acquire() and first.held
    try:
        assert not second.acquire() and not second.held
        code = runtime.run_bot(env={"TITAN2_VAR": str(tmp_path)}, clock=FakeClock(ist_datetime(S, "09:00")),
                               calendar=CAL, notifier=recorder(), kite_factory=_no_kite,
                               cwd=_clean_app(tmp_path), check_kite=lambda **_k: None, install_signals=False)
        assert code == runtime.EXIT_LOCKED
        env = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}
        # INVOCATION_ID stands for systemd: `titan2 bot` refuses to start without it (F-15); this test pins the lock,
        #   not how the bot was started
        env.update(TITAN2_VAR=str(tmp_path), TITAN2_LOG_DIR=str(tmp_path / "log"), PYTHONDONTWRITEBYTECODE="1",
                   INVOCATION_ID="0123456789abcdef0123456789abcdef")
        proc = subprocess.run([sys.executable, "-m", "titan2", "bot"], cwd=APP, env=env, capture_output=True,
                              text=True, timeout=180)
        assert proc.returncode == runtime.EXIT_LOCKED, proc.stdout + proc.stderr
        assert "holds" in proc.stdout + proc.stderr
        assert "holds" in (tmp_path / "log" / "bot.log").read_text(encoding="utf-8")
    finally:
        first.release()
    assert not first.held and second.acquire()
    second.release()


_SYSTEMD_KEYS = ("INVOCATION_ID", "NOTIFY_SOCKET")


def _fake_runtime(monkeypatch) -> list:
    """runtime.run_bot and setup_logging recorded instead of run (and the thread name runtime.main sets restored)."""
    calls: list = []
    monkeypatch.setattr(runtime, "run_bot", lambda **kw: calls.append(kw) or runtime.EXIT_OK)
    monkeypatch.setattr(runtime, "setup_logging", lambda *a, **k: calls.append("setup_logging"))
    monkeypatch.setattr(threading.current_thread(), "name", threading.current_thread().name)
    return calls


def test_bot_verb_refuses_outside_systemd(tmp_path, monkeypatch):
    """F-15: `titan2 bot` without systemd's INVOCATION_ID and NOTIFY_SOCKET (an operator through the CLI wrapper, whose
    sudoers rule allows every verb) refuses with exit 2 and names `sudo systemctl start titan2-bot` and
    --outside-systemd. The runtime is never reached: no instance lock, no log, no titan2.db, so the real titan2-bot
    is never locked out (exit 73, never restarted). Before, run_bot was called."""
    calls = _fake_runtime(monkeypatch)
    var, logs = tmp_path / "var", tmp_path / "log"
    out: list[str] = []
    env = {"TITAN2_VAR": str(var), "TITAN2_LOG_DIR": str(logs)}
    assert cli.main(["bot"], env=env, out=out.append) == cli.EXIT_FAILED
    for k in _SYSTEMD_KEYS:                                      # the process environment is checked the same way
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TITAN2_VAR", str(var))
    assert cli.main(["bot", "--safe"], out=out.append) == cli.EXIT_FAILED
    assert calls == []
    assert len(out) == 2 and "sudo systemctl start titan2-bot" in out[0] and "--outside-systemd" in out[0]
    assert "watchdog" in out[0] and "73" in out[0]
    assert not var.exists() and not logs.exists()                # no lock file, no log, no database


def test_bot_verb_outside_systemd_reads_the_env_file(tmp_path, monkeypatch):
    """F-15 (the review's proof test, with the flag): --outside-systemd runs the bot anyway, with /etc/titan2/titan2.env
    merged in as the unit's EnvironmentFile would (the Kite key and the Telegram keys), the process environment
    winning."""
    env_file = tmp_path / "titan2.env"
    env_file.write_text("ZERODHA_API_KEY=k\nZERODHA_USER_ID=AB1234\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n",
                        encoding="utf-8")
    monkeypatch.setattr(token_mod, "ENV_FILE", env_file)
    for k in ("ZERODHA_API_KEY", "ZERODHA_USER_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") + _SYSTEMD_KEYS:
        monkeypatch.delenv(k, raising=False)                     # what `sudo -u titan` leaves: no unit environment
    calls = _fake_runtime(monkeypatch)
    assert cli.main(["bot", "--outside-systemd"]) == runtime.EXIT_OK
    (kw,) = [c for c in calls if isinstance(c, dict)]
    assert kw["safe"] is False and kw["env"]["ZERODHA_API_KEY"] == "k" and kw["env"]["ZERODHA_USER_ID"] == "AB1234"
    assert (kw["env"]["TELEGRAM_BOT_TOKEN"], kw["env"]["TELEGRAM_CHAT_ID"]) == ("t", "1")
    calls.clear()
    assert cli.main(["bot", "--outside-systemd"], env={"ZERODHA_API_KEY": "mine"}) == runtime.EXIT_OK
    (kw,) = [c for c in calls if isinstance(c, dict)]
    assert kw["env"]["ZERODHA_API_KEY"] == "mine" and kw["env"]["TELEGRAM_CHAT_ID"] == "1"


def test_bot_verb_under_systemd_runs_as_before(monkeypatch):
    """Started by systemd (INVOCATION_ID, or the Type=notify NOTIFY_SOCKET), `titan2 bot` runs the runtime with
    exactly the environment it was given (the unit's EnvironmentFile already applied) and the --safe flag."""
    calls = _fake_runtime(monkeypatch)
    for key in _SYSTEMD_KEYS:
        calls.clear()
        env = {"TITAN2_VAR": "/var/lib/titan2", key: "1"}
        assert cli.main(["bot", "--safe"], env=env) == runtime.EXIT_OK
        (kw,) = [c for c in calls if isinstance(c, dict)]
        assert kw == {"safe": True, "env": env}


class RecordingAlerter:
    """The alerts.Alerter surface BotProcess uses, recording instead of sending (no thread, no Telegram)."""

    healthcheck = None

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def start(self) -> None:
        pass

    def stop(self, timeout: float | None = None) -> None:
        pass

    def submit(self, severity: str, code: str, text: str, dedup_key: str | None = None) -> bool:
        self.sent.append((severity, code, text, dedup_key))
        return True

    def dispatch(self, cmd: Alert) -> bool:
        return self.submit(cmd.severity, cmd.code, cmd.text, cmd.dedup_key)

    def fail(self, text: str) -> None:
        self.sent.append(("FAIL", "", text, None))

    def ping_now(self) -> None:
        pass

    def ping_ready(self) -> None:
        pass

    def unacked_critical(self) -> int:
        return 0

    def codes(self, code: str) -> list[tuple]:
        return [s for s in self.sent if s[1] == code]


def _seed_events(var: Path, *rows: tuple) -> None:
    """A previous run's events in titan2.db: (kind, wall, payload)."""
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        try:
            with st.tx():
                for kind, wall, payload in rows:
                    st.append_event(kind, wall=wall, mono=1.0, payload=payload)
        finally:
            st.close()


def _start(tmp_path: Path, var: Path, monkeypatch, when: datetime, kite_factory: Any = None) -> tuple[int, Any]:
    rec = RecordingAlerter()
    monkeypatch.setattr(runtime.alerts.Alerter, "from_env", lambda *a, **k: rec)
    settings_mod._reset_load_once()
    with store_mod.core_thread():
        code = runtime.run_bot(env={"TITAN2_VAR": str(var)}, clock=FakeClock(when), calendar=CAL, notifier=recorder(),
                               kite_factory=kite_factory or _no_kite, ticker_factory=_no_kite,
                               cwd=_clean_app(tmp_path), check_kite=lambda **_k: None, install_signals=False)
    return code, rec


def _events(var: Path, kind: str) -> list[dict]:
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        return st.events(kind=kind)
    finally:
        st.close()


def test_restart_after_an_unclean_exit_mid_session_alerts(tmp_path, monkeypatch):
    """F-16: a run that died without a clean exit (a watchdog SIGABRT, an OOM kill or SIGKILL) left its BOOT without
    the runtime's SHUTDOWN; the engine's own SHUTDOWN STOPPING does not count. The next start (here Saturday's IDLE
    run) sends one CRITICAL BOT_KILLED naming when the bot was last alive, inside Friday's session, and records a
    CRITICAL UNCLEAN_EXIT event. That run ends with SHUTDOWN IDLE, so a second start reports nothing."""
    var = tmp_path / "var"
    _seed_events(var, ("BOOT", ist_datetime(FRI, "09:10:00"), {"version": "x"}),
                 ("HEARTBEAT", ist_datetime(FRI, "11:42:05"), {}),
                 ("SHUTDOWN", ist_datetime(FRI, "11:42:06"), {"phase": "STOPPING", "reason": "SIGTERM"}))
    code, rec = _start(tmp_path, var, monkeypatch, ist_datetime(SAT, "10:00"))
    assert code == runtime.EXIT_OK
    (killed,) = rec.codes("BOT_KILLED")
    assert killed[0] == "CRITICAL" and killed[3] == "bot:killed"
    assert "2026-10-09 11:42:06" in killed[2] and "journalctl" in killed[2]
    (ev,) = _events(var, "UNCLEAN_EXIT")
    assert ev["severity"] == "CRITICAL" and ev["payload"]["in_session"] is True
    assert ev["payload"]["last_alive"].startswith("2026-10-09T11:42:06")
    code, rec = _start(tmp_path, var, monkeypatch, ist_datetime(SAT, "11:00"))
    assert code == runtime.EXIT_OK and rec.codes("BOT_KILLED") == [] and len(_events(var, "UNCLEAN_EXIT")) == 1


def test_no_restart_alert_after_a_clean_exit_or_outside_hours(tmp_path, monkeypatch):
    """A previous run that ended with the runtime's SHUTDOWN (EXITED) is not reported. One that died after the close
    (last alive 15:50) is recorded as a WARN UNCLEAN_EXIT event, with no Telegram alert."""
    clean = tmp_path / "clean"
    _seed_events(clean, ("BOOT", ist_datetime(FRI, "08:45:00"), {}), ("HEARTBEAT", ist_datetime(FRI, "12:00:00"), {}),
                 ("SHUTDOWN", ist_datetime(FRI, "15:40:00"), {"phase": "EXITED", "exit": 0}))
    code, rec = _start(tmp_path, clean, monkeypatch, ist_datetime(SAT, "10:00"))
    assert code == runtime.EXIT_OK and rec.codes("BOT_KILLED") == [] and _events(clean, "UNCLEAN_EXIT") == []
    late = tmp_path / "late"
    _seed_events(late, ("BOOT", ist_datetime(FRI, "15:41:00"), {}), ("HEARTBEAT", ist_datetime(FRI, "15:50:00"), {}))
    code, rec = _start(tmp_path, late, monkeypatch, ist_datetime(SAT, "10:00"))
    assert code == runtime.EXIT_OK and rec.codes("BOT_KILLED") == []
    (ev,) = _events(late, "UNCLEAN_EXIT")
    assert ev["severity"] == "WARN" and ev["payload"]["in_session"] is False


def test_crash_path_records_its_shutdown(tmp_path, monkeypatch):
    """The BOT_CRASHED path (an exception escaping the run) records SHUTDOWN phase CRASHED, so the next start does not
    report the same run again as BOT_KILLED."""
    var = tmp_path / "var"

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("a crash inside the run")

    code, rec = _start(tmp_path, var, monkeypatch, ist_datetime(S, "10:00"), kite_factory=boom)
    assert code == runtime.EXIT_EXCEPTION_STORM and [s[0] for s in rec.codes("BOT_CRASHED")] == ["CRITICAL"]
    last = _events(var, "SHUTDOWN")[-1]
    assert last["payload"]["phase"] == "CRASHED" and last["severity"] == "CRITICAL"
    code, rec = _start(tmp_path, var, monkeypatch, ist_datetime(SAT, "10:00"))
    assert code == runtime.EXIT_OK and rec.codes("BOT_KILLED") == [] and _events(var, "UNCLEAN_EXIT") == []


def test_unsafe_start_exit78(tmp_path):
    """A module in the working directory that shadows an import (v1's tree shadowing the release), with nothing
    open, or a patched Kite: exit 78 at once."""
    app = _clean_app(tmp_path)
    (app / "json.py").write_text("x = 1\n")
    common = dict(env={"TITAN2_VAR": str(tmp_path / "var")}, clock=FakeClock(ist_datetime(S, "09:00")),
                  calendar=CAL, notifier=recorder(), kite_factory=_no_kite, install_signals=False)
    with store_mod.core_thread():
        assert runtime.run_bot(cwd=app, check_kite=lambda **_k: None, **common) == runtime.EXIT_FATAL
        (app / "json.py").unlink()

        def patched(**_k: Any) -> None:
            raise RuntimeError("KiteConnect's order path is patched")

        assert runtime.run_bot(cwd=app, check_kite=patched, **common) == runtime.EXIT_FATAL
    assert not (tmp_path / "var" / "db").exists()                 # nothing was opened before the checks


def test_stray_research_script_never_blocks_a_restart(tmp_path):
    """An operator's _research.py next to the titan2 link cannot shadow anything the bot imports: the bot starts as
    usual (exit 78 is never restarted by systemd, which would leave open legs unmanaged)."""
    app = _clean_app(tmp_path)
    (app / "_analyze_today_remote.py").write_text("print('research')\n")
    settings_mod._reset_load_once()
    with store_mod.core_thread():
        code = runtime.run_bot(env={"TITAN2_VAR": str(tmp_path / "var")}, clock=FakeClock(ist_datetime(SAT, "10:00")),
                               calendar=CAL, notifier=recorder(), kite_factory=_no_kite, cwd=app,
                               check_kite=lambda **_k: None, install_signals=False)
    assert code == runtime.EXIT_OK


def test_shadowing_module_with_open_legs_runs_exits_only(tmp_path):
    """A shadowing module in the working directory while a structure is open: never exit 78 (systemd does not
    restart it and the leg would ride to the broker square-off). The bot runs in SAFE mode (HALT SAFE_FLAG: exits
    only) with a CRITICAL alert, and still runs recovery."""
    var = tmp_path / "var"
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        fri = ist_datetime(FRI, "14:00")
        intent = Intent("u17shadow001", "opening_drive", "1.x", "NSE:RELIANCE", Direction.UP, StructureType.LONG_CE,
                        StrikePref.ITM1, EXPIRY_RULE_NEAREST, 2900.0, Recheck(2880.0, 2930.0), 10.0, 5.0, fri, {})
        with st.tx():
            st.insert_intent(intent, FRI)
        positions.reserve(st, intent, book="PAPER:opening_drive", account=Account.PAPER, policy=Policy.FULL,
                          now_mono=5.0, now_wall=fri)
        st.close()
    token_mod.write_session(token_mod.new_session("abcdef123456", "AB1234", ist_datetime(SAT, "08:30")),
                            token_mod.session_path(var))
    app = _clean_app(tmp_path)
    (app / "json.py").write_text("x = 1\n")
    kite, notifier = FakeKite(), recorder()
    proc = runtime.BotProcess(env={"TITAN2_VAR": str(var), "ZERODHA_API_KEY": "testkey"},
                              clock=ScaledClock(ist_datetime(SAT, "10:00")), calendar=CAL, notifier=notifier,
                              kite_factory=lambda *_a, **_k: kite, ticker_factory=_no_kite, cwd=app,
                              check_kite=lambda **_k: None, install_signals=False)
    with store_mod.core_thread():
        code = proc.run()
    assert code == runtime.EXIT_OK and proc.safe
    assert "json.py" in proc.unsafe_cwd and "SAFE mode" in proc.unsafe_cwd
    assert proc.rt is not None and any(a.code == "INVARIANT" and a.dedup_key == "invariant:unsafe_cwd"
                                       and a.severity == "CRITICAL" for a in proc.rt.boot_alerts)
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        boot = st.events(kind="BOOT")[-1]
        halts = {h.kind for h in st.active_halts()}
        kinds = [e["kind"] for e in st.events()]
    finally:
        st.close()
    assert boot["payload"]["safe"] is True and boot["severity"] == "CRITICAL"
    assert "json.py" in boot["payload"]["unsafe_cwd"]
    assert "SAFE_FLAG" in halts and kinds.count("RECOVERY") == 3


def test_non_trading_day_exit0(tmp_path):
    """Outside a session (a weekend, a holiday) or after the session's last duty + 5 min (15:45 with the 15:40
    close), with nothing to recover: READY=1, STOPPING=1, exit 0; no Kite client, no feed, the lock released.
    At 15:44 the bot does start (here its Kite factory is reached, which this test forbids: exit 3)."""
    assert runtime.late_cutoff(CAL, S, DEFAULTS) == ist_datetime(S, "15:45")
    assert runtime.late_cutoff(CAL, SAT, DEFAULTS) is None
    app = _clean_app(tmp_path)
    with store_mod.core_thread():
        for tag, when in (("sat", ist_datetime(SAT, "10:00")), ("holiday", ist_datetime(HOLIDAY, "09:30")),
                          ("late", ist_datetime(S, "15:45:01"))):
            settings_mod._reset_load_once()
            var, notifier = tmp_path / tag, recorder()
            code = runtime.run_bot(env={"TITAN2_VAR": str(var)}, clock=FakeClock(when), calendar=CAL,
                                   notifier=notifier, kite_factory=_no_kite, ticker_factory=_no_kite, cwd=app,
                                   check_kite=lambda **_k: None, install_signals=False)
            assert code == runtime.EXIT_OK, tag
            assert states(notifier) == ["READY=1", "STOPPING=1"], tag
            lock = runtime.InstanceLock(var / "run" / runtime.LOCK_NAME)
            assert lock.acquire(), tag
            lock.release()
            st = store_mod.open_readonly(var / "db" / "titan2.db")
            try:
                kinds = [e["kind"] for e in st.events()]
                idle = st.events(kind="SHUTDOWN")[-1]["payload"]
            finally:
                st.close()
            assert kinds == ["BOOT", "SETTINGS_LOADED", "SHUTDOWN"] and idle["phase"] == "IDLE", tag
        settings_mod._reset_load_once()
        built: list = []

        def counting(*a: Any, **k: Any) -> Any:
            built.append(a)
            raise RuntimeError("stop here: the session is still on")

        code = runtime.run_bot(env={"TITAN2_VAR": str(tmp_path / "early")}, clock=FakeClock(ist_datetime(S, "15:44")),
                               calendar=CAL, notifier=recorder(), kite_factory=counting, cwd=app,
                               check_kite=lambda **_k: None, install_signals=False)
    assert built and code == runtime.EXIT_EXCEPTION_STORM
    assert not [t for t in threading.enumerate() if t.name == "alerts" and t.is_alive()]


@pytest.mark.parametrize("laptop", [False, True])
def test_a_start_after_todays_done_is_recovery_only(tmp_path, laptop):
    """Review round 2 (A4): a start between today's EOD DONE (15:40) and late_cutoff (15:45) ran a normal session:
    EngineState.load restored DONE with the other EOD steps, so the engine never sent Exit(0) again and the process
    ran on into the next day. A trading-day start after today's recorded DONE is recovery-only now; with nothing to
    recover it is READY=1, STOPPING=1, exit 0, and no Kite client is built. The laptop's run_day starts the same
    BotProcess (laptop=True). An earlier session's DONE changes nothing: that start runs (its Kite factory is reached,
    which this test forbids: exit 3)."""
    app = _clean_app(tmp_path)
    var = tmp_path / "var"
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        with st.tx():
            for day in (FRI, S):
                st.append_event("PHASE", wall=ist_datetime(day, "15:40:00"), mono=1.0, session=day,
                                payload={"phase": "DONE", "step": "DONE", "at": f"{day}T15:40:00+05:30"})
        st.close()
        notifier = recorder()
        code = runtime.run_bot(env={"TITAN2_VAR": str(var)}, clock=FakeClock(ist_datetime(S, "15:42")), calendar=CAL,
                               notifier=notifier, kite_factory=_no_kite, ticker_factory=_no_kite, cwd=app,
                               check_kite=lambda **_k: None, install_signals=False, laptop=laptop)
        assert code == runtime.EXIT_OK
        assert states(notifier) == ["READY=1", "STOPPING=1"]
        ro = store_mod.open_readonly(var / "db" / "titan2.db")
        try:
            idle = ro.events(kind="SHUTDOWN")[-1]["payload"]
        finally:
            ro.close()
        assert idle["phase"] == "IDLE" and "done" in idle["reason"]
        settings_mod._reset_load_once()
        mon = date(2026, 10, 19)                                 # the next Monday: only Friday's DONE is older
        code = runtime.run_bot(env={"TITAN2_VAR": str(var)}, clock=FakeClock(ist_datetime(mon, "15:42")),
                               calendar=CAL, notifier=recorder(), kite_factory=_no_kite, ticker_factory=_no_kite,
                               cwd=app, check_kite=lambda **_k: None, install_signals=False, laptop=laptop)
    assert code == runtime.EXIT_EXCEPTION_STORM


def _rehearsal_done(var: Path, *, boot_at: str | None = None) -> None:
    """The DONE row an old `titan2 laptop --date S` rehearsal left in the real state folder before S (stamped with
    S's 15:40 on its shifted clock; N5 gives rehearsals their own folders now but cleans nothing up), and optionally
    the real S's own earlier start (its BOOT row, written after the rehearsal's rows)."""
    st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
    with st.tx():
        st.append_event("PHASE", wall=ist_datetime(S, "15:40:00"), mono=1.0, session=S,
                        payload={"phase": "DONE", "step": "DONE", "at": f"{S}T15:40:00+05:30"})
        if boot_at is not None:
            st.append_event("BOOT", wall=ist_datetime(S, boot_at), mono=2.0, session=S, payload={"pid": 1})
    st.close()


@pytest.mark.parametrize("laptop", [False, True])
def test_a_rehearsals_done_row_never_makes_the_real_mornings_start_recovery_only(tmp_path, laptop, caplog):
    """The review of round 3 (A4 x N5), ported from the probe test_a4_stale_done_morning.py: one PHASE DONE row for
    S stamped S 15:40, which a rehearsal wrote before S, then S's 08:50 start. _done_today trusted any DONE row of the
    session, so the start became recovery-only and, with nothing to recover, exited 0 at 08:50 ("today's EOD timeline
    is done"): no session that day. A DONE row stamped later than now was not written by a run on this day: ignored
    with a WARN, and the start goes ahead (here it reaches the Kite factory, which this test forbids: exit 3)."""
    app = _clean_app(tmp_path)
    var = tmp_path / "var"
    with store_mod.core_thread():
        _rehearsal_done(var)
        with caplog.at_level("WARNING", logger="titan2.runtime"):
            code = runtime.run_bot(env={"TITAN2_VAR": str(var)}, clock=FakeClock(ist_datetime(S, "08:50")),
                                   calendar=CAL, notifier=recorder(), kite_factory=_no_kite,
                                   ticker_factory=_no_kite, cwd=app, check_kite=lambda **_k: None,
                                   install_signals=False, laptop=laptop)
        ro = store_mod.open_readonly(var / "db" / "titan2.db")
        try:
            last = ro.events(kind="SHUTDOWN")[-1]["payload"]
        finally:
            ro.close()
    assert code == runtime.EXIT_EXCEPTION_STORM and last.get("phase") != "IDLE", (code, last)
    assert any("ignoring the EOD DONE row stamped" in r.getMessage() for r in caplog.records)


def test_a_rehearsals_done_row_older_than_now_is_still_not_todays(tmp_path, caplog):
    """The same row met by a restart at S 15:42, after the real S had started at 08:50 and died before its own DONE:
    the row is older than now, but it was written before a start (08:50) that came before its stamp, so no run on S
    wrote it. Ignored: the restart is a normal late start (its truth check and daily files still to run), not a
    recovery-only exit. A DONE the real run wrote (the A4 test above) still makes it recovery-only."""
    app = _clean_app(tmp_path)
    var = tmp_path / "var"
    with store_mod.core_thread():
        _rehearsal_done(var, boot_at="08:50:00")
        with caplog.at_level("WARNING", logger="titan2.runtime"):
            code = runtime.run_bot(env={"TITAN2_VAR": str(var)}, clock=FakeClock(ist_datetime(S, "15:42")),
                                   calendar=CAL, notifier=recorder(), kite_factory=_no_kite,
                                   ticker_factory=_no_kite, cwd=app, check_kite=lambda **_k: None,
                                   install_signals=False)
    assert code == runtime.EXIT_EXCEPTION_STORM
    assert any("ignoring the EOD DONE row stamped" in r.getMessage() for r in caplog.records)


def test_a_recovery_only_run_waits_for_a_late_square_off_to_be_booked():
    """Review round 2 (A1): a recovery-only run on a trading day after BROKER_SQUAREOFF_AT may find the paper exchange
    squaring off at its first step; the run exits only once the engine's late recon has booked it (or its bound has
    passed), not after its two post-start recons, which both fall inside the start grace, nor at RECOVERY_ONLY_MAX_S."""
    clock = FakeClock(ist_datetime(S, "15:50"))
    rt = runtime.Runtime(store=None, ctx=None, clock=clock, recovery_only=True)
    rt.ready_mono = clock.mono()
    done = SimpleNamespace(extra_round_at=None, last_recon_mono=clock.mono() + 10.0)
    engine = SimpleNamespace(late_recon_holding=True, state=SimpleNamespace(broker={a: done for a in Account}))
    clock.advance(runtime.RECOVERY_ONLY_MAX_S + 5.0)
    assert not rt._recon_done(engine, clock.mono())            # still holding: past the 60 s, it waits
    engine.late_recon_holding = False
    assert rt._recon_done(engine, clock.mono())
    rt.ready_mono = clock.mono()
    assert not rt._recon_done(engine, clock.mono())            # not holding: the two recons decide, as before


def test_a_recovery_only_run_waits_for_the_engines_eod_steps():
    """Review round 2 follow-up (P2): a recovery-only run on a trading day after 15:45 runs today's EOD steps, and the
    engine's own DONE ends it. The run's exit (_recon_done) must not come first: it would stop the core loop before
    the truth check's answers and the day's files, as an early Exit(0) did. It waits while the engine says the steps
    from TRUTH on are still due (Engine.eod_finishing)."""
    clock = FakeClock(ist_datetime(S, "16:30"))
    rt = runtime.Runtime(store=None, ctx=None, clock=clock, recovery_only=True)
    rt.ready_mono = clock.mono()
    done = SimpleNamespace(extra_round_at=None, last_recon_mono=clock.mono() + 10.0)
    engine = SimpleNamespace(late_recon_holding=False, eod_finishing=True,
                             state=SimpleNamespace(broker={a: done for a in Account}))
    clock.advance(runtime.RECOVERY_ONLY_MAX_S + 5.0)
    assert not rt._recon_done(engine, clock.mono())            # past the 60 s and both recons: it still waits
    engine.eod_finishing = False
    assert rt._recon_done(engine, clock.mono())


@pytest.mark.parametrize("start,recovery_only", [("15:41:00", False), ("15:50:00", True)])
def test_a_start_after_the_eod_times_runs_the_truth_check_and_the_days_files_before_it_exits(tmp_path, start,
                                                                                            recovery_only):
    """Review round 2 follow-up (P2), on the real core loop: a start after 15:40 (also a recovery-only one; a late
    start's hold that ends after 15:40 likewise) has TRUTH, BAR_CHECK, EOD_TASKS and DONE due in its first Timer
    after READY. They all ran in that Timer, and core_loop stops on that batch's Exit(0): the FetchTradebook answers
    were never handled (no EOD_TRUTH_CHECK that day) and the files EOD_TASKS marks, which _publish writes at the next
    handle(), were never written (no daily report, no DAILY_REPORT alert, no exports). Each step now waits for what the
    earlier ones sent, and the run still ends with exit 0."""
    with store_mod.core_thread():
        clock = FakeClock(ist_datetime(S, start))
        rt, parts = real_runtime(tmp_path, clock, gate_live=False)
        rt.recovery_only = recovery_only
        rt.ctx.reports_dir, rt.ctx.exports_dir = tmp_path / "reports", tmp_path / "exports"
        rt.core_q.put(RecoveryStart())
        runtime.core_loop(rt)
        events = rt.store.events()
        rt.store.close()
        parts["px"].close()
    assert rt.exit_code == 0 and states(rt.notifier) == ["READY=1", "STOPPING=1"]
    truth = [e for e in events if e["kind"] == "EOD_TRUTH_CHECK"]
    assert sorted(e["payload"]["account"] for e in truth) == ["PAPER", "SHADOW"]
    assert all(e["payload"]["ok"] for e in truth), [e["payload"] for e in truth]
    steps = {e["payload"]["step"]: e["seq"] for e in events if e["kind"] == "PHASE" and e["payload"].get("step")}
    assert max(e["seq"] for e in truth) < steps["EOD_TASKS"] < steps["DONE"]
    assert "DAILY_REPORT" in [a.code for a in parts["alerter"].dispatched]
    assert (tmp_path / "reports" / f"{S}.md").is_file()
    assert (tmp_path / "exports" / "events" / f"{S}.jsonl.gz").is_file()


class ScaledClock(Clock):
    """Wall and monotonic time running `rate` times faster than real time (for the threaded end-to-end run)."""

    def __init__(self, start: datetime, rate: float = 10.0) -> None:
        self._t0, self._start, self._rate = time.monotonic(), start, rate

    def mono(self) -> float:
        return 1000.0 + (time.monotonic() - self._t0) * self._rate

    def now_wall(self) -> datetime:
        return self._start + timedelta(seconds=(time.monotonic() - self._t0) * self._rate)


class FakeKite:
    """The KiteConnect methods a recovery-only run reaches: an empty, healthy LIVE account."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def _note(self, name: str) -> None:
        with self.lock:
            self.calls.append(name)

    def profile(self) -> dict:
        self._note("profile")
        return {"user_id": "AB1234", "user_name": "test"}

    def orders(self) -> list:
        self._note("orders")
        return []

    def positions(self) -> dict:
        self._note("positions")
        return {"net": [], "day": []}

    def trades(self) -> list:
        self._note("trades")
        return []


def test_non_trading_day_recovers_open_structure_then_exits(tmp_path):
    """A Saturday start with a structure left non-terminal on Friday: the real lanes, paper-sim and engine run
    recovery (the stale RESERVED structure is voided), READY, two reconciles, then exit 0. No feed is built."""
    var = tmp_path / "var"
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        fri = ist_datetime(FRI, "14:00")
        intent = Intent("u17stale0001", "opening_drive", "1.x", "NSE:RELIANCE", Direction.UP, StructureType.LONG_CE,
                        StrikePref.ITM1, EXPIRY_RULE_NEAREST, 2900.0, Recheck(2880.0, 2930.0), 10.0, 5.0, fri, {})
        with st.tx():
            st.insert_intent(intent, FRI)
        reserved = positions.reserve(st, intent, book="PAPER:opening_drive", account=Account.PAPER,
                                     policy=Policy.FULL, now_mono=5.0, now_wall=fri)
        st.close()
    token_mod.write_session(token_mod.new_session("abcdef123456", "AB1234", ist_datetime(SAT, "08:30")),
                            token_mod.session_path(var))
    kite, notifier = FakeKite(), recorder()
    with store_mod.core_thread():
        code = runtime.run_bot(env={"TITAN2_VAR": str(var), "ZERODHA_API_KEY": "testkey"},
                               clock=ScaledClock(ist_datetime(SAT, "10:00")), calendar=CAL, notifier=notifier,
                               kite_factory=lambda *_a, **_k: kite, ticker_factory=_no_kite, cwd=_clean_app(tmp_path),
                               check_kite=lambda **_k: None, install_signals=False)
    assert code == runtime.EXIT_OK
    assert states(notifier) == ["READY=1", "STOPPING=1"]
    assert {"profile", "orders", "positions", "trades"} <= set(kite.calls)
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        row = st.query_one("SELECT state, void_reason FROM structures WHERE structure_id = ?",
                           (reserved.structure_id,))
        kinds = [e["kind"] for e in st.events()]
        halts = st.active_halts()
    finally:
        st.close()
    # never traded again: voided (the gateway's TTL timer or recovery's stale pass, whichever runs first)
    assert row["state"] == StructState.VOID.value and row["void_reason"].startswith(("STALE_SESSION", "TTL:"))
    assert kinds.count("RECOVERY") == 3 and kinds.count("READY") == 1 and kinds[-1] == "SHUTDOWN"
    assert not [h for h in halts if h.kind == "TOKEN"]                  # profile() as the session's user worked


class InlineReactor:
    """twisted's callFromThread, run at once (the feed's threading is market/feed.py's own test subject)."""

    def callFromThread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        fn(*args, **kwargs)

    def stop(self) -> None:
        pass


class FakeTicker:
    """KiteTicker stand-in: connect() calls on_connect at once; subscriptions are recorded."""

    def __init__(self) -> None:
        self.subscribed_tokens: dict = {}
        self.calls: list[tuple] = []
        self.websocket_thread = None

    def connect(self, threaded: bool = False) -> None:
        self.calls.append(("connect", threaded))
        self.on_connect(None, None)

    def subscribe(self, tokens: list) -> None:
        self.calls.append(("subscribe", tuple(tokens)))

    def set_mode(self, mode: str, tokens: list) -> None:
        self.calls.append(("set_mode", mode, tuple(tokens)))

    def unsubscribe(self, tokens: list) -> None:
        self.calls.append(("unsubscribe", tuple(tokens)))

    def close(self) -> None:
        self.calls.append(("close",))


class TradingKite(FakeKite):
    def instruments(self, exchange: str) -> list:
        self._note(f"instruments:{exchange}")
        raise OSError("instruments dump unavailable in this test")


def test_trading_day_wiring_end_to_end(tmp_path, monkeypatch):
    """A Monday 08:50 start through the real _build: lanes, paper-sim, prep worker, alerts, WS feed and signals
    threads. READY, then the feed connects; prep runs on its own thread (the dump fails: the engine halts entries
    with NO_INSTRUMENTS, nothing else breaks); a SIGTERM after READY ends the run with exit 0 and every thread stops."""
    var = tmp_path / "var"
    token_mod.write_session(token_mod.new_session("abcdef123456", "AB1234", ist_datetime(S, "08:30")),
                            token_mod.session_path(var))
    kite, ticker, notifier = TradingKite(), FakeTicker(), recorder()
    ran: list[str] = []

    def no_selfcheck(phase: str, ctx: Any) -> Any:          # the real phase runs host probes and subprocesses
        ran.append(phase)
        raise RuntimeError("self-check not run in this test")

    monkeypatch.setattr(runtime.selfcheck, "run_phase", no_selfcheck)
    ban_text = f"Securities in Ban For Trade Date {S:%d-%b-%Y}:\nNIL\n".upper()
    seen: dict = {}

    def stop_after_ready(rt: runtime.Runtime) -> None:
        def watch() -> None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (rt.ready and rt.ctx.prep.get("BAN_LIST") is not None):
                time.sleep(0.02)
            seen.update(ready=rt.ready, feed_started=rt.feed_started, signals=rt.signals.degraded() is False,
                        prep=dict(rt.ctx.prep), quote_client=rt.reads_lane.quote_data is not rt.reads_lane.data,
                        live=(rt.order_lane.broker.orders_allowed, rt.ctx.live_orders, rt.ctx.stop_flatten_by))
            rt.stop.request("SIGTERM")

        threading.Thread(target=watch, name="u17-stopper", daemon=True).start()

    with store_mod.core_thread():
        code = runtime.run_bot(env={"TITAN2_VAR": str(var), "ZERODHA_API_KEY": "testkey"},
                               clock=ScaledClock(ist_datetime(S, "08:50"), rate=4.0), calendar=CAL, notifier=notifier,
                               kite_factory=lambda *_a, **_k: kite, ticker_factory=lambda *_a, **_k: ticker,
                               reactor=InlineReactor(), ban_fetch=lambda: ban_text, cwd=_clean_app(tmp_path),
                               check_kite=lambda **_k: None, install_signals=False, on_runtime=stop_after_ready)
    assert code == runtime.EXIT_OK
    assert seen["ready"] and seen["feed_started"] and seen["signals"]
    assert seen["quote_client"]           # lane-reads-quotes has its own Kite client (no shared requests session)
    assert seen["live"] == (True, True, None)    # EC2: LIVE exits and covers go out, LIVE tags import (laptop: none)
    assert seen["prep"].get("INSTRUMENTS") is False and seen["prep"].get("BAN_LIST") is True
    assert states(notifier) == ["READY=1", "STOPPING=1"]
    assert ("connect", True) in ticker.calls and ("close",) in ticker.calls
    assert "instruments:NFO" in kite.calls
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        kinds = [e["kind"] for e in st.events()]
        halts = {h.kind for h in st.active_halts()}
        feed = [e["payload"] for e in st.events(kind="FEED_STATE")]
    finally:
        st.close()
    assert kinds[:2] == ["BOOT", "SETTINGS_LOADED"] and "READY" in kinds and kinds[-1] == "SHUTDOWN"
    assert "NO_INSTRUMENTS" in halts and "TOKEN" not in halts
    assert feed and feed[0]["connected"] is True
    names = {"signals", "prep", "lane-live-orders", "lane-reads", "lane-reads-quotes", "paper-sim", "alerts"}
    assert not [t.name for t in threading.enumerate() if t.name in names and t.is_alive()]


# ---------------------------------------------------------------------------------------------------------------
# The signals thread and prep
# ---------------------------------------------------------------------------------------------------------------
class Runner:
    """A StrategyRunner stand-in that records its calls and returns scripted RunResults."""

    def __init__(self, name: str, results: dict | None = None) -> None:
        self.name = name
        self.strategy = SimpleNamespace()
        self.calls: list[tuple] = []
        self.results = results or {}

    def _call(self, hook: str, arg: Any) -> RunResult:
        self.calls.append((hook, arg))
        return self.results.get(hook, RunResult())

    def on_tick(self, t: Any, mv: Any) -> RunResult:
        return self._call("on_tick", t)

    def on_bar_close(self, b: Any, mv: Any) -> RunResult:
        return self._call("on_bar_close", b)

    def on_timer(self, now: Any, mv: Any) -> RunResult:
        return self._call("on_timer", now)

    def on_feed_reset(self) -> RunResult:
        return self._call("on_feed_reset", None)


def _tick(clock: FakeClock, ltp: float, vol: int) -> UnderlyingTick:
    return UnderlyingTick("NSE:RELIANCE", 738561, ltp, vol, 2900.0, 2880.0, clock.now_wall(), clock.mono(),
                          clock.now_wall())


def test_signals_host_bars_gating_and_failures():
    clock = FakeClock(ist_datetime(S, "09:14:50"))
    sq, posted, alerted = SignalQueue(), [], []
    intent = Intent("u17intent001", "a", "1.x", "NSE:RELIANCE", Direction.UP, StructureType.LONG_CE,
                    StrikePref.ATM, EXPIRY_RULE_NEAREST, 2900.0, Recheck(2890.0, 2910.0), 10.0, 0.0,
                    clock.now_wall(), {})
    a = Runner("a", {"on_tick": RunResult(intents=(intent,))})
    b = Runner("b", {"on_timer": RunResult(error="b.on_timer raised ValueError('x')")})
    off = Runner("off")
    modes = {"a": "PAPER", "b": "SHADOW", "off": "OFF"}
    host = runtime.SignalsHost(signal_q=sq, runners={"a": a, "b": b, "off": off}, quotes=QuoteCache(mono=clock.mono),
                               clock=clock, calendar=CAL, settings=DEFAULTS, post=posted.append,
                               alert=lambda *x: alerted.append(x), prep_state=runtime.PrepState(S, DEFAULTS),
                               feed_health=lambda: FeedHealth(True, 0.0, clock.mono(), 0, False), mode_of=modes.get)
    def pump() -> None:
        """A due timer (it first takes every item queued up to its instant), then the rest (the clock stands still)."""
        for _ in range(4):
            host.step(0.0)

    def hooks(r: Runner, hook: str) -> list:
        return [arg for h, arg in r.calls if h == hook]

    assert not host.degraded()                                       # not started: nothing to report
    sq.put_link(FeedLink(True, False, clock.now_wall(), clock.mono()))
    pump()
    clock.advance_to("09:15:01")
    sq.put_ticks((_tick(clock, 2900.0, 1000),))
    pump()
    assert host.last_ticks["NSE:RELIANCE"].ltp == 2900.0
    assert not any(r.calls for r in (a, b, off))                     # bars are built, strategies not yet enabled
    host.enable()                                                    # StartSignals
    clock.advance_to("09:19:59")
    t2 = _tick(clock, 2950.0, 5000)
    sq.put_ticks((t2,))
    pump()
    assert hooks(a, "on_tick") == [t2] and hooks(b, "on_tick") == [t2] and not off.calls     # OFF is never run
    assert hooks(a, "on_timer") and posted == [IntentReceived(intent)]
    assert alerted and alerted[0][:2] == ("CRITICAL", "STRATEGY_DISABLED") and "b is disabled" in alerted[0][2]
    clock.advance_to("09:20:00.300")
    pump()                                                           # timer: the 09:15 bar closes
    (closed,) = [p for p in posted if isinstance(p, BarsClosed)]
    bar = closed.bars[0]
    assert (bar.symbol, bar.start, bar.open, bar.high, bar.close, bar.volume, bar.valid) == (
        "NSE:RELIANCE", ist_datetime(S, "09:15"), 2900.0, 2950.0, 2950.0, 5000, True)
    assert hooks(a, "on_bar_close") == [bar] and hooks(b, "on_bar_close") == [bar]
    assert host.bars.bars("NSE:RELIANCE", 5) == (bar,)
    sq.put_link(FeedLink(True, True, clock.now_wall(), clock.mono()))
    pump()
    assert hooks(off, "on_feed_reset") == [None] and hooks(a, "on_feed_reset") == [None]     # resets reach all
    prev = [Bar("NSE:RELIANCE", ist_datetime(FRI, "15:35"), 1.0, 2.0, 0.5, 1.5, 10, True, "HIST")]
    host.set_prev_session("NSE:RELIANCE", prev)
    pump()
    assert host.bars.prev_session_bars("NSE:RELIANCE", 20) == tuple(prev)
    assert host.feed_ok()


class _Universe:
    def universe(self, kind: str) -> frozenset:
        return frozenset({"NSE:RELIANCE"})


def _opening_host() -> tuple[FakeClock, SignalQueue, list, runtime.SignalsHost]:
    """opening_drive (PAPER) on NSE:RELIANCE, enabled, with Friday's last 20 bars at 1,000 shares each."""
    clock = FakeClock(ist_datetime(S, "09:14:50"))
    sq, posted = SignalQueue(), []
    host = runtime.SignalsHost(signal_q=sq, runners={"opening_drive": StrategyRunner(OpeningDrive("1.test"))},
                               quotes=QuoteCache(mono=clock.mono), clock=clock, calendar=CAL, settings=DEFAULTS,
                               post=posted.append, alert=lambda *x: None, prep_state=_Universe(),
                               feed_health=lambda: FeedHealth(True, 0.0, clock.mono(), 0, False),
                               mode_of=lambda name: "PAPER")
    sq.put_link(FeedLink(True, False, clock.now_wall(), clock.mono()))
    for _ in range(3):
        host.step(0.0)
    last = ist_datetime(FRI, "15:35")
    host.set_prev_session("NSE:RELIANCE", [Bar("NSE:RELIANCE", last - timedelta(minutes=5 * (19 - k)), 100.0, 101.0,
                                               99.0, 100.0, 1000, True, "HIST") for k in range(20)])
    host.enable()
    clock.advance_to("09:15:01")
    sq.put_ticks((UnderlyingTick("NSE:RELIANCE", 738561, 100.0, 4000, 100.0, 99.0, clock.now_wall(), clock.mono(),
                                 clock.now_wall()),))
    return clock, sq, posted, host


def _put(clock: FakeClock, sq: SignalQueue, at: str, ltp: float, vol: int) -> None:
    clock.advance_to(at)
    sq.put_ticks((UnderlyingTick("NSE:RELIANCE", 738561, ltp, vol, 100.0, 99.0, clock.now_wall(), clock.mono(),
                                 clock.now_wall()),))


def test_signals_timer_takes_queued_ticks_first():
    """The trade that ends the 09:15 bar (received 09:19:59.990) is still in signal_q when the 09:20:00.050 timer is
    due: the bar opening_drive evaluates must include it (close = the last LTP before 09:20, §10.3), and it must not
    arrive afterwards as a LATE_TICK that invalidates a bar the strategy already acted on."""
    clock, sq, posted, host = _opening_host()
    _put(clock, sq, "09:19:59.500", 101.0, 4500)                   # +1.0 %: no signal yet
    for _ in range(4):
        host.step(0.0)
    _put(clock, sq, "09:19:59.990", 103.0, 9000)                   # close +3.0 %, volume 9,000 >= 3 x 1,000
    clock.advance_to("09:20:00.050")
    for _ in range(4):
        host.step(0.0)
    bars = [b for ev in posted if isinstance(ev, BarsClosed) for b in ev.bars if b.symbol == "NSE:RELIANCE"]
    assert [(b.start, b.close, b.valid) for b in bars] == [(ist_datetime(S, "09:15"), 103.0, True)]
    assert [ev.intent.underlying for ev in posted if isinstance(ev, IntentReceived)] == ["NSE:RELIANCE"]

    clock, sq, posted, host = _opening_host()                      # the +3 % bar, then one more trade inside it
    _put(clock, sq, "09:19:59.500", 103.0, 9000)
    for _ in range(4):
        host.step(0.0)
    _put(clock, sq, "09:19:59.990", 103.1, 9100)
    clock.advance_to("09:20:00.050")
    for _ in range(4):
        host.step(0.0)
    bars = [b for ev in posted if isinstance(ev, BarsClosed) for b in ev.bars if b.symbol == "NSE:RELIANCE"]
    assert [(b.close, b.volume, b.valid) for b in bars] == [(103.1, 9100, True)]   # never re-emitted invalid
    assert len([ev for ev in posted if isinstance(ev, IntentReceived)]) == 1

    # an item received after the timer's instant (kite-ws enqueued it during the drain) waits for that timer
    clock, sq, posted, host = _opening_host()
    _put(clock, sq, "09:19:59.500", 103.0, 9000)
    for _ in range(4):
        host.step(0.0)
    clock.advance_to("09:20:00.050")
    later = timedelta(milliseconds=10)
    sq.put_ticks((UnderlyingTick("NSE:RELIANCE", 738561, 103.5, 9500, 100.0, 99.0, clock.now_wall() + later,
                                 clock.mono() + 0.010, clock.now_wall() + later),))
    host.step(0.0)                                                 # the due timer: the later tick is held back
    bars = [b for ev in posted if isinstance(ev, BarsClosed) for b in ev.bars if b.symbol == "NSE:RELIANCE"]
    assert [(b.close, b.valid) for b in bars] == [(103.0, True)] and host._held is not None
    host.step(0.0)                                                 # then it is handled, in the 09:20 bar
    assert host._held is None and host.last_ticks["NSE:RELIANCE"].ltp == 103.5
    assert [b.valid for ev in posted if isinstance(ev, BarsClosed) for b in ev.bars] == [True]


def _instruments() -> InstrumentStore:
    unders = [Underlying("NSE:NIFTY 50", 256265, "NIFTY", "INDEX"), Underlying("NSE:NIFTY BANK", 260105, "BANKNIFTY",
              "INDEX"), Underlying("NSE:RELIANCE", 738561, "RELIANCE", "STOCK"),
              Underlying("NSE:INFY", 408065, "INFY", "STOCK")]
    contracts, tok = [], 90_000
    for name, und, k, lot in (("NIFTY", "NSE:NIFTY 50", 25000.0, 65), ("BANKNIFTY", "NSE:NIFTY BANK", 56000.0, 30),
                              ("RELIANCE", "NSE:RELIANCE", 2900.0, 250), ("INFY", "NSE:INFY", 1500.0, 400)):
        for e in (date(2026, 10, 27), date(2026, 11, 24)):
            for ot in ("CE", "PE"):
                tok += 1
                ts = f"{name}{e:%y%m%d}{k:g}{ot}"
                contracts.append(Instrument(f"NFO:{ts}", tok, ts, "NFO", name, und, ot, k, e, lot, 0.05))
    return InstrumentStore(S, CAL, contracts, unders, min_stocks=1)


class HistData:
    def __init__(self, fail: set[str]) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def historical(self, symbol: str, token: int, frm: datetime, to: datetime, interval: str) -> tuple:
        self.calls.append(symbol)
        if symbol in self.fail:
            raise OSError(f"no history for {symbol}")
        start = frm
        out = []
        while start < to:
            out.append(Bar(symbol, start, 100.0, 101.0, 99.0, 100.5, 1000, True, "HIST"))
            start += timedelta(minutes=5)
        return tuple(out)


def test_prep_prev_bars_cached_in_db(tmp_path):
    """PREV_BARS: every stock's previous-session HIST bars (fetched, persisted through BarsClosed, handed to the
    signals thread); a failure keeps ok False (the engine retries); a restart reuses what titan2.db holds."""
    state = runtime.PrepState(S, DEFAULTS)
    posted, given = [], {}
    data = HistData(fail={"NSE:INFY"})
    prepper = runtime.Prepper(state=state, data=data, var_dir=tmp_path, calendar=CAL, settings=DEFAULTS,
                              post=posted.append, on_prev_bars=lambda s, b: given.__setitem__(s, b))
    assert prepper.prev_bars() == (False, "today's instruments are not loaded")
    state.set_instruments(_instruments())
    ok, detail = prepper.prev_bars()
    assert not ok and "1/2 stocks" in detail and "1 fetched, 1 failed" in detail
    (persisted,) = posted
    assert isinstance(persisted, BarsClosed) and len(persisted.bars) == 77          # 09:15-15:40 of Friday
    assert persisted.bars[0].start == ist_datetime(FRI, "09:15") and persisted.bars[-1].start == ist_datetime(FRI,
                                                                                                             "15:35")
    assert len(given["NSE:RELIANCE"]) == 77 and state.coverage() == (1, 2)
    with store_mod.core_thread():                                   # the core persists the BarsClosed event
        st = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        with st.tx():
            for b in persisted.bars:
                st.put_bar(b)
        st.close()
    data2 = HistData(fail={"NSE:INFY", "NSE:RELIANCE"})
    prepper2 = runtime.Prepper(state=state, data=data2, var_dir=tmp_path, calendar=CAL, settings=DEFAULTS,
                               post=posted.append, on_prev_bars=lambda s, b: given.__setitem__(s, b))
    ok, detail = prepper2.prev_bars()
    assert data2.calls == ["NSE:INFY"] and "1/2 stocks" in detail and not ok      # RELIANCE came from titan2.db
    worker = runtime.PrepWorker(prepper2.handlers(), posted.append)
    (res,) = [worker.execute(Prep("INSTRUMENTS"))]
    assert isinstance(res, PrepResult) and not res.ok and res.detail              # no data.instruments: a failure
    assert not worker.execute(Prep("TOKEN")).ok                                   # no handler: refused, not silent
    assert state.universe("STOCKS") == frozenset()                                # no ban list yet: no stock eligible
    assert state.universe("INDICES") == frozenset({"NSE:NIFTY 50", "NSE:NIFTY BANK"})
    (tmp_path / "config" / "ban_override").mkdir(parents=True)
    (tmp_path / "config" / "ban_override" / f"{S}.csv").write_text("NIL\n", encoding="utf-8")
    assert prepper2.ban_list() == (True, "OVERRIDE: 0 banned (manual override)")
    assert state.universe("STOCKS") == frozenset({"NSE:RELIANCE", "NSE:INFY"})
    assert state.ban_symbols() == frozenset()


# ---------------------------------------------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------------------------------------------
def test_load_handlers_and_plugin_rules(tmp_path, monkeypatch):
    h = plugins.load_handlers()
    assert set(h) == {StructureType.LONG_CE, StructureType.LONG_PE}
    ce, pe = h[StructureType.LONG_CE], h[StructureType.LONG_PE]
    assert isinstance(ce, SingleLegHandler) and ce is pe
    pkg = tmp_path / "u17plug"
    pkg.mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    bodies = {
        "u17plug.ok": "def register(handlers, deps):\n    assert deps['x'] == 1\n",
        "u17plug.raises": "def register(handlers, deps):\n    raise ValueError('broken plugin')\n",
        "u17plug.replaces": ("from titan2.core.single_leg import SingleLegHandler\n"
                             "from titan2.model import StructureType\n"
                             "def register(handlers, deps):\n"
                             "    handlers[StructureType.LONG_CE] = SingleLegHandler()\n"),
        "u17plug.badkey": "def register(handlers, deps):\n    handlers['IRON_CONDOR'] = object()\n",
        "u17plug.mutates_deps": "def register(handlers, deps):\n    deps['y'] = 2\n",
    }
    (pkg / "__init__.py").write_text("")
    for name, body in bodies.items():
        (pkg / f"{name.split('.')[1]}.py").write_text(body)
    ok = plugins.load_handlers({"x": 1}, plugin="u17plug.ok")
    assert set(ok) == set(h) and all(isinstance(v, SingleLegHandler) for v in ok.values())
    for bad in ("u17plug.raises", "u17plug.replaces", "u17plug.badkey", "u17plug.mutates_deps"):
        with pytest.raises(plugins.PluginError):
            plugins.load_handlers({"x": 1}, plugin=bad)
    assert set(plugins.load_handlers(plugin="u17plug_absent")) == set(h)         # no plugin: phase 1 only


# ---------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------
class FakeBot(threading.Thread):
    """The bot's side of the CLI seam: operator.read_new on commands_cli.db, acks written to titan2.db."""

    def __init__(self, var: Path, decide: Callable[[Any], list[tuple[str, str]]]) -> None:
        super().__init__(name="fake-bot", daemon=True)
        self.var, self.decide = var, decide
        self.halt = threading.Event()
        self.rows: list = []
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            with store_mod.core_thread():
                st = Store(self.var / "db" / "titan2.db")
                last = st.last_command_id("CLI")
                while not self.halt.is_set():
                    for row in operator.read_new(self.var / "db" / "commands_cli.db", "CLI", last):
                        self.rows.append(row)
                        for status, result in self.decide(row):
                            now = datetime.now(IST)
                            with st.tx():
                                st.put_command_ack("CLI", row.cmd_id, row.kind, status, now, result=result,
                                                   done_wall=None if status == "ACCEPTED" else now)
                            time.sleep(0.05)
                        last = row.cmd_id
                    time.sleep(0.02)
                st.close()
        except BaseException as exc:          # surfaced by the test, never swallowed
            self.error = exc


def test_cli_ack(tmp_path):
    """Mutating verbs insert into commands_cli.db and wait for the bot's ack in titan2.db: DONE -> 0, REJECTED -> 2,
    ACCEPTED followed by DONE -> 0; a wrong --confirm is refused locally (no row); no bot -> 2 with what happens
    next; the rows are what the bot's operator.read_new expects; a recreated file never reuses an acked id."""
    var = tmp_path / "var"
    Store(var / "db" / "titan2.db").close()
    env = {"TITAN2_VAR": str(var), "SUDO_USER": "opsuser"}
    replies = {"PAUSE": [("DONE", "paused")], "ACK_HALT": [("REJECTED", "no active halt 7")],
               "FLATTEN_ALL": [("ACCEPTED", "flattening every bot leg"), ("DONE", "verified flat")],
               "SET_MODE": [("DONE", "opening_drive: SHADOW takes effect at the next session")],
               "RUN_RECON": [("DONE", "recon requested for every account")]}
    bot = FakeBot(var, lambda row: replies[row.kind])
    bot.start()
    lines: list[str] = []

    def run(*argv: str, wait_s: float = 10.0) -> int:
        rc = cli.main(list(argv), env=env, out=lines.append, wait_s=wait_s, poll_s=0.02)
        assert bot.error is None, repr(bot.error)
        return rc

    try:
        assert run("pause") == cli.EXIT_OK, lines
        assert lines[-1] == "PAUSE #1: DONE: paused"
        assert run("ack-halt", "7", "--note", "checked the order book") == cli.EXIT_FAILED
        assert lines[-1] == "ACK_HALT #2: REJECTED: no active halt 7"
        assert run("flatten-all", "--confirm", "yes") == cli.EXIT_FAILED
        assert lines[-1] == "refused: flatten-all needs --confirm FLATTEN"
        assert run("flatten-all", "--confirm", "FLATTEN") == cli.EXIT_OK
        assert lines[-1] == "FLATTEN_ALL #3: DONE: verified flat"
        assert run("mode", "opening_drive", "SHADOW") == cli.EXIT_OK
    finally:
        bot.halt.set()
        bot.join(5)
    assert bot.error is None
    rows = {r.kind: r for r in bot.rows}
    assert [r.cmd_id for r in bot.rows] == [1, 2, 3, 4]
    assert rows["ACK_HALT"].args == {"halt_id": 7, "note": "checked the order book"}
    assert rows["FLATTEN_ALL"].confirm_text == "FLATTEN" and rows["PAUSE"].args == {}
    assert rows["SET_MODE"].args == {"strategy": "opening_drive", "mode": "SHADOW"}
    assert {r.requested_by for r in bot.rows} == {"opsuser"} and len({r.nonce for r in bot.rows}) == 4
    assert all(r.created_wall is not None and abs((datetime.now(IST) - r.created_wall).total_seconds()) < 60
               for r in bot.rows)

    # the bot is down: not acknowledged, exit 2, and the operator learns what happens to the command
    assert run("resume", wait_s=0.2) == cli.EXIT_FAILED
    assert "NOT ACKNOWLEDGED" in lines[-1] and "expires 300 s" in lines[-1]
    assert run("kill", "--confirm", "KILL", wait_s=0.2) == cli.EXIT_FAILED
    assert "stays queued and runs when the bot next starts" in lines[-1]

    # commands_cli.db recreated: ids continue above the highest one the bot acked (no CommandsReset, no lost row)
    (var / "db" / "commands_cli.db").unlink()
    bot = FakeBot(var, lambda row: replies[row.kind])
    bot.start()
    try:
        assert run("recon") == cli.EXIT_OK
    finally:
        bot.halt.set()
        bot.join(5)
    assert bot.error is None and [r.cmd_id for r in bot.rows] == [5] and bot.rows[0].kind == "RUN_RECON"
    assert set(cli.CLI_KINDS) == operator.KINDS["CLI"]


def test_cli_status_settings_and_reports(tmp_path):
    var = tmp_path / "var"
    env = {"TITAN2_VAR": str(var)}
    lines: list[str] = []
    now = ist_datetime(S, "10:00")

    def run(*argv: str) -> int:
        return cli.main(list(argv), env=env, out=lines.append, now=lambda: now)

    # before the bot ever ran: status works (the deploy preflight's first run) and says so
    assert run("status", "--json") == cli.EXIT_OK
    info = json.loads(lines[-1])
    assert info["db"]["exists"] is False and info["bot_active"] is False and info["trading_day"] is True
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        intent = Intent("u17status001", "opening_drive", "1.x", "NSE:RELIANCE", Direction.UP, StructureType.LONG_CE,
                        StrikePref.ITM1, EXPIRY_RULE_NEAREST, 2900.0, Recheck(2880.0, 2930.0), 10.0, 5.0, now, {})
        with st.tx():
            st.insert_intent(intent, S)
            st.add_halt("GLOBAL", "PAUSE", "operator PAUSE", "RESUME", now, session=S, operator=True)
        positions.reserve(st, intent, book="PAPER:opening_drive", account=Account.PAPER, policy=Policy.FULL,
                          now_mono=5.0, now_wall=now)
        st.close()
    (var / "pub").mkdir(parents=True)
    (var / "pub" / "status.json").write_text(json.dumps({"phase": "TRADING", "allow_live": False}), encoding="utf-8")
    assert run("status", "--json") == cli.EXIT_OK
    info = json.loads(lines[-1])
    assert info["bot_active"] is True and info["status"]["phase"] == "TRADING"
    db = info["db"]
    assert (db["nonterminal_structures"], db["open_legs"], db["nonterminal_orders"]) == (1, 0, 0)
    assert [h["kind"] for h in info["db"]["active_halts"]] == ["PAUSE"]
    assert run("status") == cli.EXIT_OK and "halt #1 PAUSE [GLOBAL]" in lines[-1] and "bot: running" in lines[-1]

    # settings: show, set (validated, tighten-only), diff, a loosening refused
    assert run("settings", "set", "MAX_ENTRIES_PER_DAY", "1") == cli.EXIT_OK
    assert "MAX_ENTRIES_PER_DAY: 3 -> 1" in lines[-1]
    assert run("settings", "set", "MAX_ENTRIES_PER_DAY", "9") == cli.EXIT_FAILED and lines[-1].startswith("refused")
    assert run("settings", "diff") == cli.EXIT_OK and lines[-1] == "MAX_ENTRIES_PER_DAY: default 3 -> 1"
    assert run("settings", "show") == cli.EXIT_OK and json.loads(lines[-1])["MAX_ENTRIES_PER_DAY"] == 1

    assert run("is-trading-day", "--date", "2026-10-20") == cli.EXIT_NO          # the real calendar: a holiday
    assert run("is-trading-day", "--date", "2026-10-12") == cli.EXIT_OK and "09:15-15:40" in lines[-1]
    assert run("is-trading-day", "--date", "12-10-2026") == cli.EXIT_FAILED
    assert run("trades") == cli.EXIT_OK and lines[-1] == f"no trades on {S}"
    assert run("selftest") == cli.EXIT_OK and "not required" in lines[-1]
    assert run("g1") == cli.EXIT_OK and lines[-1] == "no strategies registered yet"
    assert run("report") == cli.EXIT_OK and str(S) in lines[-1]

    # backup: online copies of every database present, each checked; settings copied; never the env or session
    (var / "session").mkdir()
    (var / "session" / "kite_session.json").write_text("{}")
    assert run("backup", "--to", str(tmp_path / "bk"), "--gzip") == cli.EXIT_OK
    got = sorted(p.name for p in (tmp_path / "bk").iterdir())
    assert got == ["settings.json", "titan2.db.gz"]
    with gzip.open(tmp_path / "bk" / "titan2.db.gz", "rb") as f:
        (tmp_path / "restored.db").write_bytes(f.read())
    con = sqlite3.connect(tmp_path / "restored.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM structures").fetchone()[0] == 1
    finally:
        con.close()
    assert run("backup", "--to", str(tmp_path / "bk")) == cli.EXIT_FAILED          # never overwrites a backup
    assert run("backup", "--to", str(tmp_path / "plain")) == cli.EXIT_OK
    assert (tmp_path / "plain" / "titan2.db").is_file()                            # --db-copy for the deploy check


def test_cli_misc_verbs(tmp_path, monkeypatch):
    env = {"TITAN2_VAR": str(tmp_path / "var")}
    lines: list[str] = []
    sent: list[tuple] = []
    monkeypatch.setattr(cli.alerts, "critical_now", lambda text, **kw: sent.append((text, kw["env"])) or 0)
    assert cli.main(["alert", "--critical", "unit titan2-bot failed"], env=env, out=lines.append) == 0
    assert sent == [("unit titan2-bot failed", env)]
    ran: list[tuple] = []
    monkeypatch.setattr(cli.selfcheck, "cli", lambda phase, **kw: ran.append((phase, kw["db_copy"])) or 2)
    assert cli.main(["selfcheck", "--phase", "deploy", "--db-copy", "x.db"], env=env, out=lines.append) == 2
    assert ran == [("deploy", "x.db")]
    monkeypatch.setattr(cli.token_mod, "run", lambda **kw: ran.append(("token", kw["request_token"])) or 0)
    assert cli.main(["token", "--request-token", "abc123xyz"], env=env, out=lines.append) == 0
    assert ran[-1] == ("token", "abc123xyz")
    with pytest.raises(SystemExit) as exc:                                         # usage errors are exit 2
        cli.main(["live", "opening_drive", "--confirm", "abc"], env=env, out=lines.append)
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["mode", "opening_drive", "LIVE"], env=env, out=lines.append)    # LIVE only via `live`
    assert cli.main(["g1", "nosuch"], env=env, out=lines.append) == cli.EXIT_FAILED   # no database yet
    assert parse_wall(cli.status_info(tmp_path / "var", ist_datetime(S, "10:00"), CAL)["now"]).date() == S
