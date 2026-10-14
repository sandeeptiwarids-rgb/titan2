"""U14: Engine.load / Engine.handle, operator commands, runtime invariants (spec §3.3, §8.9, §10.8, §12.5).

Real Store (tmp), real order/position machines, recon, risk, ledger and g1; a FakeClock; a QuoteCache the test feeds.
The gateway and the exit loop are small stand-ins that use only the engine services their contract documents
(core/state.py GatewayPort / ExitLoopPort, core/engine.py docstring). Rig.run is the §3.3 core loop: one transaction
per event with check_invariants inside it; rollback, reload and on_invariant_violation / raise_halt on a failure.
A fake broker answers every broker read at once from the engine's own fills (plus test overrides).
"""

from __future__ import annotations

import dataclasses
import gzip
import itertools
import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from titan2 import store as store_mod
from titan2.clock import FakeClock, SessionCalendar, iso, ist_datetime
from titan2.core import exits, invariants, operator, positions, recon, risk
from titan2.core.engine import NIFTY_SPOT, READ_LOST_S, SELFTEST_BOOK, Engine
from titan2.core.events import (
    Alert, BarsClosed, Cancel, CancelResult, ContractNote, Exit, FeedState, FetchHistorical, FetchMargins,
    FetchOrders, FetchPositions, FetchQuotes, FetchTradebook, HistoricalResult, IntentReceived, LookupResult,
    LookupTag, MarginsResult, OrderSnapshot, OrdersSnapshot, Place, PlaceResult, PositionsSnapshot, Prep, PrepResult,
    Priority, QuoteResult, RecoveryStart, RunSelfcheck, SelfcheckResult, Shutdown, StartSignals, Timer,
    TradebookSnapshot, Unsubscribe,
)
from titan2.core.single_leg import SingleLegHandler
from titan2.core.state import EngineContext
from titan2.instruments import InstrumentStore
from titan2.market.quotes import QuoteCache
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, Bar, BrokerOrder, BrokerPosition, DecisionQuote, Direction, FeedHealth,
    HaltClears, Instrument, Intent, InvariantViolation, LegState, Level, Margins, OrderState, Policy, Quote, Recheck,
    Role, Side, StrikePref, Structure, StructState, StructureType, Underlying,
)
from titan2.selfcheck import HaltSpec
from titan2.settings import DEFAULTS
from titan2.store import Store

S = date(2026, 10, 12)                                   # a Monday
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]})
STRAT, VERSION = "opening_drive", "1.0abc"
PAPER_BOOK, LIVE_BOOK = f"PAPER:{STRAT}", f"LIVE:{STRAT}"
LOT, TICK = 100, 0.05
INST = Instrument("NFO:RELIANCE26OCT2900CE", 12345, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", "NSE:RELIANCE", "CE",
                  2900.0, date(2026, 10, 27), LOT, TICK)
INST2 = Instrument("NFO:INFY26OCT1500CE", 23456, "INFY26OCT1500CE", "NFO", "INFY", "NSE:INFY", "CE",
                   1500.0, date(2026, 10, 27), LOT, TICK)
ENTRY_DQ = DecisionQuote(bid=41.90, ask=42.15, mid=42.025, age_ms=180, src="REST", ltp=42.10, walk_px=42.15)
ENTRY_LIMIT = 42.25                                      # min(ask + 2 ticks, floor_tick(mid x 1.01))
READS = (FetchPositions, FetchOrders, FetchTradebook, FetchMargins)
_ids = itertools.count(1)


# ---------------------------------------------------------------------------------------------------------------
# Stand-ins for U13's gateway and exit loop (only the documented engine services)
# ---------------------------------------------------------------------------------------------------------------
class FakeGateway:
    """Every intent reserves and enters at once (no gates): the engine's entry services under test."""

    def __init__(self) -> None:
        self.book = PAPER_BOOK
        self.inst = {INST.underlying: INST, INST2.underlying: INST2}
        self.placed: list[str] = []
        self.quote_results: list[QuoteResult] = []
        self.timers = 0
        self.fail_after_reserve = False

    def on_intent(self, eng, intent):
        eng.store.insert_intent(intent, eng.session)
        st = eng.reserve(intent, book=self.book, account=risk.book_account(self.book),
                         policy=risk.book_policy(self.book) or Policy.FULL)
        if self.fail_after_reserve:
            raise RuntimeError("gateway bug after the reservation")
        leg, o, cmds = eng.begin_entry(st, inst=self.inst[intent.underlying], qty=LOT, limit_px=ENTRY_LIMIT,
                                       decision_quote=ENTRY_DQ, decision={"limit": ENTRY_LIMIT})
        self.placed.append(o.order_pk)
        return cmds

    def on_quote_result(self, eng, ev):
        self.quote_results.append(ev)
        return None

    def on_timer(self, eng):
        self.timers += 1
        return []


class SimpleExitLoop:
    """One EXIT SELL for the open qty at the fresh bid, then verify-flat on a post-fill positions read, then close."""

    def __init__(self, act: bool = True) -> None:
        self.act = act
        self.steps = 0
        self.terminal: list[str] = []
        self.covers: list = []

    def step(self, eng, leg):
        self.steps += 1
        if not self.act or eng.state.busy(leg) or eng.now_wall >= eng.eod.stop_sending:
            return []
        st = eng.state.structures[leg.structure_id]
        if leg.state is LegState.EXITING:
            qty = leg.open_qty
            if qty > 0:
                q = eng.quotes.fresh(leg.inst.symbol, 1.0, eng.now_mono)
                if q is None or not q.bids:
                    return eng.request_quotes((leg.inst.symbol,), Priority.EXIT)
                limit = exits.exit_limit(q, qty, leg.inst.tick, 0)
                dq = DecisionQuote(q.bid, q.ask, q.mid(), 0, q.source, q.ltp, q.bid)
                _, cmds = eng.create_order(leg, Role.EXIT, qty, limit, decision=dq, band_idx=0)
                return cmds
            positions.to_verify_flat(eng.store, st, leg, now_mono=eng.now_mono, now_wall=eng.now_wall)
        if leg.state is LegState.VERIFY_FLAT:
            acct = Account(leg.account)
            bv = eng.state.broker[acct]
            vs = eng.state.verify.get(leg.leg_id) or recon.VerifyState(eng.now_mono)
            d = recon.verify_flat(leg.leg_id, eng.symbol_view(acct, leg.inst.symbol), bv.positions, bv.orders, vs,
                                  eng.now_mono, account=acct, pos_in_flight=bv.pos_pending > 0,
                                  ords_in_flight=bv.ords_pending > 0)
            eng.state.verify[leg.leg_id] = d.state
            if d.outcome == "CLOSED":
                return eng.close(st)
            return list(d.commands)
        return []

    def on_order_terminal(self, eng, order, leg):
        self.terminal.append(order.order_pk)
        return []

    def cover(self, eng, plan):
        self.covers.append(plan)
        return []


