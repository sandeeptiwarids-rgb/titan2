"""Cross-unit contracts: the real output of one unit fed to the real consumer in another.

Unit tests use fakes on both sides of a seam, so a mismatch between two units (a symbol format, a time zone, a field
the consumer expects) can pass every unit test. These tests wire the real pieces the way the runtime and engine
will, without network or threads:
- Feed (kiteconnect-shaped ticks) -> QuoteCache + SignalQueue -> Bars -> opening_drive -> Intent -> titan2.db;
- Kite historical candles -> store.put_bar -> the self-check's previous-session coverage count;
- intent -> risk gates (real InstrumentStore, Feed health, recon memory) -> exits.entry_limit + sizing.size ->
  frozen ExitParams through titan2.db -> exits.decide on the QuoteCache's newest quote -> risk.leg_mtm;
- order/position state machines -> PaperSim + PaperExchange (fill-time book, square-off) -> orders.handle ->
  recon (verify_flat, reconcile) -> close()/close_external() -> the §9.8 paper truth check against the exchange.
"""

from __future__ import annotations

import collections
import contextlib
import itertools
import math
import queue
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from titan2 import charges, g1, ledger, selfcheck, settings as settings_mod, store as store_mod, strategies
from titan2.broker import lanes
from titan2.broker.kite import bar_from_kite
from titan2.broker.paper import MSG_BAND, SQOFF_TAG, PaperExchange, PaperSim
from titan2.clock import FakeClock, SessionCalendar, ist_datetime
from titan2.core import exits, orders, positions, recon, risk, sizing
from titan2.core.engine import Engine
from titan2.core.events import (
    Alert, ContractNote, ContractNoteResult, Exit, FeedState, FetchMargins, FetchOrders, FetchPositions, FetchQuotes,
    FetchTradebook, IntentReceived, MarginsResult, OrderSnapshot, OrdersSnapshot, PositionsSnapshot, Prep, PrepResult,
    Priority, QuoteResult, RecoveryStart, Timer, TradebookSnapshot,
)
from titan2.core.recon import ReconClass, ReconMemory
from titan2.core.single_leg import SingleLegHandler, decision_quote
from titan2.core.state import EngineContext
from titan2.instruments import InstrumentStore
from titan2.market.bars import Bars
from titan2.market.feed import Feed, FeedLink, SignalQueue, SignalsLost
from titan2.market.quotes import QuoteCache
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, DecisionQuote, Direction, ExitProfile, FeedHealth, Instrument, Intent, Leg, LegState,
    Level, Margins, Mode, Order, OrderState, Policy, Quote, Recheck, Refusal, Role, Side, Sized, StrikePref,
    StructState, StructureType, Underlying, Urgency,
)
from titan2.settings import DEFAULTS, INTENT_MAX_PER_MIN, exit_profile_problems

S = date(2026, 10, 12)                       # a Monday session
PREV = date(2026, 10, 9)                     # the Friday before it
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]})
REL, INFY = "NSE:RELIANCE", "NSE:INFY"
REL_TOK, INFY_TOK = 738561, 408065
SYMBOLS = {REL_TOK: REL, INFY_TOK: INFY}
UNIVERSE = frozenset({REL, INFY})
KITE_TZ = timezone(timedelta(hours=5, minutes=30))     # historical_data() dates carry a fixed +05:30 offset


