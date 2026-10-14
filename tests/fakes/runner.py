"""ScenarioRunner: the real titan2 core wired to fakes and stepped deterministically (spec §16.1).

Real components: Engine (+ gateway, SingleLegHandler, exit loop, recon, risk, ledger, g1), Store on a temp titan2.db,
PaperExchange (FaultyPaperExchange: the paper side of the FaultInjector) inside an inline PaperSim, KiteBroker and
KiteData on FakeKite (thread_check=False) behind an inline order lane and the real ReadsLane, the real Feed with a
FakeTicker fed by the ScriptedMarket, the real QuoteCache, and real SQLite command DBs (the dashboard's own schema).

One step (default 50 ms): the market sends its WS ticks, the broker (FakeKite) and the paper exchange run what is
due, a Timer is posted every 250 ms, and every queued event goes through the §3.3 core loop (one transaction per
event, check_invariants inside it; rollback + reload + on_invariant_violation or raise_halt on failure; commands
dispatched only after the commit). Lanes execute their commands inline; their answers become events in turn, until
nothing is left. Then the signals (when started) run the strategies on the queued ticks.

A process is everything a crash loses (engine, store connection, paper-sim, lanes, feed, queues). crash() drops it;
boot() starts a new one exactly as the runtime would (§9.5: recovery on REST, READY, then the WS with the
underlyings in quote mode and held options in full mode). The broker and the market outlive it.

Dual run (§16.1, R4): the account under test is PAPER or LIVE. LIVE runs get ALLOW_LIVE, every registered strategy
LIVE with a CERTIFIED trial and an unlock report, five earlier LIVE sessions (past the first-week cap) and, unless the
scenario asks for the real one, today's self-test already passed. trace() normalizes the account's broker commands
(the account and tag letters, broker ids) and its booked trades, so the PAPER and LIVE runs of a scenario can be
compared for equality.

Laptop profile: ScenarioRunner(..., laptop=True), or laptop_profile() before a boot(), wires the next process as
BotProcess(laptop=True) does: allow_live False, live_orders False (the KiteBroker refuses every LIVE place and cancel,
recovery imports no LIVE tags) and the laptop runner's Ctrl+C FLATTEN_ALL as a stop's (stop_flatten_by). A LIVE
runner switched to it mid-day is EC2 handing the shared Kite account over to the laptop.

Broker-read delay (decision B1): paper-sim answers a positions or orders read after a seeded delay (one FIFO per
account). The account under test pays it in both runs. The LIVE run mirrors it: LIVE FetchPositions/FetchOrders are
held in the process with the same scheduling, drawn from the PAPER account's read seed (read_latency_seed(session,
PAPER)) and answered from FakeKite through the process's KiteData, stamped with their dispatch time. The draws are
kept in the runner, so they outlive crash() (like FakeKite's order latency); held reads die with the process. The
LIVE-only WS back-fill FetchOrders (cause FeedState, excluded from trace()) is answered at once, without a draw.
Only the account under test pays the delay: the other accounts' reads are answered at once (DualPaperSim for PAPER
and SHADOW, the reads lane for LIVE in a PAPER run). Their delays would come from read histories the two runs do not
share (the LIVE run's PAPER twin trades, while the PAPER run's LIVE account is idle), and recovery and the entry
resume wait for every account, so those delays would show in the compared traces.
"""

from __future__ import annotations

import collections
import itertools
import json
import math
import queue
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from titan2 import laptop as laptop_mod
from titan2 import ledger, selfcheck, store as store_mod, strategies
from titan2.broker.kite import KiteBroker, KiteBuckets, KiteData
from titan2.broker.lanes import ACCOUNT_READ_COMMANDS, OrderLane, ReadsLane, TokenBucket, execute_broker, lane_for
from titan2.broker.paper import DELAYED_READS, PaperSim, read_latency_seed
from titan2.charges import OrderFills, net_pnl, rates_for
from titan2.clock import FakeClock, ist_datetime, iso, session_of
from titan2.core.engine import Engine
from titan2.core.events import (
    Alert, Cancel, Command, ContractNote, Event, Exit, FetchMargins, FetchOrders, FetchPositions, FetchQuotes,
    FetchTradebook, FetchTrades, IntentReceived, LookupTag, Place, PollOrder, RecoveryStart, RunSelfcheck,
    SelfcheckResult, StartSignals, Subscribe, Timer, Unsubscribe, BarsClosed,
)
from titan2.core.single_leg import SingleLegHandler
from titan2.core.state import EngineContext
from titan2.dash.app import ensure_commands_db
from titan2.market.bars import Bars
from titan2.market.feed import Feed, FeedLink, SignalQueue, SignalsLost
from titan2.market.quotes import QuoteCache
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, Direction, Intent, InvariantViolation, Policy, Recheck, Role, Side, StrikePref,
    Structure, StructState, StructureType, parse_tag,
)
from titan2.settings import (
    DEFAULTS, INTENT_MAX_PER_MIN, PAPER_LATENCY_CLIP_MS, TIMER_DT, Settings, exit_profile_problems,
)
from titan2.store import Store

from fakes import universe as uni
from fakes.fake_kite import FakeKite
from fakes.faults import FaultInjector
from fakes.market import FakeTicker, ImmediateReactor, ScriptedMarket
from fakes.paper import FaultyPaperExchange

CLI_KINDS = ("PAUSE", "RESUME", "FLATTEN_ALL", "KILL", "ACK_HALT", "ACK_ALERT", "ACK_FOREIGN", "SET_MODE", "RUN_RECON")
DEFAULT_STRATEGY = "price_spike_up"
SETTLE_LIMIT = 200_000


class Crash(Exception):
    """Raised inside the runner to kill the process at a scripted point (the scenario then calls boot())."""


@dataclass(frozen=True)
class Sent:
    wall: datetime
    mono: float
    cause: str                         # type name of the event whose handling produced the command
    cmd: Command