# ---------------------------------------------------------------------------------------------------------------
# The rig: the §3.3 core loop, a fake broker, quotes, commands DBs
# ---------------------------------------------------------------------------------------------------------------
def commands_db(path: Path, kinds: tuple[str, ...]) -> Path:
    """A commands DB with the DETAIL §4.4 table (kinds: the CHECK list; a wider list forges a row)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commands(cmd_id INTEGER PRIMARY KEY, created_ts TEXT NOT NULL,"
                " requested_by TEXT NOT NULL,"
                f" kind TEXT NOT NULL CHECK (kind IN ({','.join(repr(k) for k in kinds)})),"
                " args TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(args)), confirm_text TEXT,"
                " nonce TEXT NOT NULL UNIQUE)")
    con.commit()
    con.close()
    return path


CLI_KINDS = ("PAUSE", "RESUME", "FLATTEN_ALL", "KILL", "ACK_HALT", "ACK_ALERT", "ACK_FOREIGN", "SET_MODE", "RUN_RECON")


def add_command(path: Path, kind: str, created: datetime, *, args: dict | None = None,
                confirm: str | None = None, by: str = "op", cmd_id: int | None = None) -> int:
    con = sqlite3.connect(path)
    try:
        cur = con.execute("INSERT INTO commands(cmd_id, created_ts, requested_by, kind, args, confirm_text, nonce) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)", (cmd_id, iso(created), by, kind, json.dumps(args or {}),
                                                           confirm, f"nonce-{next(_ids):08d}"))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def instrument_store(session: date = S) -> InstrumentStore:
    contracts, n = [], itertools.count(900_001)
    for name, sym, strikes, lot in (("NIFTY", NIFTY_SPOT, (24900.0, 25000.0, 25100.0), 65),
                                    ("BANKNIFTY", "NSE:NIFTY BANK", (51900.0, 52000.0, 52100.0), 30)):
        for expiry in (date(2026, 10, 13), date(2026, 10, 27)):
            for k in strikes:
                for ot in ("CE", "PE"):
                    monthly = expiry == date(2026, 10, 27)         # NSE: monthly YYMON, weekly YYMDD
                    ts = (f"{name}{expiry:%y%b}{int(k)}{ot}" if monthly
                          else f"{name}{expiry:%y}{expiry.month}{expiry:%d}{int(k)}{ot}").upper()
                    contracts.append(Instrument(f"NFO:{ts}", next(n), ts, "NFO", name, sym, ot, k, expiry, lot, TICK))
    for inst in (INST, INST2):
        contracts.append(inst)
        pe = inst.tradingsymbol[:-2] + "PE"
        contracts.append(dataclasses.replace(inst, symbol=f"NFO:{pe}", tradingsymbol=pe, opt_type="PE",
                                             token=inst.token + 1))
    unders = [Underlying(NIFTY_SPOT, 256265, "NIFTY", "INDEX"),
              Underlying("NSE:NIFTY BANK", 260105, "BANKNIFTY", "INDEX"),
              Underlying("NSE:RELIANCE", 738561, "RELIANCE", "STOCK"), Underlying("NSE:INFY", 408065, "INFY", "STOCK")]
    return InstrumentStore(session, CAL, contracts, unders, min_stocks=0)


class Rig:
    def __init__(self, tmp_path: Path, *, start: str = "2026-10-12T10:00:00+05:30", allow_live: bool = False,
                 recovered: bool = True, act: bool = True, instruments: bool = False, strict: bool = True,
                 store: Store | None = None, clock: FakeClock | None = None) -> None:
        self.tmp = tmp_path
        self.clock = clock or FakeClock(start)
        self.quotes = QuoteCache(mono=self.clock.mono)
        self.store = store or Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        self.gateway = FakeGateway()
        self.exit_loop = SimpleExitLoop(act)
        self.dash = commands_db(tmp_path / "dash" / "commands_dash.db", ("PAUSE", "RESUME", "FLATTEN_ALL"))
        self.cli = commands_db(tmp_path / "db" / "commands_cli.db", CLI_KINDS)
        self.inst_store = instrument_store() if instruments else None
        self.ctx = EngineContext(
            settings=DEFAULTS, calendar=CAL, clock=self.clock, quotes=self.quotes, versions={STRAT: VERSION},
            handlers={StructureType.LONG_CE: object(), StructureType.LONG_PE: object()}, gateway=self.gateway,
            exit_loop=self.exit_loop, allow_live=allow_live, instruments=lambda: self.inst_store,
            ban_list=lambda: frozenset(), pub_dir=tmp_path / "pub", reports_dir=tmp_path / "reports",
            exports_dir=tmp_path / "exports", command_dbs={"DASH": self.dash, "CLI": self.cli},
            start_mono=self.clock.mono(), recovered=recovered,
            prep={"INSTRUMENTS": True, "BAN_LIST": True, "PREV_BARS": True} if instruments else {})
        self.strict = strict
        self.cmds: list = []
        self.extra_nets: dict[tuple[Account, str], int] = {}
        self.broker_orders: dict[Account, list] = {}
        self.auto_reads = True
        self._bid = itertools.count(1)
        self.eng = Engine.load(self.store, self.ctx)

    # -- the core loop --------------------------------------------------------------------------------------
    def core(self, ev):
        try:
            with self.store.tx():
                cmds = self.eng.handle(ev)
                self.eng.check_invariants()
        except InvariantViolation as iv:
            self.eng = Engine.load(self.store, self.ctx)
            with self.store.tx():
                cmds = self.eng.on_invariant_violation(iv)
        except Exception as exc:
            if self.strict:
                raise
            self.eng = Engine.load(self.store, self.ctx)
            with self.store.tx():
                cmds = self.eng.raise_halt("EXCEPTION", "GLOBAL", "ACK", repr(exc)[:500])
        self.cmds.extend(cmds)
        return cmds

    def run(self, ev):
        """One event through the core loop; with auto_reads its broker reads are answered at once, and the commands
        those answers produce (an exit placed when its cap read lands, DETAIL §5.5) are returned with the event's."""
        cmds = self.core(ev)
        if self.auto_reads:
            cmds = list(cmds) + self.answer(cmds)
        return cmds

    def answer(self, cmds) -> list:
        """Answer every read in cmds (and in what the answers produce); returns the answers' other commands."""
        todo = [c for c in cmds if isinstance(c, READS)]
        out: list = []
        while todo:
            more = self.core(self.reply(todo.pop(0)))
            todo += [c for c in more if isinstance(c, READS)]
            out += [c for c in more if not isinstance(c, READS)]
        return out

    def reply(self, c):
        m = self.clock.mono()
        acct = Account(c.account)
        if isinstance(c, FetchPositions):
            return PositionsSnapshot(acct, True, self.positions(acct), m, m)
        if isinstance(c, FetchOrders):
            return OrdersSnapshot(acct, True, tuple(self.broker_orders.get(acct, ())), m, m)
        if isinstance(c, FetchTradebook):
            return TradebookSnapshot(acct, True, (), m)
        return MarginsResult(acct, True, Margins(acct, 500000.0, {}), m)

    def positions(self, acct: Account) -> tuple[BrokerPosition, ...]:
        nets: dict[str, int] = {}
        for o in self.eng.state.orders.values():
            if Account(o.account) is acct and o.filled_qty:
                nets[o.symbol] = nets.get(o.symbol, 0) + (o.filled_qty if o.side == Side.BUY else -o.filled_qty)
        for (a, sym), q in self.extra_nets.items():
            if a is acct:
                nets[sym] = nets.get(sym, 0) + q
        return tuple(BrokerPosition(acct, sym, sym.split(":")[1], "NFO", "MIS", n, max(n, 0), max(-n, 0), 0.0, 0.0)
                     for sym, n in sorted(nets.items()))

    # -- time, quotes, orders -------------------------------------------------------------------------------
    def tick(self, seconds: float = 0.25):
        self.clock.advance(seconds)
        return self.run(Timer(self.clock.now_wall(), self.clock.mono()))

    def until(self, hhmmss: str, step: float = 0.25, before=None) -> list:
        target = ist_datetime(self.clock.now_wall().date(), hhmmss)
        out = []
        while self.clock.now_wall() < target:
            if before is not None:
                before()
            out += self.tick(min(step, (target - self.clock.now_wall()).total_seconds()))
        return out

    def book(self, bid: float, ask: float, inst: Instrument = INST, qty: int = 5000) -> Quote:
        q = Quote(inst.symbol, inst.token, (Level(bid, qty), Level(round(bid - TICK, 2), qty)),
                  (Level(ask, qty), Level(round(ask + TICK, 2), qty)), round((bid + ask) / 2, 2), None,
                  self.clock.now_wall(), None, self.clock.mono(), self.clock.now_wall(), "WS")
        self.quotes.put(q)
        return q

    def place_ok(self, pk: str) -> str:
        o = self.eng.state.orders[pk]
        bid = f"B{next(self._bid):06d}"
        self.run(PlaceResult(o.account, pk, "OK", bid))
        return bid

    def snap(self, pk: str, status: str, filled: int, avg: float | None = None):
        o = self.eng.state.orders[pk]
        return self.run(OrderSnapshot(o.account, o.broker_order_id, pk, status, filled, avg, None, None, "WS",
                                      self.clock.mono()))

    def fill(self, pk: str, px: float, qty: int | None = None):
        o = self.eng.state.orders[pk]
        if o.broker_order_id is None:
            self.place_ok(pk)
        return self.snap(pk, "COMPLETE" if (qty or o.qty) == o.qty else "CANCELLED", qty or o.qty, px)

    def intent(self, underlying: str = "NSE:RELIANCE"):
        it = Intent(intent_id=f"i{next(_ids):011d}", strategy=STRAT, strategy_version=VERSION, underlying=underlying,
                    direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ATM,
                    expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0, recheck=Recheck(-math.inf, math.inf),
                    ttl_s=10.0, created_mono=self.clock.mono(), created_wall=self.clock.now_wall(),
                    reason={"why": "test"})
        self.run(IntentReceived(it))
        return self.gateway.placed[-1]

    def open_leg(self, underlying: str = "NSE:RELIANCE", px: float = 42.20):
        """An entry that fills at px with a fresh book around it: the leg is OPEN afterwards."""
        inst = self.gateway.inst[underlying]
        self.book(41.90, 42.15, inst)
        pk = self.intent(underlying)
        self.book(px - 0.10, px + 0.10, inst)
        self.fill(pk, px)
        o = self.eng.state.orders[pk]
        return self.eng.state.legs[o.leg_id]

    def leg(self, leg_id: int):
        return self.eng.state.legs[leg_id]

    def struct(self, leg_id: int):
        return self.eng.state.structures[self.eng.state.legs[leg_id].structure_id]

    # -- assertions helpers -------------------------------------------------------------------------------------
    def alerts(self, code: str | None = None, severity: str | None = None) -> list[Alert]:
        return [c for c in self.cmds if isinstance(c, Alert) and (code is None or c.code == code)
                and (severity is None or c.severity == severity)]

    def events(self, kind: str) -> list[dict]:
        return self.store.events(kind=kind)

    def halts(self, kind: str | None = None):
        return [h for h in self.store.active_halts() if kind is None or h.kind == kind]

    def ack(self, source: str, cmd_id: int) -> dict | None:
        row = self.store.query_one("SELECT * FROM command_acks WHERE source=? AND cmd_id=?", (source, cmd_id))
        return None if row is None else dict(row)

    def of(self, cls, cmds=None) -> list:
        return [c for c in (self.cmds if cmds is None else cmds) if isinstance(c, cls)]


@pytest.fixture
def core():
    with store_mod.core_thread():
        yield


@pytest.fixture
def rig(tmp_path, core):
    r = Rig(tmp_path)
    r.tick()
    yield r
    r.store.close()


def make_live(rig: Rig, *, certified: bool = True) -> None:
    """opening_drive LIVE with a CERTIFIED trial and an unlock report (the two keys turned)."""
    st = rig.store
    with st.tx():
        st.register_trial(STRAT, VERSION, rig.clock.now_wall(), {})
        rid = st.insert_g1_report({"strategy": STRAT, "version": VERSION, "kind": "CONFIRM",
                                   "computed_ts": rig.clock.now_wall(), "passed": True, "inputs_hash": "h" * 64,
                                   "detail": {}})
        if certified:
            st.update_trial(STRAT, VERSION, state="CERTIFIED", cert_report_id=rid, state_ts=rig.clock.now_wall())
        st.update_row("strategy_modes", {"strategy": STRAT}, {"mode": "LIVE", "live_unlock_report_id": rid,
                                                              "live_since_session": S})
    rig.eng = Engine.load(rig.store, rig.ctx)


def comparable(state):
    """The engine state minus what a reload re-anchors (monotonic times) or never persists (lookup counts)."""
    def leg(lg):
        return dataclasses.replace(lg, last_fill_mono=None, exiting_since_mono=None, breach_first_mono=None,
                                   breach_quote_mono=None, no_bid_since_mono=None)

    def order(o):
        return dataclasses.replace(o, submitted_mono=None, lookups=0)

    def struct(s):
        return dataclasses.replace(s, intent=dataclasses.replace(s.intent, created_mono=0.0))

    return ({k: struct(v) for k, v in state.structures.items()}, {k: leg(v) for k, v in state.legs.items()},
            {k: order(v) for k, v in state.orders.items()}, tuple(state.halts), state.modes)


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_reload_equals_db(rig):
    """The in-memory state after a run of events equals a fresh Engine.load; an exception rolls the event back and
    the reloaded engine equals the DB (the failed reservation never existed) and halts entries."""
    a = rig.open_leg()                                           # OPEN, filled
    for _ in range(8):                                           # evaluations move peak/trough/stop marks
        rig.book(42.60, 42.80)
        rig.tick()
    pk_b = rig.intent("NSE:INFY")                                # ENTERING, entry WORKING
    rig.place_ok(pk_b)
    rig.run(PlaceResult(Account.PAPER, "T2XP00000000000", "OK", "B999"))     # an answer for an unknown order: ignored
    assert rig.eng.state.legs[a.leg_id].peak_rb is not None
    assert rig.eng.state.orders[pk_b].state is OrderState.WORKING
    rig.tick(1.5)                                                # flush the 1/s marks
    fresh = Engine.load(rig.store, rig.ctx)
    assert comparable(fresh.state) == comparable(rig.eng.state)
    assert fresh.state.timers.keys() == {pk_b}                   # only the working order gets timers back
    assert any(isinstance(c, (FetchPositions, FetchOrders)) for c in fresh._pending)    # recon right after a load

    # an exception mid-event: rollback, reload, HALT(EXCEPTION); the reservation is gone from DB and memory
    n_structures = len(rig.eng.state.structures)
    rig.strict = False
    rig.gateway.fail_after_reserve = True
    it = Intent(intent_id="iexception01", strategy=STRAT, strategy_version=VERSION, underlying="NSE:SBIN",
                direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ATM,
                expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=1500.0, recheck=Recheck(-math.inf, math.inf), ttl_s=10.0,
                created_mono=rig.clock.mono(), created_wall=rig.clock.now_wall(), reason={"why": "x"})
    rig.run(IntentReceived(it))
    assert len(rig.eng.state.structures) == n_structures
    assert rig.store.query_one("SELECT * FROM intents WHERE intent_id='iexception01'") is None
    assert [h.kind for h in rig.halts("EXCEPTION")] == ["EXCEPTION"]
    assert comparable(Engine.load(rig.store, rig.ctx).state) == comparable(rig.eng.state)