class ImmediateReactor:
    """twisted's callFromThread, run inline (the seam under test is the data, not the threading)."""

    def callFromThread(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class Ticker:
    subscribed_tokens: dict = {}
    websocket_thread = None

    def subscribe(self, tokens):
        pass

    def set_mode(self, mode, tokens):
        pass

    def unsubscribe(self, tokens):
        pass


def kite_tick(token: int, ltp: float, volume: int, day_open: float, when: datetime) -> dict:
    """A kiteconnect 5.0.1 quote-mode tick: timestamps are naive host-local times (datetime.fromtimestamp)."""
    local = when.astimezone().replace(tzinfo=None)
    return {"tradable": True, "mode": "quote", "instrument_token": token, "last_price": ltp,
            "last_traded_quantity": 1, "average_traded_price": ltp, "volume_traded": volume,
            "total_buy_quantity": 0, "total_sell_quantity": 0,
            "ohlc": {"open": day_open, "high": max(day_open, ltp), "low": min(day_open, ltp), "close": day_open},
            "change": 0.0, "last_trade_time": local, "exchange_timestamp": local}


def prev_session_candles(symbol: str, volume: int = 20_000, n: int | None = None) -> tuple:
    """The previous session's 5-minute candles as kite.historical_data() returns them, normalized by the adapter."""
    open_, close = CAL.session_bounds(PREV)
    rows, t = [], open_
    while t < close:
        rows.append({"date": t.astimezone(KITE_TZ), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
                     "volume": volume})
        t += timedelta(minutes=5)
    return tuple(bar_from_kite(symbol, r) for r in rows[-n if n else 0:])


class Market:
    """The signals-thread wiring: Feed -> SignalQueue -> Bars + strategy runners, and a MarketView over them."""

    def __init__(self) -> None:
        self.clock = FakeClock(ist_datetime(S, "09:14:00"), mono_start=10_000.0)
        self.quotes = QuoteCache(mono=self.clock.mono)
        self.posted: list = []
        self.sq = SignalQueue()
        self.feed = Feed(Ticker(), self.quotes, self.posted.append, self.sq, SYMBOLS.get, clock=self.clock,
                         reactor=ImmediateReactor())
        self.feed.request(sorted(SYMBOLS), "quote")
        self.builder = Bars.for_session(CAL, S)
        self.runners, failed = strategies.load(DEFAULTS, exit_profile_problems, max_per_min=INTENT_MAX_PER_MIN)
        assert failed == {}
        self.closed: list = []

    # MarketView
    def ltp(self, u):
        q = self.quotes.last(u)
        return None if q is None or q.ltp is None else (q.ltp, q.age(self.clock.mono()))

    def last_tick(self, u):
        return None

    def bars(self, u, n):
        return self.builder.bars(u, n)

    def prev_session_bars(self, u, n):
        return self.builder.prev_session_bars(u, n)

    def universe(self):
        return UNIVERSE

    def feed_ok(self):
        return self.feed.health().connected

    def now_mono(self):
        return self.clock.mono()

    def drain(self) -> list:
        """The signals thread: every queued item to Bars first, then to the strategies."""
        intents = []
        while True:
            try:
                item = self.sq.get(timeout=0)
            except queue.Empty:
                return intents
            if isinstance(item, FeedLink):
                self.closed += self.builder.on_link(item.connected, item.recv_wall)
            elif isinstance(item, SignalsLost):
                self.closed += self.builder.on_gap(item.first_wall, item.last_wall)
            else:
                for t in item:
                    self.closed += self.builder.on_tick(t)
                    for r in self.runners.values():
                        intents += r.on_tick(t, self).intents

    def opening(self) -> list:
        """Connect at 09:14:50; a 5-minute opening drive (RELIANCE +3% on heavy volume, INFY flat); 09:20 timer."""
        for sym in UNIVERSE:
            self.builder.set_prev_session(sym, prev_session_candles(sym))
        self.clock.advance_to(ist_datetime(S, "09:14:50"))
        self.feed.on_connect(None)
        vol = {REL_TOK: 600_000, INFY_TOK: 50_000}              # cumulative, pre-open included
        for i in range(300):
            self.clock.advance_to(ist_datetime(S, "09:15:00") + timedelta(seconds=i, milliseconds=100))
            vol[REL_TOK] += 1_000
            vol[INFY_TOK] += 100
            now = self.clock.now_wall()
            self.feed.on_ticks(None, [kite_tick(REL_TOK, round(100.0 + 3.0 * i / 299, 2), vol[REL_TOK], 100.0, now),
                                      kite_tick(INFY_TOK, 50.0, vol[INFY_TOK], 50.0, now)])
            assert self.drain() == []
        self.clock.advance_to(ist_datetime(S, "09:20:00") + timedelta(milliseconds=100))
        self.drain()
        self.closed += self.builder.close_due(self.clock.now_wall())
        res = self.runners["opening_drive"].on_timer(self.clock.now_wall(), self)
        assert res.error is None and res.dropped == ()
        return list(res.intents)


def test_feed_bars_opening_drive_intent_reaches_the_db(tmp_path):
    m = Market()
    intents = m.opening()
    assert isinstance(m.posted[0], FeedState) and m.posted[0].connected
    first = {b.symbol: b for b in m.closed}
    rel = first[REL]
    assert rel.start == ist_datetime(S, "09:15:00") and rel.source == "LIVE" and rel.valid
    assert (rel.open, rel.close, rel.volume) == (100.0, 103.0, 900_000)    # ohlc.open; cumulative volume at 09:19:59
    assert first[INFY].valid and m.runners["opening_drive"].strategy.skips == {INFY: "SKIP:RET"}

    (i,) = intents
    assert (i.underlying, i.direction, i.structure, i.strike_pref) == (REL, Direction.UP, StructureType.LONG_CE,
                                                                       StrikePref.ITM1)
    assert i.trigger_px == 103.0 and math.isclose(i.recheck.lo, 103.0 * 0.995) and math.isclose(i.recheck.hi,
                                                                                                103.0 * 1.01)
    assert i.strategy_version == strategies.version(strategies.REGISTRY["opening_drive"], DEFAULTS)
    assert strategies.intent_problem(i) is None
    with store_mod.core_thread():
        st = store_mod.Store(tmp_path / "titan2.db", backup_dir=tmp_path / "backups")
        with st.tx():                                            # the intents CHECKs accept a strategy's intent
            st.insert_intent(i, S)
            for b in m.closed:
                st.put_bar(b)
        back = st.load_intent(i.intent_id, m.clock.mono(), m.clock.now_wall())
        assert (back.underlying, back.direction, back.structure, back.recheck) == (i.underlying, i.direction,
                                                                                  i.structure, i.recheck)
        assert st.bars(REL, source="LIVE") == [rel]
        st.close()


def test_hist_bars_through_the_store_feed_the_selfcheck_coverage(tmp_path):
    var = tmp_path / "var"
    (var / "db").mkdir(parents=True)
    rel, tcs = prev_session_candles(REL), prev_session_candles("NSE:TCS")
    infy = prev_session_candles(INFY)
    infy = tuple(replace(b, valid=False) for b in infy[:-10]) + infy[-10:]      # INFY: only 10 valid bars
    with store_mod.core_thread():
        st = store_mod.Store(var / "db" / store_mod.DB_FILENAME, backup_dir=tmp_path / "backups")
        with st.tx():
            for b in rel + infy + tcs:
                st.put_bar(b)
        assert st.bars(REL, source="HIST") == list(rel)              # the +05:30 dates and the valid flags round-trip
        assert st.bars(INFY, source="HIST") == list(infy)
        st.close()

    class Instruments:
        def universe(self, ban_list, allow, deny):
            return UNIVERSE | {"NSE:TCS"}

    loaded = settings_mod.LoadResult(DEFAULTS, True, (), var / "config" / "settings.json", False, None)
    ctx = selfcheck.Context(selfcheck.PRESTART, env={}, now=ist_datetime(S, "09:05:00"), var_dir=var,
                            settings=loaded, calendar=CAL, instruments=Instruments())
    try:
        assert ctx.bars() == (2, 3)                       # RELIANCE and TCS have 20+ valid HIST bars, INFY not
    finally:
        ctx.close()


def _instruments() -> InstrumentStore:
    unders = [Underlying("NSE:NIFTY 50", 256265, "NIFTY", "INDEX"), Underlying("NSE:NIFTY BANK", 260105, "BANKNIFTY",
                                                                                 "INDEX"),
              Underlying(REL, REL_TOK, "RELIANCE", "STOCK"), Underlying(INFY, INFY_TOK, "INFY", "STOCK")]
    contracts, tok = [], 50_000
    for name, und, strikes, expiries, lot in (
            ("RELIANCE", REL, (95.0, 97.5, 100.0, 102.5, 105.0, 107.5), (date(2026, 10, 27),), 500),
            ("INFY", INFY, (45.0, 50.0, 55.0), (date(2026, 10, 27),), 400),
            ("NIFTY", "NSE:NIFTY 50", (25000.0, 25050.0), (date(2026, 10, 13), date(2026, 10, 20)), 65),
            ("BANKNIFTY", "NSE:NIFTY BANK", (56000.0, 56100.0), (date(2026, 10, 27), date(2026, 11, 24)), 30)):
        for e in expiries:
            for k in strikes:
                for ot in ("CE", "PE"):
                    tok += 1
                    ts = f"{name}{e:%y%m%d}{k:g}{ot}"
                    contracts.append(Instrument(f"NFO:{ts}", tok, ts, "NFO", name, und, ot, k, e, lot, 0.05))
    return InstrumentStore(S, CAL, contracts, unders, min_stocks=1)


def test_entry_plan_exit_params_and_decide_across_units(tmp_path):
    m = Market()
    (intent,) = m.opening()
    ins = _instruments()
    spot = m.quotes.last(REL)                             # the underlying's newest WS quote
    contract = ins.select_contract(intent.underlying, "CE", intent.strike_pref, spot.ltp, S)
    assert isinstance(contract, Instrument) and (contract.strike, contract.lot_size) == (100.0, 500)

    book, rates, clock = "PAPER:opening_drive", charges.rates_for(S), m.clock
    bs = risk.BookState(book, Policy.FULL, 0, 0, 0, 0, 0, 0, 0.0, 0.0, False, {}, frozenset(), ())
    profile = strategies.REGISTRY[intent.strategy].EXIT_PROFILE

    def ctx() -> risk.GateContext:
        recon = ReconMemory(status="CLEAN", last_clean_mono=clock.mono() - 2.0, clean_streak=2)
        return risk.GateContext(
            now_mono=clock.mono(), now_wall=clock.now_wall(), session=S, settings=DEFAULTS, calendar=CAL,
            clock_synced=True, instruments_loaded=True, rates=rates, instruments=ins, ban_list=frozenset(),
            blocked_symbols=frozenset(), feed=m.feed.health(), signals_degraded=False,
            recon={a: recon for a in Account},
            margins={a: MarginsResult(a, True, Margins(a, 500_000.0, {}), clock.mono() - 5.0) for a in Account},
            halts=(), strategy_modes={n: Mode.PAPER for n in m.runners},
            running_versions={n: r.version for n, r in m.runners.items()}, disabled_strategies=frozenset(),
            structure_types=frozenset({StructureType.LONG_CE, StructureType.LONG_PE}), book=bs, live_account=None,
            live=None)

    def plan(inst, q):                                    # the gateway's G18 plan (U13 wires exactly this)
        limit = exits.entry_limit(q, inst.tick)
        if limit is None:
            return None, None
        sized = sizing.size(inst, q, limit, profile, DEFAULTS, risk.book_realized_net(bs), rates, Policy.FULL)
        if not isinstance(sized, Sized):
            return limit, sized
        return exits.entry_limit(q, inst.tick, sized.qty), sized

    def book_quote(bid: float, ask: float) -> Quote:
        now = clock.now_wall()
        bids = [Level(bid, 3000), Level(bid - 0.05, 3000), Level(bid - 0.1, 3000)]
        asks = [Level(ask, 3000), Level(ask + 0.05, 3000), Level(ask + 0.1, 3000)]
        return Quote(contract.symbol, contract.token, bids, asks, ask, 10_000, now - timedelta(seconds=2), now,
                     clock.mono(), now, "REST")

    assert risk.pre_reserve_gates(ctx(), intent).passed     # feed warm since 09:14:50, CLEAN recon, inside the window
    cheap = risk.post_quote_gates(ctx(), intent, contract, book_quote(4.95, 5.0), spot, plan)
    assert cheap.result == Refusal("SIZE", "PREMIUM_FLOOR")
    q = book_quote(24.95, 25.0)
    out = risk.post_quote_gates(ctx(), intent, contract, q, spot, plan)
    assert out.passed and out.limit_px == 25.1
    assert (out.sized.qty, out.sized.lots, out.sized.binding) == (500, 1, "LOTS")
    assert risk.final_gate(ctx(), intent).passed

    # the entry fills at 25.00: levels frozen from the fills survive titan2.db unchanged (the store keeps ms)
    fill_wall = clock.now_wall() + timedelta(microseconds=400_123)
    p = exits.exit_params_from_fills(profile, [charges.OrderFills(Side.BUY, 500, 12_500.0)], rates, fill_wall)
    assert exits.exit_params_problems(p) == []
    assert store_mod.exit_params_from_json(store_mod.db_json(p)) == p
    assert p.hard_sl_px == 21.25 and math.isclose(p.tp_px, 28.75) and p.be_px > 25.0

    entry = Order("T2EP26101200101", 1, 1, Account.PAPER, book, Role.ENTRY, Side.BUY, contract.symbol, 500,
                  out.limit_px, 1, None, state=OrderState.COMPLETE, filled_qty=500, avg_fill_px=25.0)
    leg = Leg(1, 1, 1, Account.PAPER, book, S, 1, contract, state=LegState.OPEN, orders=[entry.order_pk],
              stop_px=p.hard_sl_px, entry_fill_wall=p.entry_fill_wall).bind_orders({entry.order_pk: entry})
    eod = exits.EodTimes.for_session(S, DEFAULTS)
    clock.advance(1.0)
    m.quotes.put(book_quote(27.3, 27.35))
    d = exits.decide(leg, m.quotes.last(contract.symbol), exits.RefreshState(), clock.mono(), clock.now_wall(), p,
                     eod, None)
    assert isinstance(d, exits.NoAction) and d.marks.lock_armed and d.marks.stop_px >= p.floor_px
    d.marks.apply_to(leg)
    fresh = m.quotes.fresh(contract.symbol, DEFAULTS.STALE_REFRESH_S, clock.mono())
    rb, thin = exits.realizable_bid(fresh, leg.open_qty)
    done = [charges.OrderFills(Side.BUY, 500, 12_500.0)]
    mtm = risk.leg_mtm(done, leg.open_qty, rb, m.quotes.age(contract.symbol, clock.mono()), rates,
                       DEFAULTS.STALE_REFRESH_S)
    assert (rb, thin) == (27.3, False) and math.isclose(mtm, charges.mtm_net(done, 500, 27.3, rates))
    clock.advance(DEFAULTS.STALE_REFRESH_S + 0.5)           # no newer quote: the MTM is unknown, never cached
    assert risk.leg_mtm(done, leg.open_qty, rb, m.quotes.age(contract.symbol, clock.mono()), rates,
                        DEFAULTS.STALE_REFRESH_S) is None
    stale = exits.decide(leg, m.quotes.last(contract.symbol), exits.RefreshState(), clock.mono(), clock.now_wall(),
                         p, eod, None)                      # ... and the exit engine asks REST for a fresh book
    assert isinstance(stale, exits.RefreshQuote) and exits.QUOTE_STALE in stale.notes
    assert m.quotes.fresh(contract.symbol, DEFAULTS.STALE_REFRESH_S, clock.mono()) is None
    m.quotes.put(book_quote(29.0, 29.1))
    d = exits.decide(leg, m.quotes.last(contract.symbol), None, clock.mono(), clock.now_wall(), p, eod, None)
    assert isinstance(d, exits.Exit) and (d.reason, d.urgency.value) == ("TAKE_PROFIT", "NORMAL")
    limit = exits.exit_limit(m.quotes.fresh(contract.symbol, settings_mod.EXIT_QUOTE_MAX_AGE_S, clock.mono()), 500,
                             contract.tick, 0)
    assert limit == 28.9                                    # sweep 29.00 - 2 ticks, inside the 12% floor
    assert isinstance(intent.recheck, Recheck)


# ---------------------------------------------------------------------------------------------------------------
# The paper exchange answering the order and position machines, recon and the ledger
# ---------------------------------------------------------------------------------------------------------------
OPT = Instrument("NFO:RELIANCE26OCT2900CE", 13001218, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", REL, "CE", 2900.0,
                 date(2026, 10, 27), 250, 0.05)


class PaperDesk:
    """The engine's side of the paper seam, inline: commands go to the real PaperExchange through PaperSim (which
    runs them with lanes.execute_broker, as the paper-sim thread does); every event it answers with comes back
    through orders.find_order + orders.handle inside a store.tx(), and their commands go out again."""

    def __init__(self, tmp_path, start: str) -> None:
        self.clock = FakeClock(ist_datetime(S, start))
        self.quotes = QuoteCache(mono=self.clock.mono)
        self.px = PaperExchange(tmp_path / "paper_exchange.db", settings=DEFAULTS, clock=self.clock,
                                quotes=self.quotes, calendar=SessionCalendar.load(),
                                instruments={OPT.symbol: OPT}.get, thread_check=False)
        self.sim = PaperSim(self.px, lambda ev: None)
        self.store = store_mod.Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        self.index: dict[str, Order] = {}
        self.timers: dict[str, orders.OrderTimers] = {}
        self.legs: dict[int, Leg] = {}
        self.reads: list = []                    # answers no order owns (positions, orders)
        self.alerts: list[Alert] = []
        self.halts: list = []
        self.bid, self.ask, self.ltp = 42.00, 42.10, None
        self._ids = itertools.count(1)

    def close(self) -> None:
        self.store.close()
        self.px.close()

    def now(self) -> dict:
        return {"now_mono": self.clock.mono(), "now_wall": self.clock.now_wall()}

    def quote(self) -> Quote:
        t = OPT.tick
        q = Quote(OPT.symbol, OPT.token, (Level(self.bid, 5000), Level(round(self.bid - t, 2), 5000)),
                  (Level(self.ask, 5000), Level(round(self.ask + t, 2), 5000)), self.ltp or self.ask, None, None,
                  None, self.clock.mono(), self.clock.now_wall(), "WS")
        self.quotes.put(q)
        return q

    def decision(self) -> DecisionQuote:
        q = self.quote()
        return DecisionQuote(q.bid, q.ask, q.mid(), 0, q.source, q.ltp, q.ask)

    def send(self, fx) -> None:
        self.alerts += [c for c in fx.commands if isinstance(c, Alert)]
        self.halts += fx.halts
        for c in fx.commands:
            if lanes.lane_for(c) == "paper":
                self.sim.submit(c)

    def run(self, seconds: float, dt: float = 0.05) -> None:
        """The market and paper-sim move on: a fresh book every dt, each paper event delivered to its order."""
        for _ in range(round(seconds / dt)):
            self.clock.advance(dt)
            self.quote()
            for ev in self.sim.drain():
                self.deliver(ev)

    def deliver(self, ev) -> None:
        if isinstance(ev, OrderSnapshot):
            o = orders.find_order(self.index, ev.account, tag=ev.tag, broker_order_id=ev.broker_order_id)
        else:
            o = self.index.get(getattr(ev, "order_pk", None))
        if o is None:
            self.reads.append(ev)
            return
        t = self.timers.setdefault(o.order_pk, orders.OrderTimers())
        with self.store.tx():
            fx = orders.handle(self.store, o, self.legs[o.leg_id], ev, t, self.clock.mono(), self.clock.now_wall())
        self.send(fx)

    def read(self, kind):
        """One account read (FetchPositions / FetchOrders), answered by paper-sim after its read delay (B1: a broker
        read pays its round trip on paper too, at most PAPER_LATENCY_CLIP_MS); the market moves on meanwhile."""
        self.reads.clear()
        self.sim.submit(kind(Account.PAPER, int(Priority.EXIT)))
        for ev in self.sim.drain():
            self.deliver(ev)
        for _ in range(round(3.1 / 0.05)):
            if self.reads:
                break
            self.run(0.05)
        (answer,) = self.reads
        return answer

    def enter(self, book: str):
        """reserve -> begin_entry for one lot at exits.entry_limit -> Place to paper-sim (the gateway's last step)."""
        intent = Intent(intent_id=f"s{next(self._ids):011d}", strategy=book.split(":")[1], strategy_version="1.0abc",
                        underlying=REL, direction=Direction.UP, structure=StructureType.LONG_CE,
                        strike_pref=StrikePref.ATM, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0,
                        recheck=Recheck(-math.inf, math.inf), ttl_s=10.0, created_mono=self.clock.mono(),
                        created_wall=self.clock.now_wall(), reason={"seam": "paper"})
        dq = self.decision()
        limit = exits.entry_limit(self.quotes.last(OPT.symbol), OPT.tick, OPT.lot_size)
        with self.store.tx():
            self.store.insert_intent(intent, S)
            st = positions.reserve(self.store, intent, book=book, account=Account.PAPER, policy=Policy.FULL,
                                   **self.now())
            leg, o, fx = positions.begin_entry(self.store, self.index, st, inst=OPT, qty=OPT.lot_size,
                                               limit_px=limit, decision_quote=dq, decision={"limit": limit},
                                               **self.now())
        self.legs[leg.leg_id] = leg
        self.send(fx)
        return st, leg, o

    def entry_terminal(self, st, leg) -> None:
        with self.store.tx():
            self.send(positions.on_entry_terminal(self.store, st, leg, profile=ExitProfile(), **self.now()))

    def truth(self) -> recon.TruthCheck:
        """§9.8 for PAPER: titan2.db's ledger per book against the exchange's tradebook, recomputed by charges."""
        nets: dict[str, float] = {}
        for row in self.store.trades(session=S, account=Account.PAPER):
            nets[row["book"]] = nets.get(row["book"], 0.0) + row["net"]
        books, parts = ledger.truth_attribution(self.index.values())          # the engine's own attribution
        return recon.eod_truth_check(Account.PAPER, S, nets, books, self.px.trades_today(Account.PAPER),
                                     parts=parts)


def test_paper_round_trip_books_what_the_paper_exchange_booked(tmp_path):
    """An entry the simulated exchange rejects (price band vs the LTP) voids without blocking the contract; an entry
    and an exit fill against the fill-time book; verify-flat reads the exchange's post-fill positions; close() books
    exactly what the paper exchange's own ledger shows (the §9.8 paper truth check, to the paisa)."""
    with store_mod.core_thread():
        d = PaperDesk(tmp_path, "10:00:00")
        try:
            d.ltp = 30.0                                  # a stale LTP 30% under the book: the band check rejects
            st0, leg0, o0 = d.enter("PAPER:opening_drive")
            d.run(0.05)
            assert (o0.state, o0.status_msg) == (OrderState.REJECTED, MSG_BAND)
            assert d.halts == [] and [a.code for a in d.alerts] == ["ORDER_REJECTED"]   # "band" is not "ban"
            d.entry_terminal(st0, leg0)
            assert st0.state is StructState.VOID and st0.void_reason == f"REJECTED:{MSG_BAND}"

            d.ltp = None
            st, leg, entry = d.enter("PAPER:opening_drive")
            d.run(3.5)                                    # latency (<= 3 s), the fill, FetchTrades
            assert (entry.state, entry.filled_qty, entry.avg_fill_px) == (OrderState.COMPLETE, 250, 42.10)
            assert [f.source for f in d.store.effective_fills([entry.order_pk])] == ["TRADE"]
            d.entry_terminal(st, leg)
            assert st.state is StructState.OPEN and leg.open_qty == 250 and st.risk_basis_rs == 10525.0

            d.bid, d.ask = 48.30, 48.40
            q = d.quote()
            with d.store.tx():
                assert positions.begin_exit(d.store, st, leg, "TAKE_PROFIT", Urgency.NORMAL, **d.now())
                x, fx = orders.create_order(d.store, d.index, leg, role=Role.EXIT, qty=leg.open_qty,
                                            limit_px=exits.exit_limit(q, leg.open_qty, OPT.tick, 0),
                                            decision=d.decision(), band_idx=0, **d.now())
            d.send(fx)
            d.run(3.5)
            assert (x.state, x.filled_qty, x.avg_fill_px, leg.open_qty) == (OrderState.COMPLETE, 250, 48.30, 0)
            with d.store.tx():
                positions.to_verify_flat(d.store, st, leg, **d.now())
            d.run(2.5)                                    # §9.2: ask for positions >= 2 s after the last fill
            sv = recon.symbol_view(recon.BotView(Account.PAPER, S, DEFAULTS.BROKER_SQUAREOFF_AT, (leg,)), OPT.symbol)
            vf = recon.verify_flat(leg.leg_id, sv, d.read(FetchPositions), d.read(FetchOrders),
                                   recon.VerifyState(d.clock.mono()), d.clock.mono(), account=Account.PAPER)
            assert vf.outcome == "CLOSED" and vf.via == "POSITIONS"
            with d.store.tx():
                row, _ = positions.close(d.store, st, [leg], **d.now())
            assert (row["pnl_status"], row["n_executed_orders"], row["gross"]) == ("FINAL", 2, 1550.0)

            tc = d.truth()
            assert tc.ok and tc.books[0].diff == 0.0 and tc.unattributed == () and tc.g1_mismatch_books == ()
            funds = d.px.margins(Account.PAPER).raw
            assert row["net"] == funds["realized_net"] and row["charges"] == funds["charges"]
        finally:
            d.close()


def test_paper_squareoff_is_booked_as_broker_squareoff_to_the_paisa(tmp_path):
    """A leg still open at BROKER_SQUAREOFF_AT is closed by the simulated exchange (an EXCHANGE order tagged SQOFF);
    recon confirms BROKER_SQUAREOFF from two post-fill snapshots (no halt, no G1 mismatch) and close_external()
    books it with the square-off fee, exactly as the paper exchange's ledger does."""
    with store_mod.core_thread():
        d = PaperDesk(tmp_path, "15:20:00")
        try:
            st, leg, _ = d.enter("PAPER:price_spike_up")
            d.run(3.5)
            d.entry_terminal(st, leg)
            assert st.state is StructState.OPEN
            d.clock.advance_to(ist_datetime(S, DEFAULTS.BROKER_SQUAREOFF_AT))
            d.bid, d.ask = 40.00, 40.10
            d.run(0.1)
            (sq,) = [o for o in d.px.orders_today(Account.PAPER) if o.tag == SQOFF_TAG]
            assert (sq.side, sq.filled_qty, sq.avg_px, sq.placed_by) == (Side.SELL, 250, 40.00, "EXCHANGE")

            mem, res = ReconMemory(), None
            for _ in range(2):                            # seen once, then confirmed >= 3 s later outside grace
                d.run(3.1)
                bot = recon.BotView(Account.PAPER, S, DEFAULTS.BROKER_SQUAREOFF_AT, (leg,))
                res = recon.reconcile(bot, d.read(FetchPositions), d.read(FetchOrders), mem, d.clock.mono())
                mem = res.memory
            assert res.status is ReconClass.BROKER_SQUAREOFF and res.halts == () and not res.g1_mismatch
            (finding,) = res.findings
            (ext,) = finding.closes
            assert (ext.qty, ext.external_kind, str(ext.pnl_status)) == (250, charges.AUTO_SQUAREOFF, "FINAL")
            with d.store.tx():
                row, fx = positions.close_external(d.store, d.index, st, leg, ext, **d.now())
            assert fx.halts == [] and st.state is StructState.CLOSED_EXTERNAL
            assert (row["exit_reason"], row["pnl_status"], row["sqoff_fee"]) == ("BROKER_SQUAREOFF", "FINAL", 50.0)
            assert d.truth().ok
            assert row["net"] == d.px.margins(Account.PAPER).raw["realized_net"]
        finally:
            d.close()


def test_shared_squareoff_order_books_its_fixed_fees_once(tmp_path):
    """One broker square-off order closing the legs of two books on one symbol (G10 is per book) is booked as two
    shares (charges.OrderPart): its brokerage and Rs 50 square-off fee once in total, and the §9.8 truth check
    compares each book on its share, so the paper ledger equals the exchange's recompute to the paisa."""
    with store_mod.core_thread():
        d = PaperDesk(tmp_path, "15:20:00")
        try:
            held = []
            for book in ("PAPER:opening_drive", "PAPER:price_spike_up"):     # G10 is per book: both may hold it
                st, leg, _ = d.enter(book)
                d.run(3.5)
                d.entry_terminal(st, leg)
                held.append((st, leg))
            d.clock.advance_to(ist_datetime(S, DEFAULTS.BROKER_SQUAREOFF_AT))
            d.bid, d.ask = 40.00, 40.10
            mem, res = ReconMemory(), None
            for _ in range(3):
                d.run(3.1)
                bot = recon.BotView(Account.PAPER, S, DEFAULTS.BROKER_SQUAREOFF_AT, tuple(lg for _, lg in held))
                res = recon.reconcile(bot, d.read(FetchPositions), d.read(FetchOrders), mem, d.clock.mono())
                mem = res.memory
                if res.status is ReconClass.BROKER_SQUAREOFF:
                    break
            assert res.status is ReconClass.BROKER_SQUAREOFF
            nets, rows = [], []
            for ext in res.findings[0].closes:
                st, leg = next((s, lg) for s, lg in held if lg.leg_id == ext.leg_id)
                with d.store.tx():
                    row, _ = positions.close_external(d.store, d.index, st, leg, ext, **d.now())
                nets.append(row["net"])
                rows.append(row)
            assert round(sum(nets), 2) == d.px.margins(Account.PAPER).raw["realized_net"]   # "paper = engine"
            assert round(sum(r["charges"] for r in rows), 2) == d.px.margins(Account.PAPER).raw["charges"]
            # two entry orders and ONE square-off order executed: three brokerages and one square-off fee in total
            assert sum(r["sqoff_fee"] for r in rows) == 50.0 and sum(r["brokerage"] for r in rows) == 3 * 20.0
            assert sum(r["n_executed_orders"] for r in rows) == 3
            assert d.truth().ok
            # G1 re-prices every trade from titan2.db (ledger.order_values_by_structure): the shares read back as
            #   booked, so the recompute equals the ledger
            vals = ledger.order_values_by_structure(d.store, [r["structure_id"] for r in rows])
            r26 = charges.rates_for(S)
            for r in rows:
                fills = [of for v in vals[r["structure_id"]] if (of := v.order_fills()) is not None]
                assert charges.net_pnl(fills, r26).net == r["net"]
            ext_rows = [o for o in d.index.values() if o.role is Role.EXTERNAL]
            assert sorted(ledger.order_parts(d.store, [o.order_pk for o in ext_rows])) == sorted(
                o.order_pk for o in ext_rows)
        finally:
            d.close()


def test_the_decision_time_goes_with_the_order_so_a_late_packet_never_fills_it(tmp_path):
    """Review of paper model 2 (probe test_probe_lag_before_place), across the seam. The engine prices an entry from
    a REST snapshot stamped 10:30:00 (single_leg.decision_quote, then positions.begin_entry's Place). Lagged WS
    packets, stamped 10:29:57 and 10:29:58, reach the QuoteCache after that decision: the first before paper-sim runs
    the Place, the second after it. The paper exchange took "the decision" from the cache when the Place ran, so the
    first lagged packet passed for it and the second one filled the BUY at 41.85, under the decision ask of 42.10.
    The engine's request now carries its decision quote's exchange time (DecisionQuote.exch_wall ->
    OrderRequest.decision_exch_wall), so both packets are older books: the order waits for the next real one."""
    with store_mod.core_thread():
        d = PaperDesk(tmp_path, "10:30:00")
        try:
            t0 = d.clock.now_wall()

            def put(bid: float, ask: float, stamp: datetime, source: str = "WS") -> Quote:
                q = Quote(OPT.symbol, OPT.token, (Level(bid, 5000),), (Level(ask, 5000),), None, None, None, stamp,
                          d.clock.mono(), d.clock.now_wall(), source)
                d.quotes.put(q)
                return q

            def drain(seconds: float) -> None:
                for _ in range(round(seconds / 0.05)):
                    d.clock.advance(0.05)
                    for ev in d.sim.drain():
                        d.deliver(ev)

            decision = put(42.00, 42.10, t0, "REST")               # the snapshot the engine prices the entry from
            intent = Intent(intent_id="slagged00001", strategy="opening_drive", strategy_version="1.0abc",
                            underlying=REL, direction=Direction.UP, structure=StructureType.LONG_CE,
                            strike_pref=StrikePref.ATM, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0,
                            recheck=Recheck(-math.inf, math.inf), ttl_s=10.0, created_mono=d.clock.mono(),
                            created_wall=t0, reason={"seam": "lagged packet"})
            dq = decision_quote(decision, OPT.lot_size, Side.BUY, d.clock.mono())
            limit = exits.entry_limit(decision, OPT.tick, OPT.lot_size)
            with d.store.tx():
                d.store.insert_intent(intent, S)
                st = positions.reserve(d.store, intent, book="PAPER:opening_drive", account=Account.PAPER,
                                       policy=Policy.FULL, **d.now())
                leg, entry, fx = positions.begin_entry(d.store, d.index, st, inst=OPT, qty=OPT.lot_size,
                                                       limit_px=limit, decision_quote=dq, decision={"limit": limit},
                                                       **d.now())
            d.legs[leg.leg_id] = leg
            d.send(fx)                                              # the Place, queued after the decision's commit
            d.clock.advance(0.02)
            put(41.70, 41.80, t0 - timedelta(seconds=3))           # a lagged burst begins before paper-sim runs it
            d.clock.advance(0.01)
            for ev in d.sim.drain():                                # paper-sim takes the Place now
                d.deliver(ev)
            d.clock.advance(0.02)
            put(41.75, 41.85, t0 - timedelta(seconds=2))           # still older than the decision snapshot
            drain(0.5)                                              # past the due time; nothing newer comes
            assert (entry.filled_qty, entry.avg_fill_px) == (0, None), "a book older than the decision filled it"
            put(42.05, 42.15, d.clock.now_wall(), "REST")         # the next real book
            drain(0.5)
            assert (entry.state, entry.filled_qty, entry.avg_fill_px) == (OrderState.COMPLETE, OPT.lot_size, 42.15)
        finally:
            d.close()


# ---------------------------------------------------------------------------------------------------------------
# The engine (U14) with its gateway and exit loop (U13) driving the paper exchange (U08) through PaperSim
# ---------------------------------------------------------------------------------------------------------------
SEAM_STRAT, SEAM_VER = "price_spike_up", "1.seam"
SEAM_EXPIRY = date(2026, 10, 27)
SEAM_CE, SEAM_PE = "NFO:RELIANCE26OCT2900CE", "NFO:RELIANCE26OCT2900PE"


def _seam_instruments() -> InstrumentStore:
    unders = [Underlying("NSE:NIFTY 50", 256265, "NIFTY", "INDEX"),
              Underlying("NSE:NIFTY BANK", 260105, "BANKNIFTY", "INDEX"), Underlying(REL, REL_TOK, "RELIANCE", "STOCK")]
    contracts, tok = [], 80_000
    for name, und, strikes, expiries, lot in (
            ("RELIANCE", REL, (2800, 2850, 2900, 2950, 3000), (SEAM_EXPIRY,), 250),
            ("NIFTY", "NSE:NIFTY 50", (25000, 25050), (date(2026, 10, 13), SEAM_EXPIRY), 65),
            ("BANKNIFTY", "NSE:NIFTY BANK", (56000, 56100), (SEAM_EXPIRY, date(2026, 11, 24)), 30)):
        for e in expiries:
            for k in strikes:
                for ot in ("CE", "PE"):
                    tok += 1
                    ts = f"{name}{'26OCT' if e == SEAM_EXPIRY else e.strftime('%y%m%d')}{k:g}{ot}"
                    contracts.append(Instrument(f"NFO:{ts}", tok, ts, "NFO", name, und, ot, float(k), e, lot, 0.05))
    return InstrumentStore(S, CAL, contracts, unders, min_stocks=1)


class EngineDesk:
    """The runtime's wiring, inline: Engine.handle with the real gateway, SingleLegHandler and exit loop inside a
    store.tx() per event (§3.3), commands dispatched after the commit; every PAPER/SHADOW command answered by the real
    PaperExchange through PaperSim (lanes.execute_broker), the LIVE account (never traded here) and market reads
    answered at once. A Timer every dt with a fresh book (the WS) and the exchange stepped between events."""

    def __init__(self, tmp_path, start: str) -> None:
        self.clock = FakeClock(ist_datetime(S, start))
        self.cache = QuoteCache(mono=self.clock.mono)
        self.ins = _seam_instruments()
        self.book = {SEAM_CE: (42.00, 42.10), SEAM_PE: (38.00, 38.10)}
        self.events: collections.deque = collections.deque()
        self.px = PaperExchange(tmp_path / "paper_exchange.db", settings=DEFAULTS, clock=self.clock, quotes=self.cache,
                                calendar=CAL, instruments=self.ins.by_symbol, thread_check=False)
        self.sim = PaperSim(self.px, self.events.append)
        h = SingleLegHandler()
        clock = self.clock
        self.ctx = EngineContext(
            settings=DEFAULTS, calendar=CAL, clock=clock, quotes=self.cache, versions={SEAM_STRAT: SEAM_VER},
            exit_profiles={SEAM_STRAT: ExitProfile()}, handlers={StructureType.LONG_CE: h, StructureType.LONG_PE: h},
            instruments=lambda: self.ins, ban_list=lambda: frozenset(),
            feed_health=lambda: FeedHealth(True, clock.mono() - 600.0, clock.mono() - 0.1, 0, False),
            start_mono=clock.mono())                      # no pub_dir: the dashboard files are not under test
        self.ctx.prep.update({"INSTRUMENTS": True, "BAN_LIST": True, "PREV_BARS": True})
        self.sent: list = []
        self.store = store_mod.Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        self.eng = Engine.load(self.store, self.ctx)

    def close(self) -> None:
        self.store.close()
        self.px.close()

    def market(self) -> None:
        now, m = self.clock.now_wall(), self.clock.mono()
        self.cache.put(Quote(REL, REL_TOK, (), (), 2900.0, 1_000_000, now, now, m, now, "WS"))
        for sym, (bid, ask) in self.book.items():
            self.cache.put(Quote(sym, self.ins.by_symbol(sym).token,
                                 tuple(Level(round(bid - i * 0.05, 2), 5000) for i in range(3)),
                                 tuple(Level(round(ask + i * 0.05, 2), 5000) for i in range(3)), ask, 100_000,
                                 now - timedelta(seconds=2), now, m, now, "WS"))

    def dispatch(self, cmds) -> None:
        for c in cmds:
            m = self.clock.mono()
            if lanes.lane_for(c) == "paper":
                self.sim.submit(c)
            elif isinstance(c, FetchQuotes):
                got = tuple(s for s in c.symbols if self.cache.last(s) is not None)
                self.events.append(QuoteResult(c.request_id, True, got, tuple(s for s in c.symbols if s not in got)))
            elif isinstance(c, FetchPositions):
                self.events.append(PositionsSnapshot(c.account, True, (), m, m))
            elif isinstance(c, FetchOrders):
                self.events.append(OrdersSnapshot(c.account, True, (), m, m))
            elif isinstance(c, FetchTradebook):
                self.events.append(TradebookSnapshot(c.account, True, (), m))
            elif isinstance(c, FetchMargins):
                self.events.append(MarginsResult(c.account, True, Margins(c.account, 500_000.0, {}), m))
            elif isinstance(c, Prep):
                self.events.append(PrepResult(c.kind, True, "ok"))
            elif isinstance(c, ContractNote):
                assert lanes.lane_for(c) == "reads"     # Kite's calculator (§8.1), never the paper exchange
                self.events.append(ContractNoteResult(c.account, False, ()))   # as Kite refusing synthetic ids

    def handle(self, ev) -> None:
        with self.store.tx():
            out = self.eng.handle(ev)
            self.eng.check_invariants()
        self.sent.extend(out)
        self.dispatch(out)

    def settle(self) -> None:
        while True:
            self.sim.drain()                               # posts the exchange's answers to self.events
            if not self.events:
                return
            while self.events:
                self.handle(self.events.popleft())

    def run(self, seconds: float, dt: float = 0.25) -> None:
        for _ in range(round(seconds / dt)):
            self.clock.advance(dt)
            self.market()
            self.handle(Timer(self.clock.now_wall(), self.clock.mono()))
            self.settle()

    def start(self) -> None:
        """Recovery, then CLEAN recons until entries resume (§9.6)."""
        self.market()
        self.handle(RecoveryStart())
        self.settle()
        self.run(20.0)
        assert self.eng.recovery_done and self.eng.state.entries_resumed and self.eng.phase == "TRADING"

    def enter(self, intent_id: str):
        """One PAPER intent; its entry and its placebo, sent together, fill on the paper exchange. Returns
        (parent, placebo)."""
        now = self.clock.now_wall()
        self.handle(IntentReceived(Intent(intent_id, SEAM_STRAT, SEAM_VER, REL, Direction.UP, StructureType.LONG_CE,
                                          StrikePref.ATM, EXPIRY_RULE_NEAREST, 2900.0, Recheck(2885.0, 2930.0), 10.0,
                                          self.clock.mono(), now, {"m": 1.0})))
        self.settle()
        self.run(7.0)                                      # paper latency (<= 3 s) for the entry and the placebo
        (parent,) = [s for s in self.eng.state.structures.values() if s.book == f"PAPER:{SEAM_STRAT}"]
        (placebo,) = [s for s in self.eng.state.structures.values() if s.book == f"PLACEBO:{SEAM_STRAT}"]
        assert parent.state is StructState.OPEN and placebo.state is StructState.OPEN
        assert parent.placebo_status == "OPENED" and placebo.account is Account.SHADOW
        assert [lg.open_qty for lg in self.eng.state.legs_of(parent) + self.eng.state.legs_of(placebo)] == [250, 250]
        return parent, placebo

    def paper_equals_engine(self) -> None:
        """§9.8: per account, the engine's ledger equals the exchange's own books, to the paisa."""
        for acct in (Account.PAPER, Account.SHADOW):
            rows = self.store.trades(session=S, account=acct)
            funds = self.px.margins(acct).raw
            assert round(sum(r["net"] for r in rows), 2) == funds["realized_net"]
            assert round(sum(r["charges"] for r in rows), 2) == funds["charges"]
            books, parts = ledger.truth_attribution(o for o in self.eng.state.orders.values()
                                                    if Account(o.account) is acct)
            nets: dict[str, float] = {}
            for r in rows:
                nets[r["book"]] = nets.get(r["book"], 0.0) + r["net"]
            tc = recon.eod_truth_check(acct, S, nets, books, self.px.trades_today(acct), parts=parts)
            assert tc.ok and tc.unattributed == () and tc.g1_mismatch_books == ()

    def quiet(self) -> None:
        """Nothing halted, no invariant violation or exception, no CRITICAL alert."""
        assert not self.store.active_halts()
        assert not self.store.events(kind="INVARIANT_VIOLATION") and not self.store.events(kind="EXCEPTION")
        assert not [a for a in self.sent if isinstance(a, Alert) and a.severity == "CRITICAL"]


def test_engine_drives_the_paper_exchange_end_to_end(tmp_path):
    """The runtime's wiring with the real paper exchange (EngineDesk): recovery and CLEAN recons, one decision fetch,
    the PAPER entry filled against the fill-time book, its placebo on SHADOW sent with it (F-18: the same placed_ts,
    each filling after its own paper latency), a HARD_SL exit and a TAKE_PROFIT exit priced from the bid,
    verify-flat on the exchange's positions, the trades booked and paired for G1. The ledger equals the exchange's
    own books (§9.8, to the paisa) and nothing halted."""
    with store_mod.core_thread():
        d = EngineDesk(tmp_path, "09:58:00")
        try:
            d.start()
            parent, placebo = d.enter("seam00000001")
            with contextlib.closing(sqlite3.connect(tmp_path / "paper_exchange.db")) as c:
                c.row_factory = sqlite3.Row
                sent = {r["account"]: dict(r) for r in c.execute(
                    "SELECT account, placed_ts, due_ts, terminal_ts, status FROM p_orders WHERE placed_by = 'API'")}
            p_ord, b_ord = sent["PAPER"], sent["SHADOW"]
            assert p_ord["placed_ts"] == b_ord["placed_ts"]            # one decision, one commit, both sent
            assert p_ord["due_ts"] != b_ord["due_ts"]                  # each pays its own paper latency ...
            assert all(r["status"] == "COMPLETE" and r["terminal_ts"] >= r["due_ts"] for r in (p_ord, b_ord))
            d.book[SEAM_CE] = (30.00, 30.10)               # the CE far below its hard stop; the PE far above its TP
            d.book[SEAM_PE] = (48.00, 48.10)
            for _ in range(80):
                d.run(0.5)
                if parent.is_terminal and placebo.is_terminal:
                    break
            assert parent.state is StructState.CLOSED and placebo.state is StructState.CLOSED
            rows = {r["book"]: r for r in d.store.trades(session=S)}
            p_row, b_row = rows[f"PAPER:{SEAM_STRAT}"], rows[f"PLACEBO:{SEAM_STRAT}"]
            assert (p_row["exit_reason"], p_row["pnl_status"], p_row["qty"]) == ("HARD_SL", "FINAL", 250)
            assert (b_row["exit_reason"], b_row["pnl_status"], b_row["qty"]) == ("TAKE_PROFIT", "FINAL", 250)
            assert p_row["exit_avg"] == 30.00 and b_row["exit_avg"] == 48.00     # the bid, never the LTP
            assert (p_row["is_placebo"], b_row["is_placebo"], b_row["parent_structure_id"]) == (
                0, 1, parent.structure_id)
            d.paper_equals_engine()
            with d.store.tx():                             # G1 pairs the placebo with its parent (coverage 1.0)
                g1.run_nightly(d.store, d.clock.now_wall(), d.clock.mono())
            (card,) = [c for c in g1.scorecards(d.store) if c["strategy"] == SEAM_STRAT]
            assert (card["n"], card["placebo_n"], card["placebo_coverage"]) == (1, 1, 1.0)
            d.quiet()
        finally:
            d.close()


def test_engine_eod_on_the_paper_exchange(tmp_path):
    """The EOD timeline (§12.5) with the real paper exchange: positions opened before NO_NEW_AFTER exit EOD_FLATTEN at
    15:15 through the exit loop; at 15:33 the PAPER and SHADOW tradebooks come from the exchange and the truth check
    passes; the PAPER contract-note sample goes to Kite's calculator on lane-reads, never to the paper exchange (it
    could only bill it with titan2.charges: review M1), and here Kite refuses the synthetic ids, so the charges
    check records VCN_PAPER_UNSUPPORTED, never a pass; Exit(0) at 15:40 and nothing else is sent."""
    with store_mod.core_thread():
        d = EngineDesk(tmp_path, "14:49:00")
        try:
            d.start()
            parent, placebo = d.enter("seameod00001")
            d.run((ist_datetime(S, "15:14:30") - d.clock.now_wall()).total_seconds(), dt=30.0)
            assert parent.state is StructState.OPEN and placebo.state is StructState.OPEN
            for _ in range(240):                           # 15:15:00 flatten, the exits fill, verify-flat
                d.run(0.5)
                if parent.is_terminal and placebo.is_terminal:
                    break
            assert parent.state is StructState.CLOSED and placebo.state is StructState.CLOSED
            assert {r["exit_reason"] for r in d.store.trades(session=S)} == {"EOD_FLATTEN"}
            d.run((ist_datetime(S, "15:40:05") - d.clock.now_wall()).total_seconds(), dt=15.0)
            truth = {e["payload"]["account"]: e["payload"]["ok"] for e in d.store.events(kind="EOD_TRUTH_CHECK")}
            assert truth == {"PAPER": True, "SHADOW": True}
            assert [c.account for c in d.sent if isinstance(c, ContractNote)] == [Account.PAPER]
            (vcn,) = d.store.events(kind="CHARGES_CHECK")
            assert vcn["payload"] == {"account": "PAPER", "result": "VCN_PAPER_UNSUPPORTED"}   # no circular pass
            assert not d.store.events(kind="CHARGES_DRIFT") and not d.store.events(kind="EOD_TRUTH_MISMATCH")
            assert [c.code for c in d.sent if isinstance(c, Exit)] == [0] and d.eng.phase == "DONE"
            assert not [o for o in d.px.orders_today(Account.PAPER) if o.tag == SQOFF_TAG]   # flat before 15:26
            d.paper_equals_engine()
            d.quiet()
        finally:
            d.close()