@dataclass
class Process:
    """One titan2-bot process: everything a crash loses."""

    store: Store
    quotes: QuoteCache
    core_q: collections.deque
    paper: FaultyPaperExchange
    paper_sim: PaperSim
    order_lane: OrderLane
    order_q: collections.deque
    reads: ReadsLane                   # LIVE tradebook and margins reads (and the WS back-fill): answered at once
    mdata: ReadsLane                   # market data over REST: answered on the next step (REST latency)
    ticker: FakeTicker
    signal_q: SignalQueue
    feed: Feed
    ctx: EngineContext
    engine: Engine
    started_mono: float
    next_timer: float
    data: KiteData | None = None       # answers the held LIVE positions/orders reads
    # LIVE positions/orders reads, held like paper-sim's (one FIFO: LIVE is the only account on lane-reads)
    read_q: collections.deque = field(default_factory=collections.deque)    # dispatched, not drawn yet: (cmd, mono)
    held_reads: collections.deque = field(default_factory=collections.deque)  # (due, cmd, dispatch mono), FIFO
    last_due: float | None = None
    feed_started: bool = False
    signals_started: bool = False
    exit_code: int | None = None


class DualPaperSim(PaperSim):
    """PaperSim whose read delay applies to the `delayed` accounts only (the account under test): the other paper
    accounts' positions and orders reads are answered at once (see the module docstring)."""

    def __init__(self, *args: Any, delayed: frozenset[Account], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.delayed = delayed

    def _hold(self, cmd: Command, receipt: float, safe: bool) -> list[Event]:
        if Account(cmd.account) in self.delayed:
            return super()._hold(cmd, receipt, safe)
        return self._execute(cmd, safe)


class _Handler(SingleLegHandler):
    """SingleLegHandler that records when each leg was last evaluated (chaos invariant 4) and can raise once
    (a bug inside an event: the core loop's rollback-and-reload path)."""

    def __init__(self, seen: dict[int, float], runner: ScenarioRunner) -> None:
        self._seen = seen
        self._runner = runner

    def evaluate(self, st, quotes, now_mono, now_wall, forced, ctx):
        exc = self._runner.raise_in_evaluate
        if exc is not None:
            self._runner.raise_in_evaluate = None
            raise exc
        for lid in st.legs:
            self._seen[lid] = now_mono
        return super().evaluate(st, quotes, now_mono, now_wall, forced, ctx)


def fast_buckets(clock: FakeClock) -> KiteBuckets:
    """Buckets that never wait in the harness (pacing is U07's own test); FakeKite records the order rate."""
    def no_sleep(s: float) -> None:
        raise AssertionError(f"a Kite rate bucket would block for {s:.3f}s inside the harness")

    def b(name: str) -> TokenBucket:
        return TokenBucket(name, 1e6, 1_000_000, mono=clock.mono, sleep=no_sleep)

    return KiteBuckets(b("orders"), b("quote"), b("historical"), b("other"))


def make_cli_db(path: Path) -> Path:
    """db/commands_cli.db with the DETAIL §4.4 table (the CLI's extra kinds) and its insert-only triggers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        kinds = ",".join(f"'{k}'" for k in CLI_KINDS)
        con.executescript(
            "CREATE TABLE IF NOT EXISTS commands(cmd_id INTEGER PRIMARY KEY, created_ts TEXT NOT NULL, "
            f"requested_by TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ({kinds})), "
            "args TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(args)), confirm_text TEXT, nonce TEXT NOT NULL UNIQUE);"
            "CREATE TRIGGER IF NOT EXISTS c_no_upd BEFORE UPDATE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END;"
            "CREATE TRIGGER IF NOT EXISTS c_no_del BEFORE DELETE ON commands BEGIN SELECT RAISE(ABORT,'insert-only'); END;")
        con.commit()
    finally:
        con.close()
    return path


class _MarketView:
    """The signals thread's read-only MarketView over the process's QuoteCache, bars and feed."""

    def __init__(self, runner: ScenarioRunner, bars: Bars) -> None:
        self.r = runner
        self.b = bars
        self.ticks: dict[str, Any] = {}

    def ltp(self, u):
        q = self.r.proc.quotes.last(u) if self.r.proc else None
        return None if q is None or q.ltp is None else (q.ltp, q.age(self.r.clock.mono()))

    def last_tick(self, u):
        return self.ticks.get(u)

    def bars(self, u, n):
        return self.b.bars(u, n)

    def prev_session_bars(self, u, n):
        return self.b.prev_session_bars(u, n)

    def universe(self):
        return self.r.instruments.universe(self.r.ban_list or (), self.r.settings.STOCK_ALLOWLIST,
                                           self.r.settings.STOCK_DENYLIST)

    def feed_ok(self):
        return bool(self.r.proc and self.r.proc.feed.health().connected)

    def now_mono(self):
        return self.r.clock.mono()


class ScenarioRunner:
    def __init__(self, tmp: Path, acct: Account | str, *, start: str | datetime = "09:18:30",
                 session: date = uni.SESSION, settings: Settings = DEFAULTS, faults: FaultInjector | None = None,
                 selftest: str = "passed", pub: bool = False, signals: Iterable[str] = (), strict: bool = True,
                 dt: float = 0.05, fast_db: bool = True, boot: bool = True,
                 warmup_until: str | None = "09:20:00", warmup_dt: float = 0.25, laptop: bool = False) -> None:
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.acct = Account(acct)
        self.settings = settings
        self.dt = dt
        self.strict = strict
        self.fast_db = fast_db
        self.pub = pub
        self.selftest = selftest
        self.signal_names = tuple(signals)
        when = start if isinstance(start, datetime) else ist_datetime(session, start)
        self.clock = FakeClock(when, mono_start=10_000.0)
        self._prev_core = store_mod.set_core_thread()
        self._stores: dict[date, Any] = {}
        self.market = ScriptedMarket(self.clock, self.instruments)
        self.faults = faults if faults is not None else FaultInjector()
        self.faults.exit_in_progress = self._exit_in_progress
        self.faults.bind(self.acct, self.clock, self.market)
        self.kite = FakeKite(self.clock, self.market, self.instruments, settings, self.faults)
        self.ban_list: frozenset[str] | None = frozenset()
        self.allow_live = self.acct is Account.LIVE
        self.live_orders = True                    # what the next boot() wires (laptop_profile() changes them)
        self.stop_flatten_by: str | None = None
        if laptop:
            self.laptop_profile()
        self.versions = {n: strategies.version(c, settings) for n, c in strategies.REGISTRY.items()}
        self.version_inputs = {n: strategies.version_inputs(c, settings) for n, c in strategies.REGISTRY.items()}
        self.exit_profiles = {n: c.EXIT_PROFILE for n, c in strategies.REGISTRY.items()}
        self.db_path = self.tmp / "db" / "titan2.db"
        self.paper_path = self.tmp / "db" / "paper_exchange.db"
        self.dash_db = self.tmp / "dash" / "commands_dash.db"
        self.cli_db = make_cli_db(self.tmp / "db" / "commands_cli.db")
        self.dash_db.parent.mkdir(parents=True, exist_ok=True)
        ensure_commands_db(self.dash_db)
        self.pub_dir = self.tmp / "pub"
        self.sent: list[Sent] = []
        self.alerts: list[tuple[datetime, Alert]] = []
        self.exits: list[tuple[datetime, int]] = []
        self.violations: list[tuple[datetime, InvariantViolation]] = []
        self.exceptions: list[tuple[datetime, BaseException]] = []
        self.evaluated: dict[int, float] = {}
        self.raise_in_evaluate: BaseException | None = None
        self.boots = 0
        self.proc: Process | None = None
        self.crash_on: Callable[[Command], str | None] | None = None   # -> "before_dispatch" | "after_send" | None
        self.after_step: list[Callable[[ScenarioRunner], None]] = []
        self.dispatch_hooks: list[Callable[[ScenarioRunner, Command], None]] = []
        self.timer_hooks: list[Callable[[float], None]] = []     # after each Timer the core finished (heartbeat)
        self.halts_before: tuple = ()
        self.events_seen = 0
        self._ids = itertools.count(1)
        self._nonces = itertools.count(1)
        self._token_seen_rotation = False
        self._live_fixture_done = False
        self._cause = "-"
        self._down_since: float | None = None
        # The engine polls the command DBs once a second (two SQLite opens). They are handed over on the first
        # command (command() / enable_commands()): until then no command can exist, and the polls cost most of an
        # idle step.
        self._command_dbs: dict[str, Path] = {}
        self._paper_con: sqlite3.Connection | None = None
        self._paper_cache: tuple | None = None
        self._fill_logs: dict[Account, tuple[Any, list]] = {}
        self._read_rngs: dict[date, random.Random] = {}    # the LIVE read delay's draws: they outlive crash()
        if boot:
            self.boot()
            if warmup_until is not None and self.clock.now_wall() < ist_datetime(self.session, warmup_until):
                self.run_until(warmup_until, dt=warmup_dt)

    # -- the session's dump ----------------------------------------------------------------------------------------
    @property
    def session(self) -> date:
        return session_of(self.clock.now_wall())

    @property
    def instruments(self):
        d = self.session
        st = self._stores.get(d)
        if st is None:
            st = self._stores[d] = uni.instrument_store(d)
        return st

    @property
    def store(self) -> Store:
        if self.proc is None:
            raise RuntimeError("no bot process is running")
        return self.proc.store

    @property
    def engine(self) -> Engine:
        return self.proc.engine

    # -- process lifecycle -----------------------------------------------------------------------------------------
    def boot(self) -> None:
        """Start a titan2-bot process (§9.5): store, paper-sim, lanes, feed (not connected), Engine.load, then
        RecoveryStart. The WS connects once recovery is done (step())."""
        if self.proc is not None:
            raise RuntimeError("a process is already running")
        store_mod.set_core_thread()
        st = Store(self.db_path, backup_dir=self.tmp / "backups")
        if self.fast_db:
            st._conn.execute("PRAGMA synchronous=OFF")   # test speed only; crash consistency is not under test
        if self.acct is Account.LIVE and not self._live_fixture_done:
            self._live_fixture(st)
            self._live_fixture_done = True
        quotes = QuoteCache(mono=self.clock.mono)
        core_q: collections.deque = collections.deque()
        holder: dict[str, Any] = {}

        def request_depth(cmd: FetchQuotes) -> None:
            holder["mdata"].submit(cmd, reply=lambda ev: None)    # REST depth lands in the QuoteCache

        paper = FaultyPaperExchange(self.paper_path, settings=self.settings, clock=self.clock, quotes=quotes,
                                    calendar=uni.CALENDAR, instruments=self.instruments.by_symbol,
                                    request_depth=request_depth, thread_check=False, faults=self.faults,
                                    emit_early=core_q.extend)
        if self.fast_db:
            paper.start()
            paper._conn.execute("PRAGMA synchronous=OFF")
        paper_sim = DualPaperSim(paper, core_q.append, delayed=frozenset({self.acct}) - {Account.LIVE})
        buckets = fast_buckets(self.clock)
        broker = KiteBroker(self.kite, entries_allowed=self.allow_live, orders_allowed=self.live_orders,
                            thread_check=False, clock=self.clock, buckets=buckets)
        order_lane = OrderLane(broker, core_q.append, mono=self.clock.mono)
        data = KiteData(self.kite, clock=self.clock, buckets=buckets)
        reads = ReadsLane(data, core_q.append, quotes.put, prep=self._prep_handlers(), mono=self.clock.mono)
        mdata = ReadsLane(data, core_q.append, quotes.put, prep=self._prep_handlers(), mono=self.clock.mono)
        holder["mdata"] = mdata
        ticker = FakeTicker()
        signal_q = SignalQueue()
        feed = Feed(ticker, quotes, core_q.append, signal_q, self._symbol_of, clock=self.clock,
                    reactor=ImmediateReactor())
        self.market.feed, self.market.ticker, self.market.connected = feed, ticker, False
        self.kite.on_order_update = lambda row: feed.on_order_update(None, row)
        h = _Handler(self.evaluated, self)
        ctx = EngineContext(
            settings=self.settings, calendar=uni.CALENDAR, clock=self.clock, quotes=quotes, versions=self.versions,
            version_inputs=self.version_inputs, exit_profiles=self.exit_profiles,
            handlers={StructureType.LONG_CE: h, StructureType.LONG_PE: h}, allow_live=self.allow_live,
            live_orders=self.live_orders, stop_flatten_by=self.stop_flatten_by,
            instruments=lambda: self.instruments, ban_list=lambda: self.ban_list, feed_health=feed.health,
            take_dirty=feed.take_dirty, signals_degraded=lambda: False,
            pub_dir=self.pub_dir if self.pub else None, reports_dir=self.tmp / "reports",
            exports_dir=self.tmp / "exports", command_dbs=self._command_dbs, start_mono=self.clock.mono())
        engine = Engine.load(st, ctx)
        self.proc = Process(st, quotes, core_q, paper, paper_sim, order_lane, collections.deque(), reads, mdata,
                            ticker, signal_q, feed, ctx, engine, self.clock.mono(), self.clock.mono(), data=data)
        self.boots += 1
        self._down_since = None
        self._bars = Bars.for_session(uni.CALENDAR, self.session)
        self._view = _MarketView(self, self._bars) if self._bars is not None else None
        self._runners = {}
        if self.signal_names:
            loaded, failed = strategies.load(self.settings, exit_profile_problems, max_per_min=INTENT_MAX_PER_MIN)
            assert not failed, failed
            self._runners = {n: loaded[n] for n in self.signal_names}
        self._last_on_timer: float | None = None
        core_q.append(RecoveryStart())
        self.settle()

    def laptop_profile(self) -> None:
        """The next boot() is wired as BotProcess(laptop=True) wires the bot: PAPER only (allow_live False), no LIVE
        order of any role (the KiteBroker refuses them, recovery imports no LIVE tags) and the runner's Ctrl+C
        FLATTEN_ALL (requested_by laptop.REQUESTED_BY) treated as a stop's. The broker and the market are shared."""
        self.allow_live, self.live_orders = False, False
        self.stop_flatten_by = f"CLI:{laptop_mod.REQUESTED_BY}"

    def crash(self) -> None:
        """Kill the bot process now: queued events and undispatched commands are lost, the DBs stay."""
        p = self.proc
        if p is None:
            return
        self.proc = None
        self._down_since = self.clock.mono()
        self.market.feed, self.market.ticker, self.market.connected = None, None, False
        self.kite.on_order_update = None
        try:
            p.store.close()
        finally:
            p.paper.close()

    def restart(self, downtime_s: float = 0.0, dt: float = 0.25) -> None:
        """crash(), let the market and the broker run on for downtime_s, then boot()."""
        self.crash()
        if downtime_s > 0:
            self.run(downtime_s, dt=dt)
        self.boot()

    def close(self) -> None:
        if self.proc is not None:
            self.crash()
        if self._paper_con is not None:
            self._paper_con.close()
            self._paper_con = None
        store_mod.set_core_thread(self._prev_core) if self._prev_core is not None else store_mod.clear_core_thread()

    # -- the LIVE fixture (§16.1: ALLOW_LIVE plus unlocked strategies) -------------------------------------------
    def _live_fixture(self, st: Store) -> None:
        now = self.clock.now_wall()
        mono = self.clock.mono()
        prior = uni.CALENDAR.trading_days(self.session - timedelta(days=20), self.session - timedelta(days=1))[-5:]
        since = prior[0]
        with st.tx():
            for name, version in sorted(self.versions.items()):
                st.put_strategy_mode(name, version=version, mode="PAPER", changed_wall=now, changed_by="fixture")
                st.register_trial(name, version, now - timedelta(days=40), dict(self.version_inputs[name]))
                rid = st.insert_g1_report({"strategy": name, "version": version, "kind": "CONFIRM",
                                           "computed_ts": now - timedelta(days=1), "passed": True,
                                           "inputs_hash": "f" * 64, "detail": {"fixture": True}})
                st.update_trial(name, version, state="CERTIFIED", cert_report_id=rid, state_ts=now)
                st.update_row("strategy_modes", {"strategy": name},
                              {"mode": "LIVE", "live_unlock_report_id": rid, "live_since_session": since})
                for i, d in enumerate(prior):         # past the first-week cap: five earlier LIVE sessions
                    wall = ist_datetime(d, "10:00:00")
                    # the whole name: price_spike_up and price_spike_down share their first six characters
                    it = Intent(f"fx{name}{i}", name, version, "NSE:INFY", Direction.UP, StructureType.LONG_CE,
                                StrikePref.ATM, EXPIRY_RULE_NEAREST, 1500.0, Recheck(1.0, 1e9), 10.0, mono, wall,
                                {"fixture": "earlier LIVE session"})
                    st.insert_intent(it, d)
                    st.insert_structure(Structure(
                        0, d, f"LIVE:{name}", Account.LIVE, Policy.FULL, name, version, it, StructureType.LONG_CE,
                        "NSE:INFY", StructState.VOID, [], False, None, None, None, None, None, None, None,
                        "LIQUIDITY:fixture", wall, None, wall, None))
            if self.selftest == "passed":
                st.append_event("SELFTEST", wall=now, mono=mono, session=self.session,
                                payload={"attempt": 1, "outcome": "PASSED", "fixture": True})

    # -- prep (lane-reads handlers) -------------------------------------------------------------------------------
    def _prep_handlers(self) -> dict[str, Callable[[], tuple[bool, str]]]:
        def token() -> tuple[bool, str]:
            if self.faults.token_rotated and not self._token_seen_rotation and not self.faults.token_dead():
                self._token_seen_rotation = True
                return True, "NEW session token works (profile ok)"
            self.kite.profile()                     # raises TokenException while the token is dead
            return True, "profile ok"

        return {"INSTRUMENTS": lambda: (True, f"harness dump {self.session}"),
                "BAN_LIST": lambda: (self.ban_list is not None, "harness ban list"),
                "PREV_BARS": lambda: (True, "harness"), "TOKEN": token}

    def _symbol_of(self, token: int) -> str | None:
        return self.instruments.symbol_for_token(token) or self.market._token_symbol.get(token)

    # -- the core loop (§3.3) --------------------------------------------------------------------------------------
    def _core(self, ev: Event) -> None:
        p = self.proc
        self._cause = type(ev).__name__
        self.halts_before = tuple(p.engine.state.halts)     # what the event started with (chaos invariant 5)
        self.events_seen += 1
        try:
            with p.store.tx():
                cmds = p.engine.handle(ev)
                p.engine.check_invariants()
        except InvariantViolation as iv:
            self.violations.append((self.clock.now_wall(), iv))
            p.engine = Engine.load(p.store, p.ctx)
            with p.store.tx():
                cmds = p.engine.on_invariant_violation(iv)
        except Crash:
            raise
        except Exception as exc:
            self.exceptions.append((self.clock.now_wall(), exc))
            if self.strict:
                raise
            p.engine = Engine.load(p.store, p.ctx)
            with p.store.tx():
                cmds = p.engine.raise_halt("EXCEPTION", "GLOBAL", "ACK", repr(exc)[:500])
        for c in cmds:
            self._dispatch(c)
        if isinstance(ev, Timer):
            for hook in self.timer_hooks:
                hook(self.clock.mono())

    def _dispatch(self, c: Command) -> None:
        p = self.proc
        point = self.crash_on(c) if self.crash_on is not None else None
        if point == "before_dispatch":
            self.crash_on = None
            raise Crash(f"crash before dispatching {type(c).__name__}")
        self.sent.append(Sent(self.clock.now_wall(), self.clock.mono(), self._cause, c))
        for hook in self.dispatch_hooks:
            hook(self, c)
        lane = lane_for(c)
        if lane == "paper":
            p.paper_sim.submit(c)
        elif lane == "orders":
            p.order_q.append(c)
        elif (lane == "reads" and isinstance(c, DELAYED_READS) and self.acct is Account.LIVE
              and self._cause != "FeedState"):
            p.read_q.append((c, self.clock.mono()))          # held like paper-sim's reads (_live_reads)
        elif lane == "reads" and isinstance(c, ACCOUNT_READ_COMMANDS):
            p.reads.submit(c)
        elif lane == "reads":
            p.mdata.submit(c)
        elif isinstance(c, (Subscribe, Unsubscribe)):
            p.feed.dispatch(c)
        elif isinstance(c, Alert):
            self.alerts.append((self.clock.now_wall(), c))
        elif isinstance(c, StartSignals):
            p.signals_started = True
        elif isinstance(c, RunSelfcheck):
            p.core_q.append(SelfcheckResult(c.phase, True, selfcheck.Report(c.phase, self.clock.now_wall(), ())))
        elif isinstance(c, Exit):
            self.exits.append((self.clock.now_wall(), c.code))
            if p.exit_code is None:
                p.exit_code = c.code
        else:
            raise AssertionError(f"the harness cannot dispatch {c!r}")
        if point == "after_send":
            self.crash_on = None
            self._send_now(c)
            raise Crash(f"crash after sending {type(c).__name__}, before its answer")

    def _send_now(self, c: Command) -> None:
        """Execute one queued broker command at once and drop its answer (the process dies before reading it). The
        LIVE reads already handed over get their draws too, as paper-sim's drain draws its queued reads."""
        p = self.proc
        if lane_for(c) == "orders":
            p.order_q.remove(c)
            p.order_lane.execute(c)
            self.kite.flush()
        elif lane_for(c) == "paper":
            p.paper_sim.drain()
        self._live_reads(p)

    def _read_delay_ms(self) -> float:
        """The next draw of paper's read-delay model (PaperExchange.read_latency_ms) from the PAPER account's read
        seed: the n-th LIVE positions/orders read waits as long as the n-th PAPER one."""
        d = self.session
        rng = self._read_rngs.get(d)
        if rng is None:
            rng = self._read_rngs[d] = random.Random(read_latency_seed(d, Account.PAPER))
        s = self.settings
        ms = rng.lognormvariate(math.log(s.PAPER_READ_LATENCY_MEDIAN_MS), s.PAPER_READ_LATENCY_SIGMA)
        lo, hi = PAPER_LATENCY_CLIP_MS
        return min(max(ms, lo), hi)

    def _live_reads(self, p: Process) -> list[Event]:
        """paper-sim's read delay, mirrored for LIVE: the reads dispatched since the last pass get their due time
        (receipt, or the previous read's due if later, plus a draw), then every held read now due is answered from
        FakeKite through the process's KiteData, stamped with its dispatch time. Returns the events queued."""
        while p.read_q:
            c, sent = p.read_q.popleft()
            start = sent if p.last_due is None else max(sent, p.last_due)
            due = start + self._read_delay_ms() / 1000.0
            p.last_due = due
            p.held_reads.append((due, c, sent))
        out: list[Event] = []
        now = self.clock.mono()
        while p.held_reads and p.held_reads[0][0] <= now:
            _, c, sent = p.held_reads.popleft()
            out += execute_broker(c, p.data, self.clock.mono, req_mono=sent)
        p.core_q.extend(out)
        return out

    def settle(self) -> None:
        """Run the core loop and the lanes until nothing is left to do."""
        p = self.proc
        if p is None:
            return
        mdata_answered = False
        try:
            for _ in range(SETTLE_LIMIT):
                if self.proc is not p:
                    return
                if p.core_q:
                    self._core(p.core_q.popleft())
                    continue
                if p.order_q:
                    cmd = p.order_q.popleft()
                    for ev in p.order_lane.execute(cmd):
                        p.core_q.append(ev)
                    self.kite.flush()
                    continue
                if p.reads.backlog():
                    p.reads.step()
                    continue
                if p.paper_sim.drain() + self._live_reads(p):
                    continue
                if not mdata_answered and p.mdata.backlog():
                    # REST market data is answered once per pass: a request made while handling a REST answer
                    # (a failed refresh asking again) waits for the next pass, as the 1/s quote bucket would make it
                    mdata_answered = True
                    p.mdata.drain()
                    continue
                return
            raise AssertionError("settle() did not converge: an event/command loop")
        except Crash:
            self.crash()

    # -- stepping ---------------------------------------------------------------------------------------------------
    def step(self, dt: float | None = None) -> None:
        dt = self.dt if dt is None else dt
        self.clock.advance(dt)
        now, mono = self.clock.now_wall(), self.clock.mono()
        p = self.proc
        self.market.step(now, mono)
        self.kite.step(mono)
        if p is not None:
            p.paper_sim.drain()
            self._live_reads(p)
            if mono >= p.next_timer - 1e-9:
                p.core_q.append(Timer(now, mono))
                p.next_timer = mono + TIMER_DT
            self.settle()
            if self.proc is p and p.engine.recovery_done and not p.feed_started:
                self._start_feed()
                self.settle()
            if self.proc is p and p.signals_started and self._runners:
                self._signals(now, mono)
                self.settle()
            if self.proc is p and p.exit_code is not None:
                self.crash()                         # the process exits (0, 3, 75): systemd may start it again
        for fn in self.after_step:
            fn(self)

    def _start_feed(self) -> None:
        """§9.5 step 12: the WS starts once recovery is done; underlyings in quote mode, held options full."""
        p = self.proc
        p.feed_started = True
        p.feed.request(sorted(u.token for u in self.instruments.underlyings()), "quote")
        held = sorted({lg.inst.token for lg in p.engine.state.legs.values() if not lg.is_terminal})
        if held:
            p.feed.request(held, "full")
        self.market.connected = True
        p.feed.on_connect(None)

    def run(self, seconds: float, dt: float | None = None) -> None:
        end = round(self.clock.mono() + seconds, 6)          # the FakeClock counts whole microseconds
        while self.clock.mono() < end - 5e-7:
            self.step(min(self.dt if dt is None else dt, end - self.clock.mono()))

    def run_until(self, t: str | datetime, dt: float | None = None) -> None:
        target = ist_datetime(self.session, t) if isinstance(t, str) else t
        while self.clock.now_wall() < target:
            left = (target - self.clock.now_wall()).total_seconds()
            self.step(min(self.dt if dt is None else dt, left))

    def run_while(self, cond: Callable[[], bool], timeout_s: float, dt: float | None = None) -> bool:
        """Step while cond() holds, at most timeout_s. True when cond() stopped holding."""
        end = self.clock.mono() + timeout_s
        while cond():
            if self.clock.mono() >= end - 1e-9:
                return False
            self.step(dt)
        return True

    def run_until_true(self, cond: Callable[[], bool], timeout_s: float, dt: float | None = None) -> bool:
        return self.run_while(lambda: not cond(), timeout_s, dt)

    def next_session(self, d: date, start: str = "09:18:30", warmup_until: str | None = "09:20:00") -> None:
        """Jump to another session day: the bot goes down, the broker runs through the rest of the day (its MIS
        square-off included), then a new process boots on day d and warms up."""
        self.crash()
        end = ist_datetime(self.session, "15:45:00")
        if self.clock.now_wall() < end:
            self.run_until(end, dt=60.0)
        self.clock.advance_to(ist_datetime(d, start))
        self.kite.step()
        self.boot()
        if warmup_until is not None:
            self.run_until(warmup_until, dt=0.25)

    # -- signals (the signals thread, inline) ----------------------------------------------------------------------
    def _signals(self, now: datetime, mono: float) -> None:
        p = self.proc
        intents, closed = [], []
        while True:
            try:
                item = p.signal_q.get(timeout=0)
            except queue.Empty:
                break
            if isinstance(item, FeedLink):
                closed += self._bars.on_link(item.connected, item.recv_wall)
                if item.connected and item.reconnected:
                    for r in self._runners.values():
                        r.on_feed_reset()
            elif isinstance(item, SignalsLost):
                closed += self._bars.on_gap(item.first_wall, item.last_wall)
                for r in self._runners.values():
                    r.on_feed_reset()
            else:
                for t in item:
                    self._view.ticks[t.symbol] = t
                    closed += self._bars.on_tick(t)
                    for r in self._runners.values():
                        intents += r.on_tick(t, self._view).intents
        closed += self._bars.close_due(now)
        for b in closed:
            for r in self._runners.values():
                intents += r.on_bar_close(b, self._view).intents
        if self._last_on_timer is None or mono - self._last_on_timer >= TIMER_DT - 1e-9:
            self._last_on_timer = mono
            for r in self._runners.values():
                intents += r.on_timer(now, self._view).intents
        if closed:
            p.core_q.append(BarsClosed(tuple(closed)))
        for i in intents:
            p.core_q.append(IntentReceived(i))

    # -- scenario inputs --------------------------------------------------------------------------------------------
    def spot(self, underlying: str) -> float:
        return self.market.unders[underlying].ltp

    def contract(self, underlying: str = "NSE:RELIANCE", opt_type: str = "CE", pref: str = "ATM",
                 spot: float | None = None):
        return uni.symbol_of(self.instruments, underlying, opt_type, pref,
                             self.spot(underlying) if spot is None else spot, self.session)

    def make_intent(self, underlying: str = "NSE:RELIANCE", direction: str = "UP", *,
                    strategy: str = DEFAULT_STRATEGY, strike_pref: str = "ATM", ttl_s: float = 10.0,
                    recheck: tuple[float, float] | None = None, intent_id: str | None = None) -> Intent:
        spot = self.spot(underlying)
        lo, hi = recheck if recheck is not None else (round(spot * 0.99, 2), round(spot * 1.01, 2))
        d = Direction(direction)
        st = StructureType.LONG_CE if d is Direction.UP else StructureType.LONG_PE
        iid = intent_id or f"sc{next(self._ids):010d}"
        return Intent(iid, strategy, self.versions.get(strategy, "0"), underlying, d, st, StrikePref(strike_pref),
                      EXPIRY_RULE_NEAREST, spot, Recheck(lo, hi), ttl_s, self.clock.mono(), self.clock.now_wall(),
                      {"scenario": "harness"})

    def intent(self, underlying: str = "NSE:RELIANCE", direction: str = "UP", **kw: Any) -> Intent:
        """Post an IntentReceived now (as the signals thread would) and settle."""
        i = kw.pop("intent", None) or self.make_intent(underlying, direction, **kw)
        self.proc.core_q.append(IntentReceived(i))
        self.settle()
        return i

    def enable_commands(self) -> None:
        """Let the engine poll the dashboard and CLI command DBs (done by command(); call it before driving the
        dashboard app directly)."""
        self._command_dbs.update({"DASH": self.dash_db, "CLI": self.cli_db})

    def command(self, kind: str, *, source: str = "CLI", args: dict | None = None, confirm: str | None = None,
                by: str = "scenario") -> int:
        """Insert an operator command as the CLI / dashboard would (read by the engine within 1 s)."""
        self.enable_commands()
        path = self.cli_db if source == "CLI" else self.dash_db
        con = sqlite3.connect(path)
        try:
            cur = con.execute("INSERT INTO commands(created_ts, requested_by, kind, args, confirm_text, nonce) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (iso(self.clock.now_wall()), by, kind,
                                                             json.dumps(args or {}), confirm,
                                                             f"n{next(self._nonces):08d}"))
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    # -- views for assertions ---------------------------------------------------------------------------------------
    def book(self, strategy: str = DEFAULT_STRATEGY) -> str:
        return f"{self.acct.value}:{strategy}"

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        """Read rows from titan2.db: the running process's store, else a read-only connection to the file."""
        if self.proc is not None:
            return [dict(r) for r in self.proc.store.query(sql, tuple(params))]
        ro = store_mod.open_readonly(self.db_path)
        try:
            return [dict(r) for r in ro.query(sql, tuple(params))]
        finally:
            ro.close()

    def intents_today(self, where: str = "1", params: Iterable[Any] = ()) -> list[dict]:
        """Today's intents rows (the LIVE fixture's earlier sessions excluded)."""
        return self.rows(f"SELECT * FROM intents WHERE session = ? AND ({where}) ORDER BY created_ts",
                         (self.session.isoformat(), *params))

    def structures(self, book: str | None = None, state: str | None = None) -> list[dict]:
        where, params = [], []
        if book is not None:
            where.append("book = ?")
            params.append(book)
        if state is not None:
            where.append("state = ?")
            params.append(state)
        sql = "SELECT * FROM structures" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY structure_id"
        return self.rows(sql, params)

    def orders(self, *, role: str | None = None, book: str | None = None, account: Account | None = None) -> list[dict]:
        acct = (account or self.acct).value
        where, params = ["account = ?"], [acct]
        if role is not None:
            where.append("role = ?")
            params.append(role)
        if book is not None:
            where.append("book = ?")
            params.append(book)
        return self.rows(f"SELECT * FROM orders WHERE {' AND '.join(where)} ORDER BY created_ts, order_pk", params)

    def legs(self, book: str | None = None) -> list[dict]:
        if book is None:
            return self.rows("SELECT * FROM legs WHERE account = ? ORDER BY leg_id", (self.acct.value,))
        return self.rows("SELECT * FROM legs WHERE book = ? ORDER BY leg_id", (book,))

    def trades(self, book: str | None = None) -> list[dict]:
        if book is None:
            return self.rows("SELECT * FROM trades WHERE account = ? ORDER BY structure_id", (self.acct.value,))
        return self.rows("SELECT * FROM trades WHERE book = ? ORDER BY structure_id", (book,))

    def halts(self, kind: str | None = None, scope: str | None = None) -> list:
        return [h for h in self.store.active_halts() if (kind is None or h.kind == kind)
                and (scope is None or h.scope == scope)]

    def events(self, kind: str | None = None) -> list[dict]:
        return self.store.events(kind=kind)

    def alert_codes(self, severity: str | None = None) -> list[str]:
        return [a.code for _, a in self.alerts if severity is None or a.severity == severity]

    def commands(self, cls: type, *, account: Account | None = None) -> list[Command]:
        acct = account if account is not None else self.acct
        return [s.cmd for s in self.sent if isinstance(s.cmd, cls) and getattr(s.cmd, "account", acct) == acct]

    def places(self, role: Role | str | None = None, *, account: Account | None = None) -> list[Place]:
        return [c for c in self.commands(Place, account=account) if role is None or c.req.role == Role(role)]

    def broker_orders(self, account: Account | None = None) -> list:
        """Every bot order the account's broker received (FakeKite for LIVE, the paper ledger for PAPER)."""
        acct = account or self.acct
        if acct is Account.LIVE:
            return list(self.kite.api_orders())
        return [o for o in self._paper_rows() if o["account"] == acct.value and o["placed_by"] == "API"]

    def _paper_ledger(self) -> tuple[list[dict], list[dict], dict[str, dict[str, int]]]:
        """(p_orders, p_fills, nets by account) of paper_exchange.db, read through one read-only connection and
        re-read only when another connection committed since (PRAGMA data_version); the paper exchange's file
        outlives every process, like the broker."""
        con = self._paper_con
        if con is None:
            con = sqlite3.connect(f"{self.paper_path.resolve().as_uri()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            self._paper_con = con
        version = con.execute("PRAGMA data_version").fetchone()[0]
        if self._paper_cache is None or self._paper_cache[0] != version:
            orders = [dict(r) for r in con.execute("SELECT * FROM p_orders ORDER BY placed_ts, broker_order_id")]
            fills = [dict(r) for r in con.execute("SELECT * FROM p_fills ORDER BY trade_id")]
            nets: dict[str, dict[str, int]] = {}
            for f in fills:
                d = nets.setdefault(f["account"], {})
                d[f["symbol"]] = d.get(f["symbol"], 0) + (f["qty"] if f["side"] == "BUY" else -f["qty"])
            self._paper_cache = (version, orders, fills, nets)
        return self._paper_cache[1], self._paper_cache[2], self._paper_cache[3]

    def _paper_rows(self) -> list[dict]:
        return self._paper_ledger()[0]

    def paper_fills(self) -> list[dict]:
        return self._paper_ledger()[1]

    def broker_fill_log(self, account: Account | None = None) -> list[tuple[str, str, int, bool]]:
        """The account's TRUE fills in fill order: (symbol, side, qty, placed by the bot). Cached until the
        broker's ledger changes (FakeKite's trade list only grows; the paper file has a data_version)."""
        acct = account or self.acct
        if acct is Account.LIVE:
            key: Any = len(self.kite.trades_all)
        else:
            self._paper_ledger()
            key = self._paper_cache[0]
        hit = self._fill_logs.get(acct)
        if hit is not None and hit[0] == key:
            return hit[1]
        if acct is Account.LIVE:
            by_id = self.kite.orders_by_id
            out = [(t.symbol, t.side, t.qty, parse_tag(by_id[t.order_id].tag) is not None)
                   for t in self.kite.trades_all]
        else:
            orders, fills, _ = self._paper_ledger()
            bot = {o["broker_order_id"]: o["placed_by"] == "API" and parse_tag(o["tag"]) is not None
                   for o in orders}
            rows = sorted((f for f in fills if f["account"] == acct.value),
                          key=lambda f: (f["fill_ts"], f["trade_id"]))
            out = [(f["symbol"], f["side"], f["qty"], bot.get(f["broker_order_id"], False)) for f in rows]
        self._fill_logs[acct] = (key, out)
        return out

    def broker_net(self, symbol: str, account: Account | None = None) -> int:
        """The TRUE net position at the broker (its own ledger, never the API view a fault may distort)."""
        acct = account or self.acct
        if acct is Account.LIVE:
            return self.kite.net(symbol)
        return self._paper_ledger()[2].get(acct.value, {}).get(symbol, 0)

    def broker_nets(self, account: Account | None = None) -> dict[str, int]:
        acct = account or self.acct
        if acct is Account.LIVE:
            return {s: n for s, n in self.kite.nets().items()}
        return dict(self._paper_ledger()[2].get(acct.value, {}))

    def _exit_in_progress(self, account: Account, symbol: str | None) -> bool:
        """The running bot is exiting a leg with qty open on (account, symbol), or on any symbol of the account
        when symbol is None (FaultInjector.exit_in_progress)."""
        p = self.proc
        if p is None:
            return False
        return any(str(lg.state) == "EXITING" and lg.open_qty > 0 and Account(lg.account) is Account(account)
                   and (symbol is None or lg.inst.symbol == symbol) for lg in p.engine.state.legs.values())

    def net_from_fills(self, structure_id: int) -> float:
        """charges.net_pnl over the structure's effective fills: the one P&L function, from fills only. A leg's
        share of a broker order that closed several legs counts as that share of the order (ledger.order_parts)."""
        st = self.store
        orders = st.query("SELECT order_pk, side, filled_qty, external_kind FROM orders WHERE structure_id = ?",
                          (structure_id,))
        fills = st.effective_fills([o["order_pk"] for o in orders])
        parts = ledger.order_parts(st, [o["order_pk"] for o in orders])
        by_pk: dict[str, list] = {}
        for f in fills:
            by_pk.setdefault(f.order_pk, []).append(f)
        done = []
        for o in orders:
            fs = by_pk.get(o["order_pk"], [])
            if not fs:
                continue
            if o["order_pk"] in parts:
                done.append(parts[o["order_pk"]])
                continue
            done.append(OrderFills(Side(o["side"]), sum(f.qty for f in fs), math.fsum(f.qty * f.px for f in fs),
                                   o["external_kind"]))
        row = st.query_one("SELECT session FROM structures WHERE structure_id = ?", (structure_id,))
        return net_pnl(done, rates_for(date.fromisoformat(row["session"]))).net

    def open_bot_orders(self) -> list[dict]:
        return self.rows("SELECT * FROM orders WHERE role <> 'EXTERNAL' AND state IN "
                         "('NEW','SUBMITTING','UNKNOWN','WORKING','CANCEL_REQUESTED')")

    # -- the dual-run trace -----------------------------------------------------------------------------------------
    def trace(self) -> dict[str, list]:
        """The account under test's broker commands (time, normalized) and booked trades, for the PAPER/LIVE
        comparison. Excluded: the LIVE self-test's own orders and the LIVE-only WS back-fill read (§9.7)."""
        acct = self.acct
        ids: dict[str, str] = {}

        def bid(x: str | None) -> str | None:
            if x is None:
                return None
            if x not in ids:
                ids[x] = f"B{len(ids) + 1}"
            return ids[x]

        def tag(pk: str | None) -> str | None:
            if pk is None:
                return None
            if parse_tag(pk) is not None:
                return pk[:3] + "X" + pk[4:]
            if pk.startswith("EX"):
                return "EX" + str(bid(pk[2:].split(":", 1)[0]))
            return pk

        selftest = {s.cmd.order_pk for s in self.sent if isinstance(s.cmd, Place) and s.cmd.req.role == Role.SELFTEST}
        out = []
        for s in self.sent:
            c = s.cmd
            if lane_for(c) is None or getattr(c, "account", None) != acct:
                continue
            pk = getattr(c, "order_pk", None)
            if pk in selftest:
                continue
            if isinstance(c, FetchOrders) and s.cause == "FeedState":
                continue
            t = s.wall.strftime("%H:%M:%S.%f")[:-3]
            if isinstance(c, Place):
                row = ("Place", tag(pk), str(c.req.role), c.req.symbol, str(c.req.side), c.req.qty, c.req.limit_px)
            elif isinstance(c, Cancel):
                row = ("Cancel", tag(pk), bid(c.broker_order_id))
            elif isinstance(c, PollOrder):
                row = ("PollOrder", tag(pk), bid(c.broker_order_id))
            elif isinstance(c, LookupTag):
                row = ("LookupTag", tag(pk))
            elif isinstance(c, FetchTrades):
                row = ("FetchTrades", tag(pk), bid(c.broker_order_id))
            elif isinstance(c, (FetchPositions, FetchOrders)):
                row = (type(c).__name__, int(c.priority))
            elif isinstance(c, (FetchTradebook, FetchMargins)):
                row = (type(c).__name__,)
            elif isinstance(c, ContractNote):
                row = ("ContractNote",) + tuple((bid(o.broker_order_id), str(o.side), o.qty, o.avg_px)
                                                for o in c.orders)
            else:
                row = (type(c).__name__,)
            out.append((t,) + row)
        trades = []
        for tr in self.rows("SELECT * FROM trades WHERE account = ? ORDER BY entry_ts, structure_id", (acct.value,)):
            trades.append((tr["book"].split(":", 1)[1], tr["symbol"], tr["qty"], tr["entry_avg"], tr["exit_avg"],
                           tr["gross"], tr["charges"], tr["net"], tr["exit_reason"], tr["pnl_status"],
                           tr["n_executed_orders"]))
        return {"commands": out, "trades": trades}


def first_diff(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"first difference at #{i}:\n  PAPER {x}\n  LIVE  {y}"
    if len(a) != len(b):
        longer, name = (a, "PAPER") if len(a) > len(b) else (b, "LIVE")
        return f"{name} has {abs(len(a) - len(b))} more; next: {longer[min(len(a), len(b))]}"
    return "identical"