def test_command_sources_expiry(rig):
    """Both command DBs are polled; kinds are checked per source, confirm texts are required, every kind but
    PAUSE / FLATTEN_ALL / KILL expires after 300 s, each row is processed once (also after a reload)."""
    now = rig.clock.now_wall()
    d1 = add_command(rig.dash, "PAUSE", now)
    d2 = add_command(rig.dash, "FLATTEN_ALL", now, confirm="flatten")             # wrong confirm text
    c1 = add_command(rig.cli, "SET_MODE", now - timedelta(seconds=301), args={"strategy": STRAT, "mode": "SHADOW"})
    c2 = add_command(rig.cli, "PAUSE", now - timedelta(hours=2))                   # survives downtime
    c3 = add_command(rig.cli, "ACK_ALERT", now - timedelta(seconds=10), args={"key": "halt:PAUSE:GLOBAL"})
    rig.tick(1.0)
    assert rig.ack("DASH", d1)["status"] == "DONE"
    assert rig.ack("DASH", d2)["status"] == "REJECTED" and "FLATTEN" in rig.ack("DASH", d2)["result"]
    assert rig.ack("CLI", c1)["status"] == "EXPIRED"
    assert rig.ack("CLI", c2)["status"] == "DONE" and "already paused" in rig.ack("CLI", c2)["result"]
    assert rig.ack("CLI", c3)["status"] == "DONE"
    assert any(a.code == "ACK_ALERT" and a.dedup_key == "halt:PAUSE:GLOBAL" for a in rig.alerts())
    assert rig.store.strategy_mode(STRAT)["pending_mode"] is None                 # the expired SET_MODE did nothing
    (pause,) = rig.halts("PAUSE")
    assert pause.operator and pause.clears.value == "RESUME"
    assert rig.store.session_flags(strategy=STRAT, session=S)                     # an operator halt: IMPAIRED

    # a forged dashboard row (KILL) is refused; the CLI's RESUME clears the PAUSE
    forged = rig.tmp / "dash" / "forged.db"
    commands_db(forged, CLI_KINDS)
    rig.ctx.command_dbs = {"DASH": forged, "CLI": rig.cli}
    f1 = add_command(forged, "KILL", rig.clock.now_wall(), confirm="KILL", cmd_id=10)
    c4 = add_command(rig.cli, "RESUME", rig.clock.now_wall())
    rig.tick(1.0)
    assert rig.ack("DASH", f1)["status"] == "REJECTED" and "not accepted from DASH" in rig.ack("DASH", f1)["result"]
    assert not rig.halts("KILL")
    assert rig.ack("CLI", c4)["status"] == "DONE" and not rig.halts("PAUSE")

    # nothing is processed twice: not on the next poll, not after a reload (last_seen comes from command_acks)
    n = rig.store.scalar("SELECT COUNT(*) FROM command_acks")
    rig.tick(1.0)
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.tick(1.0)
    assert rig.store.scalar("SELECT COUNT(*) FROM command_acks") == n
    assert len(rig.events("COMMAND_RECEIVED")) == n
    acks = json.loads((rig.tmp / "pub" / "acks.json").read_text(encoding="utf-8"))
    assert {a["cmd_id"] for a in acks["acks"] if a["source"] == "CLI"} == {c1, c2, c3, c4}

    # a recreated command file (cmd_ids below the last one picked) would be ignored: never silently
    rig.ctx.command_dbs = {"DASH": rig.dash, "CLI": rig.cli}       # tops out at 2; DASH already picked 10
    rig.tick(1.0)
    assert rig.alerts("COMMANDS_RESET", "CRITICAL")


def test_flatten_all_verified(rig):
    """FLATTEN_ALL: HALT(FLATTEN), the working entry is cancelled, the open leg exits OPERATOR_FLATTEN; ACCEPTED
    until every structure is closed AND a post-fill positions read shows every bot symbol flat; FAILED (CRITICAL)
    after 60 s while not flat, then DONE once flat. RESUME clears FLATTEN only once flat is verified."""
    a = rig.open_leg()
    pk_b = rig.intent("NSE:INFY")
    rig.place_ok(pk_b)
    rig.exit_loop.act = False                                   # hold the exit back to see ACCEPTED and FAILED
    rig.book(42.30, 42.50)
    cmd = add_command(rig.cli, "FLATTEN_ALL", rig.clock.now_wall(), confirm="FLATTEN")
    out = rig.tick(1.0)
    assert rig.ack("CLI", cmd)["status"] == "ACCEPTED"
    assert [h.kind for h in rig.halts("FLATTEN")] == ["FLATTEN"]
    assert any(isinstance(c, Cancel) and c.order_pk == pk_b for c in out)
    assert rig.leg(a.leg_id).state is LegState.EXITING and rig.leg(a.leg_id).exit_reason == "OPERATOR_FLATTEN"
    r1 = add_command(rig.cli, "RESUME", rig.clock.now_wall())
    rig.tick(1.0)
    assert rig.ack("CLI", r1)["status"] == "REJECTED" and rig.halts("FLATTEN")

    rig.run(CancelResult(Account.PAPER, pk_b, "OK"))
    rig.snap(pk_b, "CANCELLED", 0)
    assert rig.struct(rig.eng.state.orders[pk_b].leg_id).state is StructState.VOID

    for _ in range(62):                                         # not flat for 60 s: FAILED + CRITICAL, still trying
        rig.book(42.30, 42.50)
        rig.tick(1.0)
    assert rig.ack("CLI", cmd)["status"] == "FAILED"
    assert rig.alerts("FLATTEN_FAILED", "CRITICAL")

    rig.exit_loop.act = True                                    # the exit goes out and fills
    rig.book(42.30, 42.50)
    out = rig.tick()
    (place,) = [c for c in out if isinstance(c, Place)]
    assert place.req.side is Side.SELL and place.req.qty == LOT
    rig.fill(place.order_pk, 42.25)
    assert rig.leg(a.leg_id).open_qty == 0
    rig.tick(1.0)                                               # inside the 2 s post-fill window: no verdict yet
    assert rig.ack("CLI", cmd)["status"] == "FAILED"
    for _ in range(6):
        rig.tick(1.0)
    assert rig.struct(a.leg_id).state is StructState.CLOSED
    assert rig.store.trade(rig.struct(a.leg_id).structure_id)["exit_reason"] == "OPERATOR_FLATTEN"
    assert rig.ack("CLI", cmd)["status"] == "DONE" and "verified flat" in rig.ack("CLI", cmd)["result"]
    # closed structures are not enough: a broker residual on a bot symbol keeps FLATTEN until it is gone
    rig.extra_nets[(Account.PAPER, INST.symbol)] = LOT
    rig.tick(11.0)                                              # a recon round reads the residual
    assert not operator.flat_status(rig.eng)[0]
    r2 = add_command(rig.cli, "RESUME", rig.clock.now_wall())
    rig.tick(1.0)
    assert rig.ack("CLI", r2)["status"] == "REJECTED" and rig.halts("FLATTEN")
    rig.extra_nets.clear()
    rig.tick(11.0)
    r3 = add_command(rig.cli, "RESUME", rig.clock.now_wall())
    rig.tick(1.0)
    assert rig.ack("CLI", r3)["status"] == "DONE" and not rig.halts("FLATTEN")


def test_kill_demotes(rig):
    """KILL (CLI only, typed KILL): HALT(KILL, ACK), every LIVE strategy demoted (mode PAPER, trial DEMOTED,
    CRITICAL), every bot leg exits KILL_SWITCH, an entry that fills later exits at once; ACK_HALT is refused until
    flat is verified."""
    make_live(rig)
    a = rig.open_leg()
    pk_b = rig.intent("NSE:INFY")
    rig.place_ok(pk_b)
    rig.exit_loop.act = False
    k = add_command(rig.cli, "KILL", rig.clock.now_wall(), confirm="KILL")
    rig.tick(1.0)
    assert rig.ack("CLI", k)["status"] == "ACCEPTED"
    (kill,) = rig.halts("KILL")
    assert kill.clears.value == "ACK" and kill.operator
    row = rig.store.strategy_mode(STRAT)
    assert row["mode"] == "PAPER" and row["demoted_reason"] == "KILL"
    assert rig.store.trial(STRAT, VERSION)["state"] == "DEMOTED"
    assert rig.events("LIVE_DEMOTED") and rig.alerts("LIVE_DEMOTED", "CRITICAL") and rig.alerts("KILL", "CRITICAL")
    assert rig.leg(a.leg_id).exit_reason == "KILL_SWITCH"
    assert not rig.eng.live_preconditions(STRAT).unlocked

    # the working entry fills after the KILL: it opens and exits at once with KILL_SWITCH
    rig.book(43.00, 43.20, INST2)
    rig.fill(pk_b, 42.20)
    b = rig.eng.state.legs[rig.eng.state.orders[pk_b].leg_id]
    assert b.state is LegState.EXITING and b.exit_reason == "KILL_SWITCH"

    ack1 = add_command(rig.cli, "ACK_HALT", rig.clock.now_wall(), args={"halt_id": kill.halt_id, "note": "x"})
    rig.tick(1.0)
    assert rig.ack("CLI", ack1)["status"] == "REJECTED" and "flat" in rig.ack("CLI", ack1)["result"]

    rig.exit_loop.act = True
    for _ in range(12):
        rig.book(42.30, 42.50)
        rig.book(43.00, 43.20, INST2)
        out = rig.tick(1.0)
        for p in [c for c in out if isinstance(c, Place)]:
            rig.fill(p.order_pk, p.req.limit_px)
    assert rig.ack("CLI", k)["status"] == "DONE"
    ack2 = add_command(rig.cli, "ACK_HALT", rig.clock.now_wall(), args={"halt_id": kill.halt_id, "note": "ok"})
    rig.tick(1.0)
    assert rig.ack("CLI", ack2)["status"] == "DONE" and not rig.halts("KILL")


def test_eod_timeline(tmp_path, core):
    """15:15 flatten, 15:22 backstop (CRITICAL when not flat + recon), 15:25 STOP_SENDING, 15:27/15:31 recon,
    15:33 tradebooks + contract notes, 15:36 bar check, 15:38 G1 / report / exports, 15:40 Exit(0) with the PHASE DONE
    event; each step once, also across a reload."""
    rig = Rig(tmp_path, start="2026-10-12T15:13:30+05:30", instruments=True, act=False)
    rig.tick()
    a = rig.open_leg()
    rig.run(BarsClosed((Bar("NSE:RELIANCE", ist_datetime(S, "09:15"), 2900.0, 2950.0, 2890.0, 2940.0, 50_000, True,
                            "LIVE"),)))
    out = rig.until("15:15:00", step=1.0, before=lambda: rig.book(42.30, 42.50))
    assert rig.leg(a.leg_id).state is LegState.EXITING and rig.leg(a.leg_id).exit_reason == "EOD_FLATTEN"
    out = rig.until("15:22:00", step=5.0)
    assert rig.alerts("NOT_FLAT", "CRITICAL")
    assert {c.account for c in out if isinstance(c, FetchPositions)} == set(Account)
    rig.until("15:25:00", step=5.0)
    assert len(rig.alerts("NOT_FLAT", "CRITICAL")) == 2                       # backstop + STOP_SENDING
    out = rig.until("15:33:00", step=5.0)
    # PAPER and SHADOW always; LIVE only when the bot filled LIVE orders today (none here)
    assert {c.account for c in out if isinstance(c, FetchTradebook)} == {Account.PAPER, Account.SHADOW}
    (truth,) = [e for e in rig.events("EOD_TRUTH_CHECK") if e["payload"]["account"] == "PAPER"]
    assert truth["payload"]["ok"] is True
    assert any(isinstance(c, ContractNote) and c.account is Account.PAPER for c in out)
    rig.eng = Engine.load(rig.store, rig.ctx)                                   # a reload does not repeat a step
    out = rig.until("15:36:00", step=5.0)
    assert not any(isinstance(c, FetchTradebook) for c in out)
    hists = {c.symbol: c for c in out if isinstance(c, FetchHistorical)}
    assert set(hists) == {"NSE:RELIANCE", "NSE:INFY"}                           # fired + the seeded sample
    assert hists["NSE:RELIANCE"].frm == ist_datetime(S, "09:15")
    rig.run(HistoricalResult(hists["NSE:RELIANCE"].request_id, "NSE:RELIANCE", True,
                             (Bar("NSE:RELIANCE", ist_datetime(S, "09:15"), 2900.0, 2951.0, 2889.0, 2940.5, 50_500,
                                  True, "HIST"),)))
    rig.run(HistoricalResult(hists["NSE:INFY"].request_id, "NSE:INFY", False, ()))
    checks = {c["symbol"]: c["ok"] for c in rig.store.bar_checks(S)}
    assert checks == {"NSE:RELIANCE": 1, "NSE:INFY": 0}
    rig.until("15:38:00", step=5.0)
    assert rig.store.g1_reports(strategy=STRAT, kind="PROGRESS")
    rig.tick()                                                                   # files are written after the commit
    assert (tmp_path / "reports" / f"{S}.md").is_file()
    assert (tmp_path / "exports" / "events" / f"{S}.jsonl.gz").is_file()
    assert gzip.decompress((tmp_path / "exports" / "trades" / f"{S}.jsonl.gz").read_bytes()) == b""
    assert (tmp_path / "pub" / "trades_90d.json").is_file() and (tmp_path / "pub" / "g1.json").is_file()
    assert rig.alerts("DAILY_REPORT")
    out = rig.until("15:40:00", step=5.0)
    assert [c.code for c in out if isinstance(c, Exit)] == [0]
    steps = [e["payload"]["step"] for e in rig.events("PHASE") if "step" in e["payload"]]
    assert steps == ["FLATTEN", "BACKSTOP", "STOP_SENDING", "RECON_1", "RECON_2", "TRUTH", "BAR_CHECK", "EOD_TASKS",
                     "DONE"]
    assert rig.events("PHASE")[-1]["payload"]["phase"] == "DONE" and rig.eng.phase == "DONE"
    assert not [c for c in rig.until("15:41:00", step=5.0) if isinstance(c, Exit)]


def test_selftest_gates_live(tmp_path, core):
    """§10.8: with a LIVE strategy and ALLOW_LIVE, LIVE:_selftest sends one non-filling BUY IOC (1 lot of the NIFTY
    ATM CE at floor_tick(bid x 0.97)) at 09:15:30; CANCELLED with 0 filled inside 5 s, with its tag back in a WS
    order update and in orders() (a LookupTag), passes it. Until then G13 refuses LIVE entries and I14 forbids a
    LIVE ENTRY order."""
    rig = Rig(tmp_path, start="2026-10-12T09:15:00+05:30", allow_live=True, instruments=True)
    rig.tick()
    make_live(rig)
    rig.until("09:15:29", step=1.0)
    ctx = rig.eng.gate_context(LIVE_BOOK)
    assert risk.gate_live(ctx) == risk.Refusal("LIVE", "NO_SELFTEST_TODAY")
    st = _live_structure()
    assert not rig.eng.live_allowed(st, Role.ENTRY) and rig.eng.live_allowed(st, Role.EXIT)

    spot = Quote(NIFTY_SPOT, 256265, (), (), 25010.0, None, None, None, rig.clock.mono(), rig.clock.now_wall(), "WS")
    out = rig.until("09:15:30", before=lambda: rig.quotes.put(dataclasses.replace(
        spot, recv_mono=rig.clock.mono(), recv_wall=rig.clock.now_wall())))
    (fq,) = [c for c in out if isinstance(c, FetchQuotes)]
    assert fq.symbols == ("NFO:NIFTY26OCT25000CE",) and fq.priority == Priority.ENTRY_LIVE
    opt = rig.inst_store.by_symbol(fq.symbols[0])
    rig.book(120.00, 120.50, opt)
    out = rig.run(QuoteResult(fq.request_id, True, fq.symbols))
    (place,) = [c for c in out if isinstance(c, Place)]
    assert place.account is Account.LIVE and place.req.role is Role.SELFTEST and place.req.side is Side.BUY
    assert place.req.qty == 65 and place.req.limit_px == 116.40 < 120.00
    rig.place_ok(place.order_pk)
    rig.clock.advance(1.2)
    out = rig.snap(place.order_pk, "CANCELLED", 0)                  # the WS update, carrying the tag
    assert rig.of(LookupTag, out) == [LookupTag(Account.LIVE, place.order_pk)]      # ... and orders()?
    assert not [e for e in rig.events("SELFTEST") if e["payload"]["outcome"] == "PASSED"]
    rig.clock.advance(0.3)
    o = rig.eng.state.orders[place.order_pk]
    rig.run(LookupResult(Account.LIVE, place.order_pk, True, BrokerOrder(
        Account.LIVE, o.broker_order_id, place.order_pk, opt.symbol, opt.tradingsymbol, "NFO", "MIS", Side.BUY,
        "LIMIT", "IOC", 65, 0, None, "CANCELLED", "CANCELLED", None, rig.clock.now_wall(), None, "API")))
    (passed,) = [e for e in rig.events("SELFTEST") if e["payload"]["outcome"] == "PASSED"]
    assert passed["payload"]["latency_ms"] == 1200
    assert "tag came back in a WS order update and in orders()" in rig.alerts("SELFTEST")[-1].text
    assert rig.eng.live_preconditions(STRAT).selftest_passed
    assert risk.gate_live(rig.eng.gate_context(LIVE_BOOK)) is None
    assert rig.eng.live_allowed(st, Role.ENTRY)
    st_self = rig.eng.state.structures[rig.eng.state.orders[place.order_pk].structure_id]
    assert st_self.book == SELFTEST_BOOK and st_self.state is StructState.VOID
    # passed is a DB fact: a reload keeps it, and no second self-test runs at 09:17
    rig.eng = Engine.load(rig.store, rig.ctx)
    assert rig.eng.live_preconditions(STRAT).selftest_passed
    assert not [c for c in rig.until("09:17:30", step=5.0) if isinstance(c, (Place, FetchQuotes))]


def _live_structure() -> Structure:
    """A LIVE:<s> structure, only for asking live_allowed() about it."""
    it = Intent("ilive0000001", STRAT, VERSION, "NSE:RELIANCE", Direction.UP, StructureType.LONG_CE, StrikePref.ATM,
                EXPIRY_RULE_NEAREST, 2900.0, Recheck(0.0, 1e9), 10.0, 0.0, ist_datetime(S, "09:20"), {})
    return Structure(0, S, LIVE_BOOK, Account.LIVE, Policy.FULL, STRAT, VERSION, it, StructureType.LONG_CE,
                     "NSE:RELIANCE", StructState.RESERVED, [], False, None, None, None, None, None, None, None, None,
                     ist_datetime(S, "09:20"))


def test_invariant_leg_anomaly(rig):
    """A leg-scope violation sends that leg to EXITING(ANOMALY_EXIT) (an entering leg exits as soon as it opens)
    with a CRITICAL alert and no halt; a global one raises HALT(INVARIANT, ACK). A persistent state violation is
    raised once, then known, so later events are not rolled back for ever."""
    a = rig.open_leg()
    rig.book(42.30, 42.50)
    rig.exit_loop.act = False
    with rig.store.tx():
        out = rig.eng.on_invariant_violation(InvariantViolation("I3", "oversold", scope=f"LEG:{a.leg_id}"))
    assert rig.leg(a.leg_id).state is LegState.EXITING and rig.leg(a.leg_id).exit_reason == "ANOMALY_EXIT"
    assert any(isinstance(c, Alert) and c.severity == "CRITICAL" and c.code == "INVARIANT" for c in out)
    assert not rig.halts("INVARIANT")
    (ev,) = rig.events("INVARIANT_VIOLATION")
    assert ev["payload"]["handled"] and ev["leg_id"] == a.leg_id

    pk_b = rig.intent("NSE:INFY")                                   # entering: the reason is pre-set and persisted
    b_id = rig.eng.state.orders[pk_b].leg_id
    with rig.store.tx():
        rig.eng.on_invariant_violation(InvariantViolation("I7", "x", scope=f"LEG:{b_id}"))
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.book(42.10, 42.30, INST2)
    rig.fill(pk_b, 42.20)
    assert rig.leg(b_id).state is LegState.EXITING and rig.leg(b_id).exit_reason == "ANOMALY_EXIT"

    with rig.store.tx():
        rig.eng.on_invariant_violation(InvariantViolation("I5", "two structures", scope="GLOBAL"))
    (inv,) = rig.halts("INVARIANT")
    assert inv.scope == "GLOBAL" and inv.clears.value == "ACK"

    # a persistent condition is raised once, then known (later events are not rolled back for ever); the same
    # condition getting worse is new and raised again
    rig.eng.state.orders[pk_b].role = Role.EXIT                    # an ENTRY that reads as an EXIT: oversold leg
    raised = []
    for _ in range(5):
        try:
            rig.eng.check_invariants()
            break
        except InvariantViolation as iv:
            key = invariants.known_key(iv)
            assert key not in raised and key not in rig.eng.state.known_violations
            raised.append(key)
            rig.eng.state.known_violations.add(key)
    assert [k for k in raised if k[:2] == ("I3", f"LEG:{b_id}")]
    rig.eng.check_invariants()                                     # every condition handled: no raise
    rig.eng.state.orders[pk_b].filled_qty = LOT                     # unchanged: still known
    rig.eng.check_invariants()
    rig.eng.state.orders[rig.gateway.placed[0]].role = Role.EXIT   # leg A oversold too: a new I3
    with pytest.raises(InvariantViolation) as again:
        rig.eng.check_invariants()
    assert again.value.code in ("I3", "I7") and again.value.scope == f"LEG:{a.leg_id}"


# ---------------------------------------------------------------------------------------------------------------
# More engine behaviour
# ---------------------------------------------------------------------------------------------------------------
def test_selftest_failures_disable_live_today(tmp_path, core):
    """A failed attempt warns and retries at 09:17; a second failure is HALT(LIVE_DISABLED_TODAY, ACCOUNT:LIVE) +
    CRITICAL. Without ALLOW_LIVE no self-test is sent at all."""
    rig = Rig(tmp_path, start="2026-10-12T09:15:00+05:30", allow_live=True, instruments=True)
    rig.tick()
    make_live(rig)
    opt = rig.inst_store.by_symbol("NFO:NIFTY26OCT25000CE")

    def spot_and_book():
        rig.quotes.put(Quote(NIFTY_SPOT, 256265, (), (), 25010.0, None, None, None, rig.clock.mono(),
                             rig.clock.now_wall(), "WS"))
        rig.book(120.0, 120.5, opt)

    for hhmmss in ("09:15:30", "09:17:00"):
        out = rig.until(hhmmss, step=1.5, before=spot_and_book)
        (fq,) = [c for c in out if isinstance(c, FetchQuotes)]
        rig.book(120.0, 120.5, opt)                                   # the REST answer to that FetchQuotes
        (place,) = [c for c in rig.run(QuoteResult(fq.request_id, True, fq.symbols)) if isinstance(c, Place)]
        rig.run(PlaceResult(Account.LIVE, place.order_pk, "REJECTED", None, "InputException", "Order rejected"))
    assert len([e for e in rig.events("SELFTEST") if e["payload"]["outcome"] == "FAILED"]) == 2
    assert rig.alerts("SELFTEST", "WARN")
    (h,) = rig.halts("LIVE_DISABLED_TODAY")
    assert h.scope == "ACCOUNT:LIVE" and rig.alerts("HALT_LIVE_DISABLED_TODAY", "CRITICAL")
    assert not rig.eng.live_preconditions(STRAT).selftest_passed
    assert not [c for c in rig.until("09:20:00", step=5.0, before=spot_and_book) if isinstance(c, Place)]

    rig2 = Rig(tmp_path / "nolive", start="2026-10-12T09:15:00+05:30", allow_live=False, instruments=True)
    rig2.tick()
    make_live(rig2)
    assert not [c for c in rig2.until("09:20:00", step=5.0) if isinstance(c, (Place, FetchQuotes))]
    assert risk.gate_live(rig2.eng.gate_context(LIVE_BOOK)) == risk.Refusal("LIVE", "ALLOW_LIVE_OFF")


def test_order_invariants_always_raise(rig):
    """An order created after STOP_SENDING, or a LIVE ENTRY without the keys, is rolled back every time (the
    invariant is never 'known'), so its Place never leaves the core."""
    a = rig.open_leg()
    rig.exit_loop.act = False
    rig.book(42.30, 42.50)
    leg = rig.leg(a.leg_id)
    st = rig.struct(a.leg_id)
    with rig.store.tx():
        positions.begin_exit(rig.store, st, leg, "HARD_SL", "PROTECTIVE", now_mono=rig.clock.mono(),
                             now_wall=rig.clock.now_wall())
    rig.clock.advance_to("15:25:01")
    for _ in range(2):
        with pytest.raises(InvariantViolation) as err:
            with rig.store.tx():
                rig.eng.handle(Timer(rig.clock.now_wall(), rig.clock.mono()))
                q = rig.eng.quotes.last(INST.symbol)
                dq = DecisionQuote(q.bid, q.ask, q.mid(), 0, "WS", None, q.bid)
                rig.eng.create_order(leg, Role.EXIT, LOT, 42.25, decision=dq, band_idx=0)
                rig.eng.check_invariants()
        assert err.value.code == "STOP_SENDING"
        rig.eng = Engine.load(rig.store, rig.ctx)
        leg = rig.leg(a.leg_id)
        rig.eng.state.known_violations.add(invariants.known_key(err.value))     # order checks ignore it
    assert rig.store.scalar("SELECT COUNT(*) FROM orders WHERE role='EXIT'") == 0


def test_daily_stop_trip_and_rollover(tmp_path, core):
    """The daily stop on a FULL book (two filled entries at a deep bid loss), forced exits, the NEXT_SESSION clear."""
    rig = Rig(tmp_path, act=False)
    rig.tick()
    for name, tok in (("SBIN", 34567), ("TCS", 45678)):
        inst = Instrument(f"NFO:{name}26OCT800CE", tok, f"{name}26OCT800CE", "NFO", name, f"NSE:{name}", "CE", 800.0,
                          date(2026, 10, 27), LOT, TICK)
        rig.gateway.inst[f"NSE:{name}"] = inst
    legs = []
    for name in ("SBIN", "TCS"):
        inst = rig.gateway.inst[f"NSE:{name}"]
        rig.book(41.90, 42.15, inst)
        pk = rig.intent(f"NSE:{name}")
        rig.book(42.10, 42.30, inst)
        rig.fill(pk, 42.20)
        legs.append(rig.eng.state.orders[pk].leg_id)
    # pretend the book already lost Rs 9,500 today (a closed trade from earlier) by lowering the threshold instead:
    rig.ctx.settings = dataclasses.replace(DEFAULTS, DAILY_STOP_RS=1000.0)
    rig.eng = Engine.load(rig.store, rig.ctx)
    for _ in range(6):
        for name in ("SBIN", "TCS"):
            rig.book(30.00, 30.10, rig.gateway.inst[f"NSE:{name}"])
        rig.tick()
    (h,) = rig.halts("DAILY_STOP")
    assert h.scope == f"BOOK:{PAPER_BOOK}" and h.clears.value == "NEXT_SESSION"
    assert rig.alerts("DAILY_STOP", "WARN")
    assert all(rig.leg(lid).state is LegState.EXITING for lid in legs)
    rig.eng = Engine.load(rig.store, rig.ctx)                       # sticky across a reload
    assert rig.eng.halted("DAILY_STOP", f"BOOK:{PAPER_BOOK}")
    rig.clock.advance_to(datetime(2026, 10, 13, 9, 0, tzinfo=rig.clock.now_wall().tzinfo))
    rig.tick()
    assert not rig.halts("DAILY_STOP")


def test_startup_rollover_modes_and_impaired(tmp_path, core):
    """The first event of a later session clears the NEXT_SESSION halts of earlier sessions (ACK halts stay); a
    heartbeat gap over the entry window marks every G1 strategy IMPAIRED; SET_MODE OFF is immediate (IMPAIRED),
    PAPER waits for the next session, LIVE is refused without ALLOW_LIVE; pending modes apply at the rollover."""
    rig = Rig(tmp_path, start="2026-10-09T15:00:00+05:30")      # Friday
    rig.tick()
    with rig.store.tx():
        rig.eng.add_halt("CONTRACT_BLOCKED", "SYMBOL:NFO:X", "blocked")
        rig.eng.add_halt("ORDER_STUCK", f"BOOK:{PAPER_BOOK}", "stuck")
    rig.store.close()
    rig2 = Rig(tmp_path, clock=FakeClock("2026-10-12T10:00:00+05:30"))     # Monday 10:00: 09:20-10:00 was missed
    rig2.tick()
    assert [h.kind for h in rig2.halts()] == ["ORDER_STUCK"]
    assert any("heartbeat gap" in f["detail"] for f in rig2.store.session_flags(strategy=STRAT, session=S))
    c1 = add_command(rig2.cli, "SET_MODE", rig2.clock.now_wall(), args={"strategy": STRAT, "mode": "OFF"})
    c2 = add_command(rig2.cli, "SET_MODE", rig2.clock.now_wall(), args={"strategy": STRAT, "mode": "PAPER"})
    c3 = add_command(rig2.cli, "SET_MODE", rig2.clock.now_wall(), args={"strategy": STRAT, "mode": "LIVE",
                                                                         "confirm": "h" * 64})
    c4 = add_command(rig2.cli, "SET_MODE", rig2.clock.now_wall(), args={"strategy": "nope", "mode": "PAPER"})
    rig2.tick(1.0)
    assert rig2.ack("CLI", c1)["status"] == "DONE" and rig2.ack("CLI", c2)["status"] == "DONE"
    assert rig2.ack("CLI", c3)["status"] == "REJECTED" and "ALLOW_LIVE" in rig2.ack("CLI", c3)["result"]
    assert rig2.ack("CLI", c4)["status"] == "REJECTED"
    row = rig2.store.strategy_mode(STRAT)
    assert row["mode"] == "OFF" and row["pending_mode"] == "PAPER"
    assert rig2.store.session_flags(strategy=STRAT, session=S)          # IMPAIRED (flagged once, first reason kept)
    assert [e["payload"]["to"] for e in rig2.events("MODE_CHANGED") if "to" in e["payload"]][-1] == "OFF"
    rig2.clock.advance_to(datetime(2026, 10, 13, 9, 0, tzinfo=rig2.clock.now_wall().tzinfo))
    rig2.tick()
    row = rig2.store.strategy_mode(STRAT)
    assert row["mode"] == "PAPER" and row["pending_mode"] is None and row["changed_by"] == "session"


def test_set_mode_live_needs_both_keys(tmp_path, core):
    """Key 2 is accepted only with ALLOW_LIVE, the CERTIFIED hash and no GLOBAL ACK halt; it sets pending_mode
    LIVE and the unlock report; the next session makes it LIVE."""
    rig = Rig(tmp_path, allow_live=True)
    rig.tick()
    make_live(rig)
    with rig.store.tx():
        rig.store.update_row("strategy_modes", {"strategy": STRAT}, {"mode": "PAPER", "live_unlock_report_id": None})
    rig.eng.reload_modes()
    bad = add_command(rig.cli, "SET_MODE", rig.clock.now_wall(), args={"strategy": STRAT, "mode": "LIVE",
                                                                       "confirm": "0" * 64})
    good = add_command(rig.cli, "SET_MODE", rig.clock.now_wall(), args={"strategy": STRAT, "mode": "LIVE",
                                                                        "confirm": "h" * 64})
    rig.tick(1.0)
    assert rig.ack("CLI", bad)["status"] == "REJECTED" and "HASH_MISMATCH" in rig.ack("CLI", bad)["result"]
    assert rig.ack("CLI", good)["status"] == "DONE"
    row = rig.store.strategy_mode(STRAT)
    assert row["pending_mode"] == "LIVE" and row["live_unlock_report_id"] is not None and row["mode"] == "PAPER"
    rig.clock.advance_to(datetime(2026, 10, 13, 9, 0, tzinfo=rig.clock.now_wall().tzinfo))
    rig.tick()
    row = rig.store.strategy_mode(STRAT)
    assert row["mode"] == "LIVE" and row["live_since_session"] == "2026-10-13"


def test_recovery_ready_and_restart_void(tmp_path, core):
    """RecoveryStart reads every account; once planned, a RESERVED structure is VOID("RESTART"), an entry the
    broker shows filled opens, READY follows, and two reconcile rounds are scheduled."""
    rig = Rig(tmp_path)
    rig.tick()
    rig.gateway.fail_after_reserve = False
    rig.book(41.90, 42.15)
    pk = rig.intent()                                          # ENTERING (SUBMITTING)
    o = rig.eng.state.orders[pk]
    with rig.store.tx():                                       # and a lone RESERVED structure
        it = Intent("iorphan00001", STRAT, VERSION, "NSE:INFY", Direction.UP, StructureType.LONG_CE, StrikePref.ATM,
                    EXPIRY_RULE_NEAREST, 1500.0, Recheck(-math.inf, math.inf), 10.0, rig.clock.mono(),
                    rig.clock.now_wall(), {})
        rig.store.insert_intent(it, S)
        orphan = positions.reserve(rig.store, it, book=PAPER_BOOK, account=Account.PAPER, policy=Policy.FULL,
                                   now_mono=rig.clock.mono(), now_wall=rig.clock.now_wall())
    # restart: a new process; the broker shows the entry filled under its tag
    rig.broker_orders[Account.PAPER] = [BrokerOrder(Account.PAPER, "P261012000000001", pk, INST.symbol,
                                                    INST.tradingsymbol, "NFO", "MIS", Side.BUY, "LIMIT", "IOC", LOT,
                                                    LOT, 42.20, "COMPLETE", "COMPLETE", None, rig.clock.now_wall(),
                                                    None, "API")]
    rig.extra_nets[(Account.PAPER, INST.symbol)] = LOT
    rig.ctx = dataclasses.replace(rig.ctx, recovered=False, recovery_started=False, start_mono=rig.clock.mono())
    rig.eng = Engine.load(rig.store, rig.ctx)
    assert rig.eng.phase == "PREOPEN" and not rig.eng.recovery_done
    rig.extra_nets.clear()
    rig.run(RecoveryStart())
    assert rig.eng.recovery_done and rig.events("READY")
    assert rig.eng.state.structures[orphan.structure_id].state is StructState.VOID
    assert rig.eng.state.structures[orphan.structure_id].void_reason == "RESTART"
    assert rig.eng.state.orders[pk].state is OrderState.COMPLETE and rig.eng.state.legs[o.leg_id].state is LegState.OPEN
    assert all(bv.extra_round_at is not None for bv in rig.eng.state.broker.values())


def test_status_json_and_trades_pub(rig):
    """pub/status.json (written after the commit, every second) carries the DETAIL §4.7 blocks; a close publishes
    trades_today.json and trade/<id>.json; MTM is null when the bid is stale."""
    a = rig.open_leg()
    rig.book(42.30, 42.50)
    rig.tick(1.0)
    rig.tick(1.0)
    doc = json.loads((rig.tmp / "pub" / "status.json").read_text(encoding="utf-8"))
    assert {"ts", "session", "phase", "heartbeat_age_s", "allow_live", "entry_halts", "feed", "accounts", "books",
            "positions", "strategies", "pending_commands", "unacked_critical"} <= set(doc)
    (pos,) = doc["positions"]
    assert pos["symbol"] == INST.symbol and pos["qty"] == LOT and pos["mtm_net"] is not None
    assert doc["books"][PAPER_BOOK]["entries"] == 1 and doc["strategies"][0]["name"] == STRAT
    rig.tick(4.0)                                                   # the bid goes stale: MTM unknown (null)
    assert rig.eng.leg_mtm(rig.leg(a.leg_id)) is None
    # close it and look for the trade files
    with rig.store.tx():
        rig.eng.force_exit(rig.leg(a.leg_id), "OPERATOR_FLATTEN")
    for _ in range(10):
        rig.book(42.30, 42.50)
        out = rig.tick(1.0)
        for p in [c for c in out if isinstance(c, Place)]:
            rig.fill(p.order_pk, p.req.limit_px)
    sid = rig.struct(a.leg_id).structure_id
    assert rig.struct(a.leg_id).state is StructState.CLOSED
    rig.tick()
    today = json.loads((rig.tmp / "pub" / "trades_today.json").read_text(encoding="utf-8"))
    assert [t["structure_id"] for t in today["trades"]] == [sid]
    assert (rig.tmp / "pub" / "trade" / f"{sid}.json").is_file()
    assert rig.eng.state.tails and any(isinstance(c, Unsubscribe) for c in rig.tick(46 * 60))


def test_pub_write_failures_retry_and_a_load_rebuilds(rig, monkeypatch):
    """A failed pub/ write is a WARN, never a halt: every write pauses 5 s, a marked file is retried (at most 3
    failures in a row), and an Engine.load rebuilds the trade files a crash right after a close never wrote."""
    from titan2 import ledger
    a = rig.open_leg()
    with rig.store.tx():
        cmds = rig.eng.force_exit(rig.leg(a.leg_id), "OPERATOR_FLATTEN")
    for _ in range(10):
        for p in [c for c in cmds if isinstance(c, Place)]:
            rig.fill(p.order_pk, p.req.limit_px)
        rig.book(42.30, 42.50)
        cmds = rig.tick(1.0)
    sid = rig.struct(a.leg_id).structure_id
    assert rig.struct(a.leg_id).state is StructState.CLOSED
    pub = rig.tmp / "pub"
    real, calls = ledger.publish_acks, []

    def flaky(*args, **kw):
        calls.append(rig.clock.mono())
        if len(calls) <= 2:
            raise PermissionError(13, "Access is denied")
        return real(*args, **kw)

    monkeypatch.setattr(ledger, "publish_acks", flaky)
    (pub / "acks.json").unlink(missing_ok=True)
    rig.eng.mark_pub("acks")
    for _ in range(14):
        rig.tick(1.0)
    assert len(calls) == 3 and calls[1] - calls[0] >= 5.0 and calls[2] - calls[1] >= 5.0
    assert (pub / "acks.json").is_file() and not rig.halts()
    assert [a.severity for a in rig.alerts("PUB_WRITE")] == ["WARN", "WARN"]
    # a crash right after the close: the files are gone, the next engine rebuilds them from the ledger
    (pub / "trades_today.json").unlink()
    (pub / "trade" / f"{sid}.json").unlink()
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.tick(1.0)
    today = json.loads((pub / "trades_today.json").read_text(encoding="utf-8"))
    assert [t["structure_id"] for t in today["trades"]] == [sid] and (pub / "trade" / f"{sid}.json").is_file()


def test_recon_external_close_books_and_halts(rig):
    """Two post-fill positions reads >= 3 s apart, outside grace, showing a bot leg flat with a manual SELL in the
    orders book: CLOSED_EXTERNAL at the broker price, HALT(MISMATCH, ACCOUNT:PAPER), never a bot SELL."""
    a = rig.open_leg()
    rig.exit_loop.act = False
    rig.tick(6.0)
    manual = BrokerOrder(Account.PAPER, "M1", None, INST.symbol, INST.tradingsymbol, "NFO", "MIS", Side.SELL, "LIMIT",
                         "DAY", LOT, LOT, 43.00, "COMPLETE", "COMPLETE", None, rig.clock.now_wall(), None, "WEB")
    # today's orders as the broker shows them: the bot's own filled BUY and the manual SELL (recon books a drop only
    #   when the orders back it; a read missing the bot's own BUY proves nothing)
    e = next(o for o in a.leg_orders() if o.role is Role.ENTRY)
    bot_buy = BrokerOrder(Account.PAPER, e.broker_order_id, e.order_pk, INST.symbol, INST.tradingsymbol, "NFO", "MIS",
                          Side.BUY, "LIMIT", "IOC", e.qty, e.filled_qty, e.avg_fill_px, "COMPLETE", "COMPLETE", None,
                          rig.clock.now_wall(), None, "API")
    rig.broker_orders[Account.PAPER] = [bot_buy, manual]
    rig.extra_nets[(Account.PAPER, INST.symbol)] = -LOT
    for _ in range(15):
        rig.book(42.30, 42.50)
        rig.tick(1.0)
    st = rig.struct(a.leg_id)
    assert st.state is StructState.CLOSED_EXTERNAL
    trade = rig.store.trade(st.structure_id)
    assert trade["exit_reason"] == "CLOSED_EXTERNAL" and trade["exit_avg"] == 43.00
    assert [h.scope for h in rig.halts("MISMATCH")] == ["ACCOUNT:PAPER"]
    assert not rig.store.query("SELECT * FROM orders WHERE role='EXIT'")


def test_shutdown_cancels_entries_and_exits0(rig):
    """SIGTERM: entries stop, working ENTRY orders are cancelled, no flatten; Exit(0) once no bot order is in flight
    (or after the wait)."""
    a = rig.open_leg()
    pk_b = rig.intent("NSE:INFY")
    rig.place_ok(pk_b)
    out = rig.run(Shutdown("SIGTERM"))
    assert any(isinstance(c, Cancel) and c.order_pk == pk_b for c in out)
    assert rig.leg(a.leg_id).state is LegState.OPEN                 # never flattens
    assert not rig.of(Exit, out)
    placed = len(rig.gateway.placed)
    rig.run(IntentReceived(Intent("ilate0000001", STRAT, VERSION, "NSE:INFY", Direction.UP, StructureType.LONG_CE,
                                  StrikePref.ATM, EXPIRY_RULE_NEAREST, 1500.0, Recheck(-math.inf, math.inf), 10.0,
                                  rig.clock.mono(), rig.clock.now_wall(), {})))
    assert len(rig.gateway.placed) == placed                         # refused while stopping
    rig.run(CancelResult(Account.PAPER, pk_b, "OK"))
    out = rig.snap(pk_b, "CANCELLED", 0) + rig.tick()
    assert [c.code for c in out if isinstance(c, Exit)] == [0]


def test_selftest_refuses_a_stale_open_quote(tmp_path, core):
    """A self-test quote stamped before open + 30 s is a stale carry-over: no order, the attempt fails."""
    rig = Rig(tmp_path, start="2026-10-12T09:15:00+05:30", allow_live=True, instruments=True)
    rig.tick()
    make_live(rig)
    spot = lambda: rig.quotes.put(Quote(NIFTY_SPOT, 256265, (), (), 25010.0, None, None, None, rig.clock.mono(),
                                        rig.clock.now_wall(), "WS"))
    out = rig.until("09:15:30", step=1.0, before=spot)
    (fq,) = [c for c in out if isinstance(c, FetchQuotes)]
    opt = rig.inst_store.by_symbol(fq.symbols[0])
    q = rig.book(120.0, 120.5, opt)
    rig.quotes.put(dataclasses.replace(q, exch_wall=ist_datetime(S, "09:15:05")))       # same receipt: replaces
    out = rig.run(QuoteResult(fq.request_id, True, fq.symbols))
    assert not [c for c in out if isinstance(c, Place)]
    (failed,) = [e for e in rig.events("SELFTEST") if e["payload"]["outcome"] == "FAILED"]
    assert "stale carry-over" in failed["payload"]["detail"]


def test_prep_selfcheck_token_and_feed_duties(tmp_path, core):
    """After recovery: daily prep, then the prestart self-check and StartSignals once every prep kind answered;
    NO_INSTRUMENTS while the dump fails; HALT(TOKEN) re-checks every 30 s (a NEW working token restarts with exit 75,
    a working one clears the halt); a degraded feed halts STALE_FEED until 60 s healthy; a WS that gave up
    reconnecting restarts the process after 60 s."""
    rig = Rig(tmp_path)
    health = {"h": FeedHealth(True, rig.clock.mono(), rig.clock.mono(), 0, False)}
    rig.ctx.feed_health = lambda: health["h"]
    out = rig.tick()
    assert sorted(c.kind for c in out if isinstance(c, Prep)) == ["BAN_LIST", "INSTRUMENTS", "PREV_BARS"]
    rig.run(PrepResult("INSTRUMENTS", False, "dump is short of NIFTY expiries"))
    assert rig.halts("NO_INSTRUMENTS") and rig.alerts("HALT_NO_INSTRUMENTS", "CRITICAL")
    out = rig.run(PrepResult("BAN_LIST", True, "12 names")) + rig.run(PrepResult("PREV_BARS", True, "ok"))
    assert any(isinstance(c, RunSelfcheck) for c in out) and any(isinstance(c, StartSignals) for c in out)
    assert not [c for c in rig.tick(10.0) if isinstance(c, Prep)]          # retried after 60 s, not before
    rig.inst_store = instrument_store()
    out = rig.tick(51.0)
    assert [c.kind for c in out if isinstance(c, Prep)] == ["INSTRUMENTS"]
    rig.run(PrepResult("INSTRUMENTS", True, "loaded"))
    assert not rig.halts("NO_INSTRUMENTS") and rig.eng.instruments_loaded

    class Report:                                                      # the selfcheck.Report surface the engine uses
        ok, fatal = False, False

        def halts(self):
            return [HaltSpec("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO, "ntp not synced")]

        def alert_commands(self):
            return [Alert("CRITICAL", "SELFCHECK_NTP", "ntp", "selfcheck:ntp")]

        def demotions(self):
            return []

    rig.run(SelfcheckResult("prestart", False, Report()))
    assert rig.halts("CLOCK_UNSYNCED") and rig.alerts("SELFCHECK_NTP", "CRITICAL")
    assert rig.eng.gate_context(PAPER_BOOK).clock_synced is False

    with rig.store.tx():
        rig.eng.add_halt("TOKEN", "GLOBAL", "TokenException on an order")
    out = rig.tick()
    assert [c.kind for c in out if isinstance(c, Prep)] == ["TOKEN"]
    assert not [c for c in rig.tick(10.0) if isinstance(c, Prep)]       # one re-check in flight
    rig.run(PrepResult("TOKEN", False, "profile() failed"))
    assert not [c for c in rig.tick(10.0) if isinstance(c, Prep)]
    out = rig.tick(21.0)
    assert [c.kind for c in out if isinstance(c, Prep)] == ["TOKEN"]
    rig.run(PrepResult("TOKEN", True, "SAME token works"))
    assert not rig.halts("TOKEN")
    with rig.store.tx():
        rig.eng.add_halt("TOKEN", "GLOBAL", "again")
    rig.tick(30.0)
    out = rig.run(PrepResult("TOKEN", True, "NEW token from the 08:30 login works"))
    assert [c.code for c in out if isinstance(c, Exit)] == [75]

    rig2 = Rig(tmp_path / "feed")
    h2 = {"h": FeedHealth(True, rig2.clock.mono(), rig2.clock.mono(), 0, True)}
    rig2.ctx.feed_health = lambda: h2["h"]
    rig2.tick()
    rig2.run(FeedState(True, False, True, "SIGNAL_Q_OVERFLOW"))
    assert rig2.halts("STALE_FEED")
    h2["h"] = FeedHealth(True, rig2.clock.mono(), rig2.clock.mono(), 0, False)
    rig2.tick(30.0)
    rig2.tick(31.0)
    assert rig2.halts("STALE_FEED")                                      # 60 s healthy counts from the first Timer
    rig2.tick(30.0)
    assert not rig2.halts("STALE_FEED")
    h2["h"] = FeedHealth(False, None, rig2.clock.mono(), 1, False)
    out = rig2.run(FeedState(False, False, False, "NORECONNECT"))
    assert rig2.alerts("FEED_DEAD", "CRITICAL")
    assert not [c for c in rig2.tick(30.0) if isinstance(c, Exit)]
    assert [c.code for c in rig2.tick(31.0) if isinstance(c, Exit)] == [75]


def test_live_recon_problem_halts_globally_and_demotes(rig):
    """§10.9: a confirmed FOREIGN / EXCESS / SHORT / external close on the LIVE account raises the account's own halt,
    a GLOBAL HALT(INVARIANT) (entries stop everywhere) and demotes every LIVE strategy."""
    make_live(rig)
    rig.extra_nets[(Account.LIVE, "NFO:TCS26OCT4000CE")] = 75           # a manual trade in the LIVE account
    for _ in range(3):
        rig.tick(11.0)
    assert [h.scope for h in rig.halts("UNKNOWN_POSITION")] == ["ACCOUNT:LIVE"]
    (inv,) = rig.halts("INVARIANT")
    assert inv.scope == "GLOBAL" and "LIVE reconciliation" in inv.reason
    assert rig.store.strategy_mode(STRAT)["mode"] == "PAPER"
    assert rig.store.trial(STRAT, VERSION)["state"] == "DEMOTED"
    assert rig.alerts("LIVE_DEMOTED", "CRITICAL")
    # an exact ACK_FOREIGN clears the account halt (the GLOBAL invariant halt needs its own ACK)
    ack = add_command(rig.cli, "ACK_FOREIGN", rig.clock.now_wall(),
                      args={"symbol": "TCS26OCT4000CE", "product": "MIS", "qty": 75})
    rig.tick(1.0)
    assert rig.ack("CLI", ack)["status"] == "DONE" and not rig.halts("UNKNOWN_POSITION")
    assert rig.halts("INVARIANT")


# ---------------------------------------------------------------------------------------------------------------
# Integration fixes after wave 4 (handlers, lost reads, covers on any leg; the placebo is the gateway's, F-18)
# ---------------------------------------------------------------------------------------------------------------
class SpyHandler(SingleLegHandler):
    """The phase-1 handler, recording what the engine asks it (§14: exit evaluation goes through the handler)."""

    def __init__(self) -> None:
        self.evaluated: list[tuple[int, str | None]] = []
        self.sequenced: list[int] = []

    def evaluate(self, st, quotes, now_mono, now_wall, forced, ctx):
        self.evaluated.append((st.structure_id, forced))
        return super().evaluate(st, quotes, now_mono, now_wall, forced, ctx)

    def exit_sequence(self, st):
        self.sequenced.append(st.structure_id)
        return super().exit_sequence(st)


def test_exit_evaluation_and_exit_order_go_through_the_handler(rig):
    """§14: an OPEN structure is evaluated by its StructureHandler (forced reasons included) and its exiting legs are
    stepped in the handler's exit_sequence; a handler without evaluate (here the Rig's placeholders) falls back to
    exits.decide, so a missing plugin never blocks an exit."""
    a = rig.open_leg()                                           # object() placeholders: exits.decide directly
    assert rig.leg(a.leg_id).peak_rb is not None
    spy = SpyHandler()
    rig.ctx.handlers = {StructureType.LONG_CE: spy, StructureType.LONG_PE: spy}
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.exit_loop.act = False
    rig.book(42.60, 42.80)
    rig.tick()
    sid = rig.struct(a.leg_id).structure_id
    assert spy.evaluated == [(sid, None)] and rig.leg(a.leg_id).state is LegState.OPEN
    with rig.store.tx():
        rig.eng.add_halt("FLATTEN", "GLOBAL", "test", operator=True)
    rig.book(42.60, 42.80)
    rig.tick()
    assert spy.evaluated[-1] == (sid, "OPERATOR_FLATTEN")
    assert rig.leg(a.leg_id).state is LegState.EXITING and rig.leg(a.leg_id).exit_reason == "OPERATOR_FLATTEN"
    rig.tick()
    assert sid in spy.sequenced

    # the order rule itself: per structure (in first-leg order), the legs it names in its order, then the rest
    leg = rig.leg(a.leg_id)
    other, extra = dataclasses.replace(leg, leg_id=leg.leg_id + 100), dataclasses.replace(leg, leg_id=leg.leg_id + 200)

    class Reverse:
        def exit_sequence(self, st):
            return [extra.leg_id, leg.leg_id]

    rig.eng.handlers = {StructureType.LONG_CE: Reverse()}
    got = rig.eng._exit_sequence([leg, other, extra])
    assert [lg.leg_id for lg in got] == [extra.leg_id, leg.leg_id, other.leg_id]


MARGINS_WAIT_S = 31.0                                            # > the 30 s margins cadence


def test_lost_reads_stop_counting_as_in_flight(rig):
    """The lanes answer every read; one that is never answered stops counting as in flight after READ_LOST_S
    (READ_LOST event + WARN). Without that, request_positions/request_orders would dedupe for the rest of the day
    and the exit cap and verify-flat would never get an EXIT-priority read again."""
    rig.auto_reads = False
    out = rig.tick(11.0)                                         # a recon round nobody answers: both reads lost
    assert Account.PAPER in {c.account for c in out if isinstance(c, FetchPositions)}
    bv = rig.eng.state.broker[Account.PAPER]
    assert bv.pos_pending == 1 and bv.ords_pending == 1
    with rig.store.tx():
        assert rig.eng.request_positions(Account.PAPER) == []   # deduped while in flight
    out = rig.tick(READ_LOST_S - 1.0)                           # the next round goes out, unanswered for now
    assert not rig.events("READ_LOST") and (bv.pos_pending, bv.ords_pending) == (2, 2)
    out += rig.tick(1.0)                                         # the first round's reads are lost: not counted
    lost = {(e["payload"]["account"], e["payload"]["kind"]) for e in rig.events("READ_LOST")}
    assert {("PAPER", "POSITIONS"), ("PAPER", "ORDERS")} <= lost and (bv.pos_pending, bv.ords_pending) == (1, 1)
    assert rig.alerts("READ_LOST", "WARN")
    rig.answer(out)                                              # the second round is answered: nothing in flight
    assert (bv.pos_pending, bv.ords_pending, bv.pos_sent, bv.ords_sent) == (0, 0, [], [])
    with rig.store.tx():
        assert rig.eng.request_positions(Account.PAPER) == [FetchPositions(Account.PAPER, int(Priority.EXIT))]

    # a lost margins read: G21 would refuse every entry for the rest of the day; it is asked again instead
    rig.tick(MARGINS_WAIT_S)
    assert bv.margins_pending
    out = rig.tick(READ_LOST_S)                                  # lost, and asked again on the same Timer
    assert ("PAPER", "MARGINS") in {(e["payload"]["account"], e["payload"]["kind"]) for e in rig.events("READ_LOST")}
    assert [c for c in out if isinstance(c, FetchMargins) and c.account is Account.PAPER] and bv.margins_pending


def test_cover_on_a_closed_leg_runs_on_every_timer(tmp_path, core):
    """A bot-explained short found after its leg closed (the broker shows the exit sold 3 lots; the DB knows 1): recon
    plans a COVER on that closed leg, and the real exit loop runs it on the Timer like any exit ladder. The second
    COVER goes out a Timer after the first one's partial fill, not on recon's next confirmed sighting."""
    rig = Rig(tmp_path)
    rig.ctx.exit_loop = None                                     # the real titan2.core.exit_loop
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.tick()
    a = rig.open_leg()
    with rig.store.tx():
        rig.eng.force_exit(rig.leg(a.leg_id), "OPERATOR_FLATTEN")
    for _ in range(40):
        rig.book(42.30, 42.50)
        for p in [c for c in rig.tick(0.5) if isinstance(c, Place)]:
            rig.fill(p.order_pk, p.req.limit_px)
        if rig.struct(a.leg_id).state is StructState.CLOSED:
            break
    assert rig.struct(a.leg_id).state is StructState.CLOSED
    entry, exit_ = (next(o for o in rig.leg(a.leg_id).leg_orders() if o.role is r) for r in (Role.ENTRY, Role.EXIT))

    def broker(o, side, filled):
        return BrokerOrder(Account.PAPER, o.broker_order_id, o.order_pk, INST.symbol, INST.tradingsymbol, "NFO", "MIS",
                           side, "LIMIT", "IOC", o.qty, filled, o.avg_fill_px, "COMPLETE", "COMPLETE", None,
                           rig.clock.now_wall(), None, "API")

    rig.broker_orders[Account.PAPER] = [broker(entry, Side.BUY, LOT), broker(exit_, Side.SELL, 3 * LOT)]
    rig.extra_nets[(Account.PAPER, INST.symbol)] = -2 * LOT       # the broker is 2 lots short
    first, seen = None, len(rig.cmds)
    for _ in range(120):                                         # recon plans it while handling a broker read
        rig.book(42.30, 42.50)
        rig.tick(0.5)
        covers = [c for c in rig.cmds[seen:] if isinstance(c, Place) and c.req.role is Role.COVER]
        if covers:
            (first,) = covers
            break
    assert first is not None and (first.req.side, first.req.qty) == (Side.BUY, 2 * LOT)
    assert rig.halts("SHORT_DETECTED")
    assert rig.eng.state.orders[first.order_pk].leg_id == a.leg_id      # it hangs off the closed leg
    rig.fill(first.order_pk, first.req.limit_px, qty=LOT)              # IOC: half of it filled
    rig.clock.advance(0.1)
    rig.book(42.30, 42.50)
    (second,) = [c for c in rig.tick(0.25) if isinstance(c, Place) and c.req.role is Role.COVER]
    assert (second.req.side, second.req.qty) == (Side.BUY, LOT)
    rig.fill(second.order_pk, second.req.limit_px)
    for _ in range(3):
        rig.book(42.30, 42.50)
        assert not [c for c in rig.tick(0.5) if isinstance(c, Place)]
    assert a.leg_id not in rig.eng.state.exit_memory
    covered = sum(o.filled_qty for o in rig.leg(a.leg_id).leg_orders() if o.role is Role.COVER)
    assert covered == 2 * LOT and rig.positions(Account.PAPER)[0].net_qty == 0


def test_a_broker_read_steps_the_exit_loop_and_a_token_error_halts(tmp_path, core):
    """DETAIL §5.5: a positions or orders read is a relevant event for the exit loop. The exit cap waits for one read
    made after the exit decision (G3: a position closed in between is never sold twice), and the exit goes out the
    moment that read lands, priced on the quote that decided it, not a Timer later. §9.7: a token or permission
    error on a read raises HALT(TOKEN) + one CRITICAL, like one on an order; a failed read is remembered (the exit
    cap's fallback) and never steps the loop by itself (the Timer paces the retries)."""
    rig = Rig(tmp_path)
    rig.ctx.exit_loop = None                                     # the real titan2.core.exit_loop
    rig.eng = Engine.load(rig.store, rig.ctx)
    rig.tick()
    a = rig.open_leg()
    rig.clock.advance(3.0)
    with rig.store.tx():
        reads = rig.eng.request_positions(Account.PAPER)
    rig.answer(reads)                                            # a post-fill positions read, before the decision
    rig.auto_reads = False
    rig.tick(1.5)                                                # ... (B1: any read before it waits)
    rig.book(42.30, 42.50)
    with rig.store.tx():
        out = rig.eng.force_exit(rig.leg(a.leg_id), "OPERATOR_FLATTEN")
    assert rig.of(FetchPositions, out) == [FetchPositions(Account.PAPER, int(Priority.EXIT))] and not rig.of(Place, out)
    bv = rig.eng.state.broker[Account.PAPER]
    m = rig.clock.mono()
    out = rig.core(PositionsSnapshot(Account.LIVE, True, (), m, m))            # another account's read: no exit
    assert not rig.of(Place, out)
    out = rig.core(PositionsSnapshot(Account.PAPER, False, (), m, m, "AMBIGUOUS:NetworkException: timed out"))
    assert bv.pos_failed == m and not rig.of(Place, out) and not rig.halts("TOKEN")
    out = rig.core(rig.reply(FetchPositions(Account.PAPER, Priority.EXIT)))   # requested after the decision
    (pl,) = rig.of(Place, out)
    o = rig.eng.state.orders[pl.order_pk]
    assert o.role is Role.EXIT and o.qty == LOT and o.decision.bid == 42.30
    fatal = "FATAL:TokenException(403): Incorrect `api_key` or `access_token`."
    out = rig.core(OrdersSnapshot(Account.LIVE, False, (), m, m, fatal))
    assert [(h.kind, h.scope) for h in rig.halts("TOKEN")] == [("TOKEN", "GLOBAL")]
    assert [a.severity for a in rig.of(Alert, out) if a.code == "TOKEN"] == ["CRITICAL"]
    out = rig.core(PositionsSnapshot(Account.LIVE, False, (), m, m, fatal))
    out += rig.core(QuoteResult("q:other", False, (INST.symbol,), (), fatal))
    assert not [a for a in rig.of(Alert, out) if a.code == "TOKEN"] and len(rig.halts("TOKEN")) == 1


def test_command_stream_is_deterministic(tmp_path, core):
    """§16.1: the dual run compares engine command streams, so nothing random may reach them. Two engines fed the
    same events issue identical commands; the REST refresh request ids carry the engine instance, so a reloaded
    engine never reuses an earlier one's (whose late answers it ignores)."""
    streams = []
    for name in ("a", "b"):
        rig = Rig(tmp_path / name)
        rig.tick()
        rig.open_leg()
        rig.tick(4.0)                                            # the bid is stale: decide() asks REST for a book
        rig.eng = Engine.load(rig.store, rig.ctx)
        rig.tick(4.0)
        streams.append(list(rig.cmds))
        rig.store.close()
    assert streams[0] == streams[1]
    rids = [c.request_id for c in streams[0] if isinstance(c, FetchQuotes)]
    assert len(rids) == 2 and len(set(rids)) == 2 and rids[0].split(":")[1] != rids[1].split(":")[1]
