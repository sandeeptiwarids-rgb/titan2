"""U13: the entry gateway (titan2.core.gateway), the SingleLegHandler (titan2.core.single_leg) and the exit loop
(titan2.core.exit_loop).

Real Store (tmp), QuoteCache, InstrumentStore, gates, sizing, exits, order/position state machines and recon.
FakeEngine is U14's Engine service surface (core/engine.py docstring; core/state.py GatewayPort / ExitLoopPort)
implemented the same way over those real parts, with U14's own EngineState for the memory: each call runs in one
store.tx() at the clock's time, broker events go through orders.handle and the engine's terminal-order reactions,
and every book's GateContext is built from titan2.db rows. test_engine_contract pins the fake to the real Engine.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import math
from collections import Counter
from datetime import date, timedelta

import pytest

from titan2 import store as store_mod
from titan2.charges import rates_for
from titan2.clock import FakeClock, SessionCalendar, ist_datetime, session_of
from titan2.core import exit_loop, exits, gateway, orders, positions, recon, risk
from titan2.core.events import (
    Alert, FetchMargins, FetchOrders, FetchPositions, FetchQuotes, FetchTradebook, IntentReceived, MarginsResult,
    OrderSnapshot, OrdersSnapshot, Place, PlaceResult, Priority, PositionsSnapshot, QuoteResult, RecoveryStart,
    Subscribe, Timer, TradebookSnapshot,
)
from titan2.core.exit_loop import EXIT_DIFFICULTY_ATTEMPTS, EXIT_STUCK_ATTEMPTS
from titan2.core.orders import OrderTimers
from titan2.core.recon import BotView, CoverPlan, ReconMemory
from titan2.core.risk import BookState, GateContext, LivePreconditions
from titan2.core.single_leg import (
    OPEN, VOID, WAIT, HandlerCtx, SingleLegHandler, StructureHandler, placebo_intent_id,
)
from titan2.core.state import BrokerView, EngineState, ExitLoopPort, GatewayPort
from titan2.instruments import InstrumentStore
from titan2.market.quotes import QuoteCache
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, BrokerPosition, Direction, ExitProfile, FeedHealth, HaltClears, Instrument,
    Intent, LegState, Level, Margins, Mode, OrderState, Policy, Quote, Recheck, Refusal, Role, Side, StrikePref,
    StructState, StructureType, Underlying, Urgency,
)
from titan2.settings import DEFAULTS, PROTECTIVE_BAND
from titan2.store import Store

S = date(2026, 10, 12)                        # a Monday session
EXPIRY = date(2026, 10, 27)
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]})
STRAT, VER = "price_spike_up", "1.abcdef0123"
PAPER_BOOK, LIVE_BOOK, SHADOW_BOOK, PLACEBO_BOOK = (f"{k}:{STRAT}" for k in ("PAPER", "LIVE", "SHADOW", "PLACEBO"))
REL, INFY, TCS = "NSE:RELIANCE", "NSE:INFY", "NSE:TCS"
SPOTS = {REL: 2900.0, INFY: 1500.0, TCS: 4000.0}
TICK = 0.05


def opt(name: str, strike: float, ot: str) -> str:
    return f"NFO:{name}26OCT{strike:g}{ot}"


REL_CE, REL_PE = opt("RELIANCE", 2900, "CE"), opt("RELIANCE", 2900, "PE")
INFY_CE, INFY_PE = opt("INFY", 1500, "CE"), opt("INFY", 1500, "PE")
TCS_CE, TCS_PE = opt("TCS", 4000, "CE"), opt("TCS", 4000, "PE")
_ids = itertools.count(1)


def instruments() -> InstrumentStore:
    unders = [Underlying("NSE:NIFTY 50", 256265, "NIFTY", "INDEX"),
              Underlying("NSE:NIFTY BANK", 260105, "BANKNIFTY", "INDEX"),
              Underlying(REL, 738561, "RELIANCE", "STOCK"), Underlying(INFY, 408065, "INFY", "STOCK"),
              Underlying(TCS, 2953217, "TCS", "STOCK")]
    contracts, tok = [], 70_000
    for name, und, strikes, expiries, lot in (
            ("RELIANCE", REL, (2800, 2850, 2900, 2950, 3000), (EXPIRY,), 250),
            ("INFY", INFY, (1460, 1480, 1500, 1520, 1540), (EXPIRY,), 400),
            ("TCS", TCS, (3900, 3950, 4000, 4050, 4100), (EXPIRY,), 175),
            ("NIFTY", "NSE:NIFTY 50", (25000, 25050), (date(2026, 10, 13), EXPIRY), 65),
            ("BANKNIFTY", "NSE:NIFTY BANK", (56000, 56100), (EXPIRY, date(2026, 11, 24)), 30)):
        for e in expiries:
            for k in strikes:
                for ot in ("CE", "PE"):
                    tok += 1
                    ts = f"{name}{'26OCT' if e == EXPIRY else e.strftime('%y%m%d')}{k:g}{ot}"
                    contracts.append(Instrument(f"NFO:{ts}", tok, ts, "NFO", name, und, ot, float(k), e, lot, TICK))
    return InstrumentStore(S, CAL, contracts, unders, min_stocks=1)


INSTRUMENTS = instruments()
_CRITICAL_HALTS = frozenset({"INVARIANT", "GATE_ERRORS", "EXIT_DIFFICULTY", "MISMATCH"})


class FakeEngine:
    """U14's Engine as the gateway and exit loop see it (same service names, parameters and behavior)."""

    def __init__(self, tmp_path, start: str = "10:00:00", mode: Mode = Mode.PAPER) -> None:
        self.clock = FakeClock(ist_datetime(S, start))
        self.store = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        self.settings = DEFAULTS
        self.calendar = CAL
        self.cache = QuoteCache(mono=self.clock.mono)
        self.quotes = self.cache
        self.instruments = INSTRUMENTS
        h = SingleLegHandler()
        self.handlers = {StructureType.LONG_CE: h, StructureType.LONG_PE: h}
        self.state = EngineState(session=S, structures={}, legs={}, orders={})
        self.set_mode(mode)
        self.exit_profiles = {STRAT: ExitProfile()}
        self.live = LivePreconditions(allow_live=True, unlocked=True, selftest_passed=True, demoted=False,
                                      first_week=False)
        self.sent: list = []
        self.closed: list[int] = []
        self._asked: set = set()
        self.begin()

    def set_mode(self, mode: Mode) -> None:
        self.state.modes = {STRAT: {"strategy": STRAT, "version": VER, "mode": str(mode), "pending_mode": None}}

    # -- the engine's per-event plumbing ------------------------------------------------------------------------
    def begin(self) -> None:
        self.now_mono, self.now_wall = self.clock.mono(), self.clock.now_wall()
        self.session = session_of(self.now_wall)
        self.rates = rates_for(self.session)
        self.eod = exits.EodTimes.for_session(self.session, self.settings)
        self._asked.clear()

    def track(self, cmds):
        for c in cmds or ():
            if isinstance(c, FetchPositions):
                self.state.broker[Account(c.account)].pos_pending += 1
            elif isinstance(c, FetchOrders):
                self.state.broker[Account(c.account)].ords_pending += 1
        self.sent += list(cmds or ())
        return cmds

    def call(self, fn, *args):
        self.begin()
        with self.store.tx():
            return self.track(fn(self, *args))

    # -- services (core/engine.py) ------------------------------------------------------------------------------
    def event(self, kind: str, payload: dict | None = None, severity: str = "INFO", **refs) -> int:
        return self.store.append_event(kind, wall=self.now_wall, mono=self.now_mono, payload=payload or {},
                                       severity=severity, session=self.session, **refs)

    def add_halt(self, kind: str, scope: str, reason: str, *, clears=None, operator: bool = False,
                 alert: bool = True) -> list:
        rule = risk.HALT_RULES.get(kind)
        clears = HaltClears(clears) if clears is not None else (rule.clears if rule is not None else HaltClears.ACK)
        h, new = self.store.add_halt(scope, kind, str(reason)[:500], clears, self.now_wall, session=self.session,
                                     operator=operator)
        if not new:
            return []
        self.state.halts = tuple(self.state.halts) + (h,)
        severity = "CRITICAL" if kind in _CRITICAL_HALTS else "WARN"
        return [Alert(severity, f"HALT_{kind}", f"HALT {kind} [{scope}]: {h.reason}", f"halt:{kind}:{scope}")] \
            if alert else []

    def apply_halt(self, req, *, alert: bool = True) -> list:
        return self.add_halt(req.kind, req.scope, req.reason, clears=req.clears, alert=alert)

    def profile(self, strategy: str) -> ExitProfile:
        return self.exit_profiles.get(strategy) or ExitProfile()

    def flag_impaired(self, strategy: str, detail: str) -> None:
        if self.store.flag_session(self.session, strategy, detail[:300]):
            self.event("SESSION_IMPAIRED", {"strategy": strategy, "detail": detail[:300]}, "WARN", strategy=strategy)

    def reserve(self, intent, *, book, account, policy, is_placebo=None, parent_structure_id=None, blocked_by=None):
        st = positions.reserve(self.store, intent, book=book, account=account, policy=policy, now_mono=self.now_mono,
                               now_wall=self.now_wall, is_placebo=is_placebo,
                               parent_structure_id=parent_structure_id, blocked_by=blocked_by)
        self.state.structures[st.structure_id] = st
        return st

    def void(self, st, reason: str) -> list:
        positions.void(self.store, st, self.state.legs_of(st), reason, now_mono=self.now_mono, now_wall=self.now_wall)
        return []

    def live_allowed(self, st, role) -> bool:
        if Account(st.account) is not Account.LIVE or Role(role) is not Role.ENTRY:
            return True
        lp = self.live
        return lp.allow_live and lp.unlocked and lp.selftest_passed and not lp.demoted

    def begin_entry(self, st, *, inst, qty, limit_px, decision_quote, decision, role=Role.ENTRY):
        leg, o, fx = positions.begin_entry(self.store, self.state.orders, st, inst=inst, qty=qty, limit_px=limit_px,
                                           decision_quote=decision_quote, decision=decision, now_mono=self.now_mono,
                                           now_wall=self.now_wall, role=role, live_allowed=self.live_allowed(st, role))
        self.state.legs[leg.leg_id] = leg
        self.state.timers[o.order_pk] = OrderTimers()
        return leg, o, list(fx.commands)

    def create_order(self, leg, role, qty, limit_px, *, decision=None, band_idx=None):
        st = self.state.structures[leg.structure_id]
        o, fx = orders.create_order(self.store, self.state.orders, leg, role=Role(role), qty=qty, limit_px=limit_px,
                                    now_mono=self.now_mono, now_wall=self.now_wall, decision=decision,
                                    band_idx=band_idx, live_allowed=self.live_allowed(st, role))
        self.state.timers[o.order_pk] = OrderTimers()
        return o, list(fx.commands)

    def close(self, st, residual_qty: int = 0) -> list:
        row, fx = positions.close(self.store, st, self.state.legs_of(st), now_mono=self.now_mono,
                                  now_wall=self.now_wall, residual_qty=residual_qty)
        self.closed.append(st.structure_id)
        out = list(fx.commands)
        h = risk.loss_streak_halt(st.book, float(row["net"]), self.settings)
        if h is not None:
            out += self.apply_halt(h)
        for leg in self.state.legs_of(st):
            for d in (self.state.cap_wait, self.state.verify, self.state.exit_memory):
                d.pop(leg.leg_id, None)
        return out

    def request_quotes(self, symbols, priority) -> list:
        new = [s for s in dict.fromkeys(symbols) if not self.state.refresh.get(s, exits.NO_REFRESH).in_flight]
        if not new:
            return []
        rid = f"q:test:{next(_ids)}"
        for s in new:
            prev = self.state.refresh.get(s, exits.NO_REFRESH)
            self.state.refresh[s] = exits.RefreshState(True, prev.last_ok_mono, prev.last_fail_mono)
        self.state.quote_requests[rid] = (tuple(new), self.now_mono)
        return [FetchQuotes(rid, tuple(new), int(priority))]

    def request_positions(self, account, priority=Priority.EXIT) -> list:
        acct = Account(account)
        if self.state.broker[acct].pos_pending or ("P", acct) in self._asked:
            return []
        self._asked.add(("P", acct))
        return [FetchPositions(acct, int(priority))]

    def request_orders(self, account, priority=Priority.EXIT) -> list:
        acct = Account(account)
        if self.state.broker[acct].ords_pending or ("O", acct) in self._asked:
            return []
        self._asked.add(("O", acct))
        return [FetchOrders(acct, int(priority))]

    def bot_view(self, account) -> BotView:
        acct = Account(account)
        legs = tuple(lg for _, lg in sorted(self.state.legs.items())
                     if Account(lg.account) is acct and (not lg.is_terminal or lg.session == self.session))
        return BotView(acct, self.session, self.settings.BROKER_SQUAREOFF_AT, legs)

    def symbol_view(self, account, symbol: str):
        return recon.symbol_view(self.bot_view(account), symbol)

    def book_state(self, book: str) -> BookState:
        s, session = self.settings, self.session
        c = self.store.book_counters(book, session, self.now_wall)
        live = [x for x in self.state.live_structures() if x.book == book]
        cool = self.store.cooldowns(book, session, reentry_min=s.REENTRY_COOLDOWN_MIN,
                                    reject_min=s.REJECT_COOLDOWN_MIN, void_min=s.VOID_COOLDOWN_MIN)
        return BookState(book, risk.book_policy(book) or Policy.FULL,
                         open=sum(1 for x in live if x.state is not StructState.RESERVED),
                         reserved=sum(1 for x in live if x.state is StructState.RESERVED), entries=c.entries,
                         attempts=c.attempts, orders_today=c.orders_today, entry_orders_60s=c.entry_orders_60s,
                         realized_net=c.realized_net, mtm_net=0.0, first_loss_today=c.first_loss_today,
                         cooldown_until=cool, active_underlyings=frozenset(x.underlying for x in live),
                         pending=sum(1 for x in live if x.session == session and x.opened_wall is None
                                     and x.state in (StructState.RESERVED, StructState.ENTERING)))

    def gate_context(self, book: str) -> GateContext:
        live = risk.book_account(book) is Account.LIVE
        live_books = sorted({x.book for x in self.state.structures.values() if x.account is Account.LIVE} | {book}
                            if live else ())
        return GateContext(
            now_mono=self.now_mono, now_wall=self.now_wall, session=self.session, settings=self.settings,
            calendar=self.calendar, clock_synced=True, instruments_loaded=True, rates=self.rates,
            instruments=self.instruments, ban_list=frozenset(), blocked_symbols=frozenset(),
            feed=FeedHealth(True, self.now_mono - 300.0, self.now_mono - 0.1, 0, False), signals_degraded=False,
            recon={a: ReconMemory(status="CLEAN", last_clean_mono=self.now_mono - 1.0, clean_streak=2)
                   for a in Account},
            margins={a: MarginsResult(a, True, Margins(a, 500_000.0, {}), self.now_mono - 1.0) for a in Account},
            halts=tuple(self.state.halts), strategy_modes={k: Mode(r["mode"]) for k, r in self.state.modes.items()},
            running_versions={STRAT: VER}, disabled_strategies=frozenset(), structure_types=frozenset(self.handlers),
            book=self.book_state(book),
            live_account=risk.live_account_state([self.book_state(b) for b in live_books]) if live else None,
            live=self.live if live else None)

    # -- the engine's reactions the tests drive -----------------------------------------------------------------
    def deliver(self, ev) -> list:
        """An order event: orders.handle, then the engine's terminal reactions (_order_terminal)."""
        self.begin()
        with self.store.tx():
            if isinstance(ev, OrderSnapshot):
                o = orders.find_order(self.state.orders, ev.account, tag=ev.tag, broker_order_id=ev.broker_order_id)
            else:
                o = self.state.orders[ev.order_pk]
            leg = self.state.legs[o.leg_id]
            fx = orders.handle(self.store, o, leg, ev, self.state.timers.setdefault(o.order_pk, OrderTimers()),
                               self.now_mono, self.now_wall)
            out = list(fx.commands)
            for h in fx.halts:
                out += self.apply_halt(h, alert=False)
            st = self.state.structures[leg.structure_id]
            if fx.became_terminal and o.role in (Role.ENTRY, Role.SELFTEST):
                if leg.state is LegState.ENTERING and st.state is StructState.ENTERING and not self.state.busy(leg):
                    efx = positions.on_entry_terminal(self.store, st, leg, profile=self.profile(st.strategy),
                                                      now_mono=self.now_mono, now_wall=self.now_wall)
                    out += list(efx.commands)
                    if st.state is StructState.EXITING:            # levels that fail I8: straight to the exit
                        out += exit_loop.step(self, leg)
            elif fx.became_terminal and o.role in (Role.EXIT, Role.COVER):
                out += exit_loop.on_order_terminal(self, o, leg)
            return self.track(out)

    def answer(self, f: FetchQuotes, ok: bool = True, error: str | None = None):
        """A QuoteResult: an engine refresh (state.refresh, then the legs on those symbols step) or the gateway's."""
        got = tuple(s for s in f.symbols if self.cache.last(s) is not None) if ok else ()
        ev = QuoteResult(f.request_id, ok, got, tuple(s for s in f.symbols if s not in got), error)
        self.begin()
        with self.store.tx():
            if ev.request_id in self.state.quote_requests:
                syms, _ = self.state.quote_requests.pop(ev.request_id)
                for s in syms:
                    prev = self.state.refresh.get(s, exits.NO_REFRESH)
                    if s in ev.symbols:
                        self.state.refresh[s] = exits.RefreshState(False, self.now_mono, prev.last_fail_mono)
                    else:
                        self.state.refresh[s] = exits.RefreshState(False, prev.last_ok_mono, self.now_mono)
                out = []
                for leg in self.state.legs_in(LegState.EXITING, LegState.VERIFY_FLAT):
                    if leg.inst.symbol in syms:
                        out += exit_loop.step(self, leg)
                return self.track(out)
            return self.track(gateway.on_quote_result(self, ev))

    def exits(self) -> list:
        """The Timer's exit pass: exit_loop.step for every EXITING / VERIFY_FLAT leg, then for every other leg that
        holds exit-loop memory (a cover can hang off any leg)."""
        self.begin()
        with self.store.tx():
            out = []
            stepped = set()
            for leg in self.state.legs_in(LegState.EXITING, LegState.VERIFY_FLAT):
                stepped.add(leg.leg_id)
                out += exit_loop.step(self, leg)
            for lid in sorted(self.state.exit_memory):
                if lid not in stepped and lid in self.state.legs:
                    out += exit_loop.step(self, self.state.legs[lid])
            return self.track(out)

    def timer(self) -> list:
        return self.call(gateway.on_timer)

    # -- the market and helpers ---------------------------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def book(self, symbol: str, bid: float | None = None, ask: float | None = None, *, qty: int = 5000,
             bids=None, asks=None, src: str = "WS", trade_age_s: float = 2.0) -> Quote:
        if bids is None:
            bids = [] if bid is None else [(round(bid - i * TICK, 2), qty) for i in range(3)]
        if asks is None:
            asks = [] if ask is None else [(round(ask + i * TICK, 2), qty) for i in range(3)]
        now = self.clock.now_wall()
        q = Quote(symbol, self.instruments.by_symbol(symbol).token, tuple(Level(p, n) for p, n in bids),
                  tuple(Level(p, n) for p, n in asks), ask or bid, 100_000, now - timedelta(seconds=trade_age_s),
                  now, self.clock.mono(), now, src)
        self.cache.put(q)
        return q

    def spot(self, underlying: str, ltp: float | None = None) -> Quote:
        now = self.clock.now_wall()
        q = Quote(underlying, self.instruments.underlying(underlying).token, (), (), ltp or SPOTS[underlying],
                  1_000_000, now, now, self.clock.mono(), now, "WS")
        self.cache.put(q)
        return q

    def intent(self, underlying: str = REL, direction: Direction = Direction.UP, ttl: float = 10.0,
               pref: StrikePref = StrikePref.ATM) -> Intent:
        spot = SPOTS[underlying]
        return Intent(intent_id=f"i{next(_ids):011d}", strategy=STRAT, strategy_version=VER, underlying=underlying,
                      direction=direction,
                      structure=StructureType.LONG_CE if direction is Direction.UP else StructureType.LONG_PE,
                      strike_pref=pref, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=spot,
                      recheck=Recheck(spot * 0.995, spot * 1.01), ttl_s=ttl, created_mono=self.clock.mono(),
                      created_wall=self.clock.now_wall(), reason={"m": 1.2})

    def receive(self, intent: Intent) -> list:
        return self.call(gateway.on_intent, intent)

    def fill(self, o, filled: int | None = None, avg: float | None = None, msg: str | None = None) -> list:
        """The broker accepts the IOC, then reports it terminal with `filled` (default: all of it)."""
        bid = f"B{next(_ids):09d}"
        out = self.deliver(PlaceResult(o.account, o.order_pk, "OK", bid))
        filled = o.qty if filled is None else filled
        status = "COMPLETE" if filled == o.qty else "CANCELLED"
        return out + self.deliver(OrderSnapshot(o.account, bid, o.order_pk, status, filled, avg if filled else None,
                                                msg, None, "SIM", self.clock.mono()))

    def reject(self, o, msg: str) -> list:
        return self.deliver(PlaceResult(o.account, o.order_pk, "REJECTED", None, "InputException", msg))

    def positions_view(self, account: Account, symbol: str, net: int, req_mono: float | None = None) -> None:
        """A PositionsSnapshot arrives (Engine._on_positions: one read answered, the newest OK one kept)."""
        bv = self.state.broker[account]
        bv.pos_pending = max(0, bv.pos_pending - 1)
        req = self.clock.mono() if req_mono is None else req_mono
        row = BrokerPosition(account, symbol, symbol.split(":")[1], "NFO", "MIS", net, max(net, 0), 0, 0.0, 0.0)
        bv.positions = PositionsSnapshot(account, True, (row,), req, self.clock.mono())

    def halt(self, kind: str, scope: str = "GLOBAL", operator: bool = False) -> None:
        self.begin()
        with self.store.tx():
            self.add_halt(kind, scope, "test", operator=operator)

    def close_db(self) -> None:
        self.store.close()


def cmds(out, kind: type) -> list:
    return [c for c in (out or ()) if isinstance(c, kind)]


def alerts(out, code: str) -> list[Alert]:
    return [c for c in (out or ()) if isinstance(c, Alert) and c.code == code]


def events(d: FakeEngine, kind: str) -> list[dict]:
    return d.store.events(kind=kind)


def structures_of(d: FakeEngine, intent: Intent) -> list:
    return [x for x in d.state.structures.values() if x.intent.intent_id == intent.intent_id]


@pytest.fixture
def desk(tmp_path):
    with store_mod.core_thread():
        d = FakeEngine(tmp_path)
        yield d
        d.close_db()


@pytest.fixture
def make_desk(tmp_path):
    made = []

    def make(**kw) -> FakeEngine:
        made.append(FakeEngine(tmp_path, **kw))
        return made[-1]

    with store_mod.core_thread():
        yield make
        for d in made:
            d.close_db()


def enter(d: FakeEngine, underlying: str = REL, ce: tuple[float, float] = (42.00, 42.10),
          pe: tuple[float, float] = (38.00, 38.10)) -> tuple:
    """intent -> one fetch -> decision -> the ENTRY Place (PAPER mode; its placebo's Place follows it in out).
    Returns (intent, structure, order, out)."""
    d.spot(underlying)
    i = d.intent(underlying)
    (f,) = cmds(d.receive(i), FetchQuotes)
    name = underlying.split(":")[1]
    k = SPOTS[underlying]
    d.advance(0.2)
    d.book(opt(name, k, "CE"), *ce)
    d.book(opt(name, k, "PE"), *pe)
    out = d.answer(f)
    pl = cmds(out, Place)[0]
    o = d.state.orders[pl.order_pk]
    return i, d.state.structures[o.structure_id], o, out


def open_and_exit(d: FakeEngine, reason: str = "HARD_SL") -> tuple:
    """An OPEN 1-lot RELIANCE CE position (filled at 42.10) moved to EXITING."""
    _, st, o, _ = enter(d)
    d.advance(0.3)
    d.fill(o, avg=42.10)
    leg = d.state.legs[o.leg_id]
    assert st.state is StructState.OPEN and leg.open_qty == 250
    d.begin()
    with d.store.tx():
        assert positions.begin_exit(d.store, st, leg, reason, Urgency.PROTECTIVE, now_mono=d.now_mono,
                                    now_wall=d.now_wall)
    return st, leg


def exit_orders(leg) -> list:
    return [o for o in leg.leg_orders() if o.role is Role.EXIT]


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
class ReadCounter:
    """A QuoteCache stand-in that moves the book whenever a symbol is read again: one decision snapshot means each
    symbol is read once per QuoteResult, and the LIVE book and its PAPER twin see the same quotes."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.reads: Counter = Counter()

    def fresh(self, symbol, max_age_s, now_mono=None):
        return self.inner.fresh(symbol, max_age_s, now_mono)

    def last(self, symbol):
        self.reads[symbol] += 1
        q = self.inner.last(symbol)
        if q is None or self.reads[symbol] == 1:
            return q
        bump = tuple(Level(round(lv.price + 1.0, 2), lv.qty) for lv in q.asks)
        return Quote(q.symbol, q.token, q.bids, bump, q.ltp, q.volume, q.last_trade_wall, q.exch_wall,
                     q.recv_mono, q.recv_wall, q.source)


def test_twin_single_quote_fetch(make_desk):
    d = make_desk(mode=Mode.LIVE)
    d.spot(REL)
    i = d.intent(REL)
    out = d.receive(i)
    (f,) = cmds(out, FetchQuotes)                              # ONE decision fetch for both books
    assert f.request_id == i.intent_id and f.priority == Priority.ENTRY_LIVE
    assert set(f.symbols) == {REL_CE, REL_PE, REL}            # option, the PAPER twin's placebo contract, underlying
    routes = {r["book"]: r["outcome"] for r in d.store.intent_routes(i.intent_id)}
    assert routes == {LIVE_BOOK: "RESERVED", PAPER_BOOK: "RESERVED"}
    assert cmds(out, Place) == []

    d.advance(0.2)
    d.book(REL_CE, 42.00, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    d.quotes = ReadCounter(d.cache)
    out = d.answer(f)
    places = cmds(out, Place)
    assert [p.account for p in places] == [Account.LIVE, Account.PAPER, Account.SHADOW]   # LIVE first; the PAPER
    assert d.quotes.reads[REL_CE] == 1 and all(n == 1 for n in d.quotes.reads.values())   # twin's placebo with it
    live, paper = (d.state.orders[p.order_pk] for p in places[:2])
    assert live.decision == paper.decision and live.decision.ask == 42.10        # one snapshot for both
    assert (live.limit_px, live.qty) == (paper.limit_px, paper.qty) == (42.20, 250)
    assert places[0].req.tag.startswith("T2EL") and places[1].req.tag.startswith("T2EP")
    lst, pst = d.state.structures[live.structure_id], d.state.structures[paper.structure_id]
    assert "placebo" not in lst.decision and pst.decision["placebo"]["inst"]["symbol"] == REL_PE
    assert lst.decision["plan"] == pst.decision["plan"]

    # the account guard refuses the LIVE book: the refusal is recorded, the PAPER twin still trades and the fetch
    # drops to PAPER priority
    d.live = dataclasses.replace(d.live, selftest_passed=False)
    d.quotes = d.cache
    d.spot(INFY)
    i2 = d.intent(INFY)
    (f2,) = cmds(d.receive(i2), FetchQuotes)
    routes = {r["book"]: (r["outcome"], r["gate"]) for r in d.store.intent_routes(i2.intent_id)}
    assert routes == {LIVE_BOOK: ("REFUSED", "LIVE"), PAPER_BOOK: ("RESERVED", None)}
    assert f2.priority == Priority.PAPER


def test_post_quote_gates_then_place(desk):
    d = desk
    d.spot(REL)
    i = d.intent(REL)
    (f,) = cmds(d.receive(i), FetchQuotes)
    (st,) = structures_of(d, i)
    assert st.state is StructState.RESERVED and st.legs == []
    # G19 on the fetched book: a 1.7% spread (the limit fits, the spread cap does not) is refused before any
    # order row exists
    d.book(REL_CE, 41.40, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    out = d.answer(f)
    assert cmds(out, Place) == [] and st.state is StructState.VOID and st.void_reason.startswith("LIQUIDITY:SPREAD")
    assert d.store.scalar("SELECT COUNT(*) FROM orders") == 0 and d.store.scalar("SELECT COUNT(*) FROM legs") == 0

    # a good book: G14-G21 pass, G22 passes, then leg + ENTRY (SUBMITTING) + decision_json, then Place + Subscribe
    d.spot(INFY)
    i2 = d.intent(INFY)
    (f2,) = cmds(d.receive(i2), FetchQuotes)
    d.advance(0.3)
    q = d.book(INFY_CE, 30.00, 30.10)
    d.book(INFY_PE, 29.00, 29.10)
    out = d.answer(f2)
    pl, _placebo = cmds(out, Place)                            # the parent, then its placebo (same transaction)
    sub = cmds(out, Subscribe)[0]
    o = d.state.orders[pl.order_pk]
    st2 = d.state.structures[o.structure_id]
    leg = d.state.legs[o.leg_id]
    assert (o.state, o.role, o.side, o.qty) == (OrderState.SUBMITTING, Role.ENTRY, Side.BUY, 400)
    assert o.limit_px == exits.entry_limit(q, TICK, 400) == 30.20 and pl.req.limit_px == 30.20 and pl.req.qty == 400
    assert o.decision.ask == 30.10 and o.decision.walk_px == 30.10 and o.decision.src == "WS"
    assert sub.mode == "full" and sub.tokens == (leg.inst.token,)
    assert st2.state is StructState.ENTERING and leg.state is LegState.ENTERING and leg.inst.symbol == INFY_CE
    plan = st2.decision["plan"]
    assert (plan["qty"], plan["lots"], plan["binding"], plan["limit"]) == (400, 1, "LOTS", 30.20)
    assert st2.decision["quotes"][INFY_CE]["ask"] == 30.10 and st2.decision["placebo"]["qty"] == 400
    stored = d.store.query_one("SELECT state, decision_json FROM structures WHERE structure_id=?",
                               (st2.structure_id,))
    assert stored["state"] == "ENTERING" and '"binding":"LOTS"' in stored["decision_json"]

    # G22: a halt raised after the reservation refuses before the order row is written
    d.spot(TCS)
    i3 = d.intent(TCS)
    (f3,) = cmds(d.receive(i3), FetchQuotes)
    d.halt("INVARIANT")
    d.book(TCS_CE, 60.00, 60.10)
    d.book(TCS_PE, 55.00, 55.10)
    n_orders = d.store.scalar("SELECT COUNT(*) FROM orders")
    out = d.answer(f3)
    (st3,) = structures_of(d, i3)
    assert cmds(out, Place) == [] and st3.state is StructState.VOID and st3.void_reason.startswith("FINAL:HALTS")
    assert d.store.scalar("SELECT COUNT(*) FROM orders") == n_orders


def test_refusal_voids_cooldown(desk):
    d = desk
    d.spot(REL)
    (f,) = cmds(d.receive(d.intent(REL)), FetchQuotes)
    d.book(REL_CE, 41.40, 42.10)                              # 1.7% spread: LIQUIDITY refuses after the fetch
    d.book(REL_PE, 38.00, 38.10)
    d.answer(f)
    (st,) = d.state.structures.values()
    assert st.state is StructState.VOID and st.void_reason.startswith("LIQUIDITY:") and st.closed_wall is not None
    assert d.store.cooldown_until(PAPER_BOOK, REL, S, reentry_min=30, reject_min=10, void_min=5) == \
        st.closed_wall + timedelta(minutes=5)

    # the VOID cooldown (5 min) refuses the next intent on (PAPER book, RELIANCE) at G11 -- no fetch, no slot
    d.advance(60)
    d.spot(REL)
    i2 = d.intent(REL)
    out = d.receive(i2)
    (r,) = d.store.intent_routes(i2.intent_id)
    assert cmds(out, FetchQuotes) == [] and (r["outcome"], r["gate"]) == ("REFUSED", "COOLDOWN")
    d.advance(4 * 60)
    d.spot(REL)
    i3 = d.intent(REL)
    (f3,) = cmds(d.receive(i3), FetchQuotes)
    (r,) = d.store.intent_routes(i3.intent_id)
    assert r["outcome"] == "RESERVED"

    # a broker reject voids with REJECTED:<msg> and starts the longer reject cooldown (10 min)
    d.book(REL_CE, 42.00, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    pl = cmds(d.answer(f3), Place)[0]
    o = d.state.orders[pl.order_pk]
    d.reject(o, "Insufficient funds")
    st3 = d.state.structures[o.structure_id]
    assert st3.state is StructState.VOID and st3.void_reason == "REJECTED:Insufficient funds"
    d.advance(6 * 60)
    d.spot(REL)
    i4 = d.intent(REL)
    d.receive(i4)
    (r,) = d.store.intent_routes(i4.intent_id)
    assert (r["outcome"], r["gate"]) == ("REFUSED", "COOLDOWN")
    d.advance(4 * 60 + 1)
    d.spot(REL)
    i5 = d.intent(REL)
    (f5,) = cmds(d.receive(i5), FetchQuotes)

    # a failed fetch voids every structure of the intent (QUOTE), which also cools down
    d.answer(f5, ok=False, error="TIMEOUT")
    (st5,) = structures_of(d, i5)
    assert st5.state is StructState.VOID and st5.void_reason == "QUOTE:FETCH_FAILED: TIMEOUT"
    assert events(d, "QUOTE_TIMEOUT")


def test_placebo_is_sent_with_its_parent(desk):
    """F-18 (§10.6): one decision returns two Places from one transaction, the PAPER parent and the SHADOW
    PLACEBO:<s> opposite option at the limit planned on the same snapshot. The parent is placebo_status OPENED
    before any fill, PLACEBO_OPENED is written and the placebo's intents row is a placebo of the parent's intent. A
    book that moves before the parent fills changes neither order: the price path during the parent's latency no
    longer decides which placebos fill. (Replaces the test that pinned the placebo waiting for the parent's fill.)"""
    d = desk
    d.spot(REL)
    i = d.intent(REL)
    (f,) = cmds(d.receive(i), FetchQuotes)
    assert REL_PE in f.symbols and f.priority == Priority.PAPER   # the placebo rides on the parent's one fetch
    d.advance(0.2)
    decided_wall = d.clock.now_wall()
    d.book(REL_CE, 42.00, 42.10)
    qpe = d.book(REL_PE, 38.00, 38.10)
    out = d.answer(f)
    pl, ppl = cmds(out, Place)                                 # both orders, after one commit
    parent = d.state.structures[d.state.orders[pl.order_pk].structure_id]
    po = d.state.orders[ppl.order_pk]
    pst = d.state.structures[po.structure_id]
    assert (pl.account, pl.req.symbol) == (Account.PAPER, REL_CE) and pl.req.tag.startswith("T2EP")
    assert (pst.book, pst.account, pst.policy, pst.is_placebo) == (PLACEBO_BOOK, Account.SHADOW, Policy.QUALITY, True)
    assert pst.parent_structure_id == parent.structure_id and pst.type is StructureType.LONG_PE
    plan = parent.decision["placebo"]
    assert plan["inst"]["symbol"] == REL_PE and plan["qty"] == 250 and plan["dq"]["ask"] == 38.10
    assert (po.symbol, po.qty, po.role, po.state) == (REL_PE, 250, Role.ENTRY, OrderState.SUBMITTING)
    assert po.limit_px == exits.entry_limit(qpe, TICK, 250) == 38.20 == ppl.req.limit_px
    assert po.decision.ask == 38.10 and po.decision.bid == 38.00 and ppl.req.tag.startswith("T2ES")
    assert parent.state is StructState.ENTERING and parent.placebo_status == "OPENED"      # before any fill
    assert d.store.scalar("SELECT placebo_status FROM structures WHERE structure_id=?",
                          (parent.structure_id,)) == "OPENED"
    assert [e["payload"]["placebo_structure_id"] for e in events(d, "PLACEBO_OPENED")] == [pst.structure_id]
    row = d.store.query_one("SELECT * FROM intents WHERE intent_id=?", (placebo_intent_id(i.intent_id),))
    assert (row["is_placebo"], row["parent_intent_id"], row["direction"], row["structure"]) == (
        1, i.intent_id, "DOWN", "LONG_PE")
    assert row["created_ts"] == store_mod.iso(decided_wall)                 # stamped at the decision time
    (route,) = d.store.intent_routes(row["intent_id"])
    assert (route["book"], route["outcome"], route["structure_id"]) == (PLACEBO_BOOK, "RESERVED",
                                                                       pst.structure_id)
    # the market moves before the parent fills: nothing is re-priced and the fill sends nothing new
    d.advance(0.4)
    d.book(REL_PE, 39.50, 39.60)
    d.spot(REL, 2890.0)
    out = d.fill(d.state.orders[pl.order_pk], avg=42.10)
    assert parent.state is StructState.OPEN and cmds(out, Place) == []
    assert (po.limit_px, po.decision.ask) == (38.20, 38.10) and len(events(d, "PLACEBO_OPENED")) == 1


def test_placebo_refused_at_the_decision_is_skipped_at_once(desk):
    """A placebo whose own contract fails G19 on the decision snapshot is recorded on the parent at the decision,
    SKIPPED:LIQUIDITY (G1 counts it against coverage), and no placebo order goes out, then or at the fill."""
    d = desk
    d.spot(INFY)
    (f,) = cmds(d.receive(d.intent(INFY)), FetchQuotes)
    d.book(INFY_CE, 30.00, 30.10)
    d.book(INFY_PE, 28.40, 28.90)                              # 1.7% spread on the placebo's contract
    (pl,) = cmds(d.answer(f), Place)
    parent = d.state.structures[d.state.orders[pl.order_pk].structure_id]
    assert pl.account is Account.PAPER and parent.decision["placebo"]["refused"]["gate"] == "LIQUIDITY"
    assert parent.state is StructState.ENTERING and parent.placebo_status == "SKIPPED:LIQUIDITY"
    assert [e["payload"]["gate"] for e in events(d, "PLACEBO_SKIPPED")] == ["LIQUIDITY"]
    assert not [x for x in d.state.structures.values() if x.parent_structure_id == parent.structure_id]
    out = d.fill(d.state.orders[pl.order_pk], avg=30.10)
    assert cmds(out, Place) == [] and parent.placebo_status == "SKIPPED:LIQUIDITY"

    # with the book full (2 positions) a third PAPER intent is refused at G09: no fetch, so no placebo plan either
    enter(d)
    d.spot(TCS)
    i3 = d.intent(TCS)
    assert cmds(d.receive(i3), FetchQuotes) == []
    (r,) = d.store.intent_routes(i3.intent_id)
    assert (r["outcome"], r["gate"]) == ("REFUSED", "BOOK_CAPS")


def test_exit_loop_cap_uncapped(desk):
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)                                             # past the post-fill lag: reads can count now
    d.book(REL_CE, 41.00, 41.10)
    out = d.exits()
    assert cmds(out, Place) == [] and cmds(out, FetchPositions) == [FetchPositions(Account.PAPER, Priority.EXIT)]
    for _ in range(4):                                         # the positions read is in flight: no second request
        d.advance(1.0)
        d.book(REL_CE, 41.00, 41.10)
        out = d.exits()
        assert cmds(out, Place) == [] and cmds(out, FetchPositions) == []
    d.advance(1.2)                                             # 5 s: fall back to the orders view
    d.book(REL_CE, 41.00, 41.10)
    out = d.exits()
    assert cmds(out, Place) == [] and cmds(out, FetchOrders) == [FetchOrders(Account.PAPER, Priority.EXIT)]
    d.advance(3.0)
    d.book(REL_CE, 41.00, 41.10)
    assert cmds(d.exits(), Place) == []
    d.advance(2.0)                                             # 10 s with no usable broker read: own fills
    d.book(REL_CE, 41.00, 41.10)
    out = d.exits()
    (pl,) = cmds(out, Place)
    assert alerts(out, "EXIT_UNCAPPED") and events(d, "EXIT_UNCAPPED")
    o = d.state.orders[pl.order_pk]
    assert (o.role, o.side, o.qty, o.limit_px, o.band_idx) == (Role.EXIT, Side.SELL, 250, 40.90, 0)
    (attempt,) = events(d, "EXIT_ATTEMPT")
    assert attempt["payload"]["cap_source"] == "UNCAPPED"

    # with a post-fill positions read the cap is the broker's net long: flat there -> place nothing, ask recon
    d.fill(o, filled=0)                                        # the IOC found no buyer
    d.advance(0.5)
    d.positions_view(Account.PAPER, REL_CE, 0)
    d.book(REL_CE, 41.00, 41.10)
    out = d.exits()
    assert cmds(out, Place) == [] and leg.open_qty == 250      # external wait: never sells what the broker lacks
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.advance(0.1)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.exits(), Place)
    assert d.state.orders[pl.order_pk].qty == 250 and d.state.orders[pl.order_pk].band_idx == 1   # under-fill: +1


def test_no_bid_no_order(desk):
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, bids=[], asks=[(41.10, 5000)])             # fresh, but nobody is buying
    out = d.exits()
    assert cmds(out, Place) == []
    (rq,) = cmds(out, FetchQuotes)
    assert rq.symbols == (REL_CE,) and rq.priority == Priority.EXIT
    (nb,) = events(d, "NO_BID")
    assert nb["severity"] == "INFO"
    d.advance(0.5)
    d.book(REL_CE, bids=[], asks=[(41.10, 5000)])
    out = d.exits()
    assert cmds(out, Place) == [] and cmds(out, FetchQuotes) == []          # the re-quote is in flight
    d.answer(rq)                                               # REST confirms the empty book
    assert d.state.refresh[REL_CE].in_flight is False
    d.advance(0.6)
    d.book(REL_CE, bids=[], asks=[(41.10, 5000)])
    out = d.exits()
    assert len(cmds(out, FetchQuotes)) == 1 and cmds(out, Place) == []      # re-quote at most 1/s
    warned = []
    for _ in range(10):
        d.advance(1.0)
        d.book(REL_CE, bids=[], asks=[(41.10, 5000)])
        out = d.exits()
        assert cmds(out, Place) == []
        warned += alerts(out, "NO_BID")
        for c in cmds(out, FetchQuotes):
            d.answer(c)
    assert len(warned) == 1 and warned[0].severity == "WARN"
    assert [e["severity"] for e in events(d, "NO_BID")] == ["INFO", "WARN"]
    d.advance(0.3)
    d.book(REL_CE, 40.50, 40.60)                               # bids are back: the exit goes out
    (pl,) = cmds(d.exits(), Place)
    assert d.state.orders[pl.order_pk].limit_px == 40.40

    # from 15:20 an empty book is CRITICAL
    d.fill(d.state.orders[pl.order_pk], filled=0)
    d.clock.advance_to("15:19:59")
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, bids=[], asks=[(41.10, 5000)])
    assert alerts(d.exits(), "NO_BID") == []
    d.advance(1.5)
    d.book(REL_CE, bids=[], asks=[(41.10, 5000)])
    out = d.exits()
    assert [a.severity for a in alerts(out, "NO_BID")] == ["CRITICAL"] and cmds(out, Place) == []


def test_stop_sending(make_desk):
    d = make_desk(start="14:30:00")
    st, leg = open_and_exit(d, reason="EOD_FLATTEN")
    d.clock.advance_to("15:24:59")
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.exits(), Place)                             # before 15:25 the ladder still sends
    assert d.state.orders[pl.order_pk].band_idx == PROTECTIVE_BAND          # after the 15:22 backstop: band >= 3
    d.advance(0.5)
    d.fill(d.state.orders[pl.order_pk], filled=0)
    d.clock.advance_to("15:25:00")
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, 41.00, 41.10)
    out = d.exits()
    assert cmds(out, Place) == []
    (crit,) = alerts(out, "NOT_FLAT_STOP_SENDING")
    assert crit.severity == "CRITICAL" and "square-off" in crit.text
    (ev,) = [e for e in events(d, "EXIT_STUCK") if e["payload"].get("why") == "STOP_SENDING"]
    assert ev["severity"] == "CRITICAL"
    for _ in range(8):                                         # once only, and never an order again
        d.advance(0.5)
        d.book(REL_CE, 41.00, 41.10)
        out = d.exits()
        assert cmds(out, Place) == [] and alerts(out, "NOT_FLAT_STOP_SENDING") == []
    assert leg.state is LegState.EXITING and d.store.scalar("SELECT COUNT(*) FROM trades") == 0


# ---------------------------------------------------------------------------------------------------------------
# The engine contract (U14)
# ---------------------------------------------------------------------------------------------------------------
_SERVICES = ("reserve", "void", "begin_entry", "create_order", "close", "request_quotes", "request_positions",
             "request_orders", "symbol_view", "bot_view", "event", "apply_halt", "add_halt", "flag_impaired",
             "profile", "gate_context")


def test_engine_contract():
    """The gateway and exit loop satisfy U14's ports, and every engine service they call exists on the real Engine
    with the parameters FakeEngine (and so these tests) use."""
    from titan2.core.engine import Engine

    for port, module in ((GatewayPort, gateway), (ExitLoopPort, exit_loop)):
        for name, fn in inspect.getmembers(port, inspect.isfunction):
            if name.startswith("_"):              # Python 3.10 lists Protocol's own __subclasshook__ etc. here
                continue
            mine = getattr(module, name)
            assert list(inspect.signature(mine).parameters) == list(inspect.signature(fn).parameters)[1:], name
    for name in _SERVICES:
        real = inspect.signature(getattr(Engine, name)).parameters
        fake = inspect.signature(getattr(FakeEngine, name)).parameters
        assert list(real) == list(fake), name
    st = EngineState(session=S, structures={}, legs={}, orders={})
    for attr in ("entry_memory", "exit_memory", "cap_wait", "verify", "refresh", "modes", "broker",
                 "quote_requests"):
        assert hasattr(st, attr), attr
    assert {"positions", "orders", "pos_pending", "ords_pending"} <= set(BrokerView.__slots__)


def test_real_engine_round_trip(tmp_path):
    """U14's Engine.handle() drives the gateway and the exit loop end to end: recovery and CLEAN recons, one decision
    fetch, the PAPER entry and its placebo sent together, a deep HARD_SL breach, the EXIT, verify-flat and the booked
    trade."""
    from titan2.core.engine import Engine, EngineContext

    clock = FakeClock(ist_datetime(S, "09:58:00"))
    cache = QuoteCache(mono=clock.mono)
    h = SingleLegHandler()
    ctx = EngineContext(settings=DEFAULTS, calendar=CAL, clock=clock, quotes=cache, versions={STRAT: VER},
                        exit_profiles={STRAT: ExitProfile()},
                        handlers={StructureType.LONG_CE: h, StructureType.LONG_PE: h},
                        instruments=lambda: INSTRUMENTS, ban_list=lambda: frozenset(),
                        feed_health=lambda: FeedHealth(True, clock.mono() - 600.0, clock.mono() - 0.1, 0, False),
                        start_mono=clock.mono())
    ctx.prep.update({"INSTRUMENTS": True, "BAN_LIST": True, "PREV_BARS": True})
    held = {a: () for a in Account}

    def book(sym, bid, ask):
        now = clock.now_wall()
        cache.put(Quote(sym, INSTRUMENTS.by_symbol(sym).token, tuple(Level(round(bid - i * TICK, 2), 5000)
                                                                     for i in range(3)),
                        tuple(Level(round(ask + i * TICK, 2), 5000) for i in range(3)), ask, 1,
                        now - timedelta(seconds=2), now, clock.mono(), now, "WS"))

    with store_mod.core_thread():
        store = Store(tmp_path / "titan2.db", backup_dir=tmp_path / "backups")
        try:
            eng = Engine.load(store, ctx)

            def handle(ev) -> list:
                with store.tx():
                    out = eng.handle(ev)
                    eng.check_invariants()
                for c in list(out):                            # the lanes answer every account read
                    if isinstance(c, FetchPositions):
                        out += handle(PositionsSnapshot(c.account, True, held[c.account], clock.mono(), clock.mono()))
                    elif isinstance(c, FetchOrders):
                        out += handle(OrdersSnapshot(c.account, True, (), clock.mono(), clock.mono()))
                    elif isinstance(c, FetchTradebook):
                        out += handle(TradebookSnapshot(c.account, True, (), clock.mono()))
                    elif isinstance(c, FetchMargins):
                        out += handle(MarginsResult(c.account, True, Margins(c.account, 500_000.0, {}),
                                                    clock.mono()))
                return out

            def tick(n: int, dt: float = 0.25, sym: str | None = None, bid_ask=None) -> list:
                out = []
                for _ in range(n):
                    clock.advance(dt)
                    if sym:
                        book(sym, *bid_ask)
                    out += handle(Timer(clock.now_wall(), clock.mono()))
                return out

            handle(RecoveryStart())
            tick(40, 0.5)
            assert eng.state.entries_resumed and eng.phase == "TRADING"
            now = clock.now_wall()
            cache.put(Quote(REL, 738561, (), (), 2900.0, 1, now, now, clock.mono(), now, "WS"))
            intent = Intent("e2e000000001", STRAT, VER, REL, Direction.UP, StructureType.LONG_CE, StrikePref.ATM,
                            EXPIRY_RULE_NEAREST, 2900.0, Recheck(2885.0, 2930.0), 10.0, clock.mono(), now, {"m": 1.0})
            (f,) = cmds(handle(IntentReceived(intent)), FetchQuotes)
            assert f.request_id == intent.intent_id and set(f.symbols) == {REL_CE, REL_PE, REL}
            clock.advance(0.2)
            book(REL_CE, 42.00, 42.10)
            book(REL_PE, 38.00, 38.10)
            pl, placebo = cmds(handle(QuoteResult(f.request_id, True, f.symbols)), Place)   # one decision
            assert (pl.account, pl.req.symbol, pl.req.qty, pl.req.limit_px) == (Account.PAPER, REL_CE, 250, 42.20)
            assert (placebo.account, placebo.req.symbol, placebo.req.limit_px) == (Account.SHADOW, REL_PE, 38.20)
            clock.advance(0.3)
            handle(PlaceResult(pl.account, pl.order_pk, "OK", "P000000001"))
            out = handle(OrderSnapshot(pl.account, "P000000001", pl.order_pk, "COMPLETE", 250, 42.10, None, None,
                                       "SIM", clock.mono()))
            held[Account.PAPER] = (BrokerPosition(Account.PAPER, REL_CE, "x", "NFO", "MIS", 250, 250, 0, 10525.0,
                                                  0.0),)
            assert cmds(out, Place) == []                     # the fill sends nothing: the placebo went with it
            clock.advance(2.5)
            out = tick(8, sym=REL_CE, bid_ask=(30.00, 30.10))  # far below the hard stop: no debounce
            (x,) = [c for c in cmds(out, Place) if c.req.role is Role.EXIT]
            assert (x.req.qty, x.req.limit_px) == (250, 29.90)
            clock.advance(0.3)
            handle(PlaceResult(x.account, x.order_pk, "OK", "P000000002"))
            handle(OrderSnapshot(x.account, "P000000002", x.order_pk, "COMPLETE", 250, 29.90, None, None, "SIM",
                                 clock.mono()))
            held[Account.PAPER] = ()
            tick(16)
            st = eng.state.structures[eng.state.orders[pl.order_pk].structure_id]
            assert st.state is StructState.CLOSED and st.exit_reason == "HARD_SL" and st.placebo_status == "OPENED"
            row = store.trade(st.structure_id)
            assert (row["qty"], row["entry_avg"], row["exit_avg"], row["pnl_status"]) == (250, 42.10, 29.90, "FINAL")
            assert row["net"] == round(row["gross"] - row["charges"], 2) < -3050.0
        finally:
            store.close()


# ---------------------------------------------------------------------------------------------------------------
# More of the gateway
# ---------------------------------------------------------------------------------------------------------------
def test_placebo_of_a_parent_that_never_fills(desk):
    """The placebo goes out with its parent (F-18), so a parent that ends VOID leaves an ordinary SHADOW trade behind,
    managed by the normal exits; nothing new is sent when the parent's entry ends, however late. G1 pairs placebos
    only with the parents in its sample, so it never counts this one. (Replaces the TTL test of the fill-time
    placebo: there is no fill-time plan left to expire.)"""
    d = desk
    _, parent, o, out = enter(d, TCS, ce=(60.00, 60.10), pe=(55.00, 55.10))
    (ppl,) = [p for p in cmds(out, Place) if p.account is Account.SHADOW]
    po = d.state.orders[ppl.order_pk]
    pst = d.state.structures[po.structure_id]
    assert parent.placebo_status == "OPENED" and pst.parent_structure_id == parent.structure_id
    d.advance(11.0)                                            # past the placebo intent's TTL
    out = d.fill(o, filled=0)                                  # the parent's IOC: nothing filled
    assert parent.state is StructState.VOID and cmds(out, Place) == [] and parent.placebo_status == "OPENED"
    d.fill(po, avg=55.10)                                      # the placebo fills on its own
    assert pst.state is StructState.OPEN and d.state.legs[po.leg_id].open_qty == 175


def test_stale_spot_fetches_the_underlying_first(desk):
    d = desk
    d.spot(REL)
    d.advance(3.0)                                             # the underlying LTP is now older than 2 s
    i = d.intent(REL)
    (f,) = cmds(d.receive(i), FetchQuotes)
    assert f.request_id == i.intent_id + gateway.SPOT_SUFFIX and f.symbols == (REL,)
    d.spot(REL)                                                # the REST answer lands in the QuoteCache
    (f2,) = cmds(d.answer(f), FetchQuotes)
    assert f2.request_id == i.intent_id and set(f2.symbols) == {REL_CE, REL_PE, REL}
    d.book(REL_CE, 42.00, 42.10)
    d.book(REL_PE, 38.00, 38.10)
    assert [p.account for p in cmds(d.answer(f2), Place)] == [Account.PAPER, Account.SHADOW]   # with its placebo
    assert d.answer(f2) is None                                # a repeated answer is no longer the gateway's


def test_select_refusal_voids_without_fetch(desk):
    d = desk
    d.spot(REL, 2600.0)                                        # far below the listed strikes: no ATM, no clamp
    i = dataclasses.replace(d.intent(REL), recheck=Recheck(-math.inf, math.inf))
    out = d.receive(i)
    (st,) = d.state.structures.values()
    assert cmds(out, FetchQuotes) == [] and st.state is StructState.VOID
    assert st.void_reason.startswith("CONTRACT:SPOT_OUTSIDE_STRIKES")


def test_operator_halt_diverts_paper_to_shadow(desk):
    d = desk
    d.halt("PAUSE", operator=True)
    d.spot(REL)
    i = d.intent(REL)
    (f,) = cmds(d.receive(i), FetchQuotes)
    routes = {r["book"]: (r["outcome"], r["gate"]) for r in d.store.intent_routes(i.intent_id)}
    assert routes == {PAPER_BOOK: ("DIVERTED", "HALTS"), SHADOW_BOOK: ("RESERVED", None)}
    (st,) = d.state.structures.values()
    assert (st.book, st.account, st.policy, st.blocked_by) == (SHADOW_BOOK, Account.SHADOW, Policy.QUALITY, "PAUSE")
    assert d.store.session_flags(strategy=STRAT)[0]["flag"] == "IMPAIRED"
    assert f.priority == Priority.SHADOW and REL_PE not in f.symbols       # a SHADOW book has no placebo
    d.book(REL_CE, 42.00, 42.10)
    (pl,) = cmds(d.answer(f), Place)
    assert pl.account is Account.SHADOW and "placebo" not in d.state.structures[st.structure_id].decision


def test_invalid_duplicate_and_unrouted_intents(desk):
    d = desk
    d.spot(REL)
    good = d.intent(REL)
    out = d.receive(dataclasses.replace(good, intent_id="x" * 70))
    assert alerts(out, "INTENT_INVALID") and d.store.scalar("SELECT COUNT(*) FROM intents") == 0
    assert len(cmds(d.receive(good), FetchQuotes)) == 1
    out = d.receive(good)                                      # a replay never routes twice
    assert cmds(out, FetchQuotes) == [] and alerts(out, "INTENT_INVALID")
    d.set_mode(Mode.OFF)
    d.spot(INFY)
    i = d.intent(INFY)
    assert cmds(d.receive(i), FetchQuotes) == [] and d.store.intent_routes(i.intent_id) == []


def test_timer_voids_expired_and_lost(desk):
    d = desk
    d.spot(REL)
    i = d.intent(REL, ttl=3.0)
    (f,) = cmds(d.receive(i), FetchQuotes)
    (st,) = d.state.structures.values()
    d.advance(2.0)
    d.timer()
    assert st.state is StructState.RESERVED                    # still inside its TTL, fetch not yet lost
    d.advance(1.5)
    d.timer()                                                  # no answer within the 3 s TTL: expired
    assert st.state is StructState.VOID and st.void_reason.startswith("TTL:EXPIRED")
    assert events(d, "INTENT_EXPIRED")
    d.book(REL_CE, 42.00, 42.10)
    out = d.answer(f)                                          # a late answer finds nothing to place
    assert out is not None and cmds(out, Place) == []
    # a 10 s intent whose fetch is lost is voided after 5 s (QUOTE), before its TTL
    d.spot(INFY)
    i2 = d.intent(INFY)
    d.receive(i2)
    (st2,) = structures_of(d, i2)
    d.advance(5.1)
    d.timer()
    assert st2.state is StructState.VOID and st2.void_reason.startswith("QUOTE:FETCH_LOST")


def test_reserve_slot_taken_is_a_refusal(desk):
    d = desk
    d.spot(REL)
    (f,) = cmds(d.receive(d.intent(REL)), FetchQuotes)
    (st,) = d.state.structures.values()
    d.state.structures.pop(st.structure_id)                    # the gate's view misses it; the DB index does not
    i2 = d.intent(REL)
    out = d.receive(i2)
    (r,) = d.store.intent_routes(i2.intent_id)
    assert (r["outcome"], r["gate"]) == ("REFUSED", "UNDERLYING") and r["detail"].startswith("DB_SLOT_TAKEN")
    assert cmds(out, FetchQuotes) == [] and alerts(out, "RESERVE_REFUSED")


def test_gate_errors_counted_and_halted(desk):
    d = desk
    d.spot(REL)
    real = d.gate_context
    d.gate_context = lambda book: dataclasses.replace(real(book), calendar=None)    # G05 raises on a missing calendar
    for n in range(5):
        i = d.intent(REL)
        out = d.receive(i)
        (r,) = d.store.intent_routes(i.intent_id)
        assert (r["outcome"], r["gate"], r["detail"]) == ("REFUSED", "SESSION", risk.GATE_ERROR)
        assert alerts(out, "GATE_ERROR")
    assert len(events(d, "GATE_ERROR")) == 5
    assert [h.kind for h in d.state.halts] == ["GATE_ERRORS"]
    assert alerts(out, "HALT_GATE_ERRORS")


def test_fetch_priority():
    assert gateway.fetch_priority({Account.LIVE, Account.PAPER}) == Priority.ENTRY_LIVE
    assert gateway.fetch_priority({Account.PAPER}) == gateway.fetch_priority({Account.PAPER, Account.SHADOW}) \
        == Priority.PAPER
    assert gateway.fetch_priority({Account.SHADOW}) == Priority.SHADOW


# ---------------------------------------------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------------------------------------------
def test_single_leg_handler_protocol(desk):
    d = desk
    h = SingleLegHandler()
    assert isinstance(h, StructureHandler) and h.types == frozenset({StructureType.LONG_CE, StructureType.LONG_PE})
    ctx = gateway.handler_ctx(d)
    i = d.intent(REL)
    assert h.select(i, ctx) == Refusal("CONTRACT", "NO_SPOT: no NSE:RELIANCE LTP within 2 s")
    d.spot(REL)
    (ce,) = h.select(i, ctx)
    assert ce.symbol == REL_CE and h.quote_symbols(i, [ce], False) == (REL_CE, REL)
    p = h.placebo_intent(i, ctx)
    assert (p.intent_id, p.direction, p.structure, p.is_placebo, p.parent_intent_id) == (
        i.intent_id + "-P", Direction.DOWN, StructureType.LONG_PE, True, i.intent_id)
    assert (p.strike_pref, p.recheck, p.ttl_s, p.created_wall) == (i.strike_pref, i.recheck, i.ttl_s, ctx.now_wall)
    assert h.placebo_intent(p, ctx) is None                     # no placebo of a placebo
    (pe,) = h.select(p, ctx)
    assert pe.symbol == REL_PE and pe.expiry == ce.expiry      # the same strike rule and expiry rule

    # plan_entry without a gate snapshot fails closed; a gate that raises is a GATE_ERROR with its text kept
    q = {REL_CE: d.book(REL_CE, 42.00, 42.10), REL: d.cache.last(REL)}
    assert h.plan_entry(i, [ce], q, ctx) == Refusal("FINAL", "NO_GATE_CONTEXT")
    gc = d.gate_context(PAPER_BOOK)
    plan = h.plan_entry(i, [ce], q, gateway.handler_ctx(d, gates=gc))
    (lp,) = plan.legs
    assert (lp.qty, lp.limit_px, lp.side, lp.role) == (250, 42.20, Side.BUY, Role.ENTRY)
    assert plan.risk_basis_rs == round(250 * 42.20, 2) and lp.decision_quote.walk_px == 42.10
    bctx = gateway.handler_ctx(d, gates=dataclasses.replace(gc, rates=None))   # G19's hurdle raises without rates
    r = h.plan_entry(i, [ce], q, bctx)
    assert isinstance(r, Refusal) and r.detail == risk.GATE_ERROR and bctx.errors

    # entry progress, the stored plan's order spec and the exit sequence
    _, st, o, _ = enter(d, INFY, ce=(30.00, 30.10), pe=(29.00, 29.10))
    lctx = gateway.handler_ctx(d)
    assert h.on_entry_progress(st, lctx) == WAIT and h.next_entry_orders(st, lctx) == []
    assert h.exit_sequence(st) == st.legs
    d.fill(o, filled=0)
    assert st.state is StructState.VOID and h.on_entry_progress(st, lctx) == VOID


def test_handler_evaluate_is_decide(desk):
    d = desk
    _, st, o, _ = enter(d)
    d.fill(o, avg=42.10)
    h = d.handlers[StructureType.LONG_CE]
    ctx = gateway.handler_ctx(d)
    assert h.on_entry_progress(st, ctx) == OPEN and h.exit_params(st, ctx) is st.exit_params
    d.book(REL_CE, 48.50, 48.60)                               # rb above tp (15%): take profit, on the bid
    d.begin()
    dec = h.evaluate(st, d.cache, d.now_mono, d.now_wall, None, gateway.handler_ctx(d))
    assert isinstance(dec, exits.Exit) and dec.reason == "TAKE_PROFIT"
    dec = h.evaluate(st, {REL_CE: d.cache.last(REL_CE)}, d.now_mono, d.now_wall, "OPERATOR_FLATTEN", ctx)
    assert isinstance(dec, exits.Exit) and (dec.reason, dec.urgency) == ("OPERATOR_FLATTEN", Urgency.PROTECTIVE)
    assert isinstance(ctx, HandlerCtx) and ctx.refresh is d.state.refresh


# ---------------------------------------------------------------------------------------------------------------
# More of the exit loop
# ---------------------------------------------------------------------------------------------------------------
def test_band_reject_keeps_band_and_reprices(desk):
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.exits(), Place)
    first = d.state.orders[pl.order_pk]
    assert (first.limit_px, first.band_idx) == (40.90, 0)
    d.reject(first, "Price outside the LPP range")             # a price-band reject: same band, re-price
    d.advance(0.1)
    d.book(REL_CE, 41.00, 41.10)
    assert cmds(d.exits(), Place) == []                        # 250 ms before the next attempt
    d.advance(0.2)
    d.book(REL_CE, 40.80, 40.90)
    d.positions_view(Account.PAPER, REL_CE, 250)               # B1: every attempt is capped by a read of its own
    (pl,) = cmds(d.exits(), Place)
    second = d.state.orders[pl.order_pk]
    assert second.band_idx == 0 and second.limit_px == max(40.90, 40.70) == 40.90 and leg.exit_band_idx == 0
    d.advance(0.05)
    d.fill(second, filled=0)                                   # under-filled (IOC, nothing): band + 1
    assert leg.exit_band_idx == 1
    d.advance(0.3)
    d.book(REL_CE, 40.80, 40.90)
    out = d.exits()                                            # the read from before the second attempt caps nothing
    assert cmds(out, Place) == [] and cmds(out, FetchPositions) == [FetchPositions(Account.PAPER, Priority.EXIT)]
    d.positions_view(Account.PAPER, REL_CE, 250)               # (this test used to retry on that read at once)
    (pl,) = cmds(d.exits(), Place)
    third = d.state.orders[pl.order_pk]
    assert third.band_idx == 1 and third.limit_px == exits.exit_limit(d.cache.last(REL_CE), 250, TICK, 1)


def test_protective_pricing_without_fresh_quote(desk):
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    d.positions_view(Account.PAPER, REL_CE, 250)
    q = d.book(REL_CE, 41.00, 41.10)
    d.advance(1.5)                                             # the WS froze: no quote within 1 s
    out = d.exits()
    (rq,) = cmds(out, FetchQuotes)
    assert cmds(out, Place) == []                              # exiting for 4 s: wait for REST first
    d.answer(rq, ok=False, error="TIMEOUT")
    d.advance(1.2)                                             # STALE_PROTECTIVE_S since EXITING: last bid, band 3
    (pl,) = cmds(d.exits(), Place)
    o = d.state.orders[pl.order_pk]
    assert o.band_idx == PROTECTIVE_BAND and o.limit_px == exits.exit_limit(q, 250, TICK, 3, protective=True)
    assert o.decision.bid == 41.00 and o.decision.age_ms >= 2700 and leg.exit_band_idx == PROTECTIVE_BAND


def test_exit_prices_from_a_tick_received_during_the_event(desk):
    """A WS tick stamped after the event's now_mono (it landed while the core handled the event) is the newest book:
    the exit loop prices from it at once, as decide() reads it (age 0: model.Quote.age, review round 1), instead of
    asking REST and, after STALE_PROTECTIVE_S, selling protectively from an older bid."""
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    d.positions_view(Account.PAPER, REL_CE, 250)
    q = d.book(REL_CE, 41.00, 41.10)
    q = dataclasses.replace(q, recv_mono=d.clock.mono() + 0.2)     # kite-ws stamped it 0.2 s into the event
    assert d.cache.put(q)
    out = d.exits()
    assert cmds(out, FetchQuotes) == []
    (pl,) = cmds(out, Place)
    o = d.state.orders[pl.order_pk]
    assert (o.qty, o.band_idx) == (250, 0) and o.limit_px == exits.exit_limit(q, 250, TICK, 0, protective=False)
    assert o.decision.bid == 41.00 and o.decision.age_ms == 0


def test_exit_stuck_warn_then_difficulty_halt(desk):
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    stuck, difficulty = [], []
    for _ in range(EXIT_DIFFICULTY_ATTEMPTS):
        d.positions_view(Account.PAPER, REL_CE, 250)
        d.book(REL_CE, 41.00, 41.10)
        out = d.exits()
        stuck += alerts(out, "EXIT_STUCK")
        difficulty += alerts(out, "HALT_EXIT_DIFFICULTY")
        (pl,) = cmds(out, Place)
        d.advance(0.1)
        d.fill(d.state.orders[pl.order_pk], filled=0)
        d.advance(0.3)
    assert len(exit_orders(leg)) == EXIT_DIFFICULTY_ATTEMPTS > EXIT_STUCK_ATTEMPTS
    assert len(stuck) == 1 and stuck[0].severity == "WARN"
    assert [(h.kind, h.scope) for h in d.state.halts] == [("EXIT_DIFFICULTY", f"BOOK:{PAPER_BOOK}")]
    assert len(difficulty) == 1 and difficulty[0].severity == "CRITICAL"
    assert leg.exit_band_idx == 6                               # the ladder tops out at 12%; it never gives up
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, 41.00, 41.10)
    assert len(cmds(d.exits(), Place)) == 1


def test_attempts_paced_then_exhausted_without_raising(desk, monkeypatch):
    """A ladder that never fills slows down once the leg has made many attempts (ATTEMPT_PACING) and, when its
    order tags run out (model.ATTEMPT_MAX, scaled down here), sends nothing more: one CRITICAL plus
    HALT(EXIT_DIFFICULTY), and never a make_tag ValueError inside the core loop (an exception storm, exit 3 and a
    crash loop that would stop every exit)."""
    monkeypatch.setattr(exit_loop, "ATTEMPT_MAX", 9)                  # the entry is attempt 1: 8 exit attempts
    monkeypatch.setattr(exit_loop, "ATTEMPT_PACING", ((5, 3.0),))
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    sent, exhausted = [], []
    for _ in range(200):
        d.positions_view(Account.PAPER, REL_CE, 250)
        d.book(REL_CE, 41.00, 41.10)
        out = d.exits()
        exhausted += alerts(out, "EXIT_ATTEMPTS_EXHAUSTED")
        for pl in cmds(out, Place):
            sent.append((d.state.orders[pl.order_pk].attempt, d.clock.mono()))
            d.advance(0.1)
            d.fill(d.state.orders[pl.order_pk], filled=0)
        d.advance(0.25)
    assert [a for a, _ in sent] == list(range(2, 10))                 # attempts 2..9, then none
    gaps = [round(b - a, 2) for (_, a), (_, b) in zip(sent, sent[1:])]
    assert all(g < 1.0 for g in gaps[:3]) and all(g >= 3.0 for g in gaps[4:])     # paced from the 5th attempt
    assert len(exhausted) == 1 and exhausted[0].severity == "CRITICAL"
    assert ("EXIT_DIFFICULTY", f"BOOK:{PAPER_BOOK}") in [(h.kind, h.scope) for h in d.state.halts]
    assert leg.state is LegState.EXITING and leg.open_qty == 250       # still open: the square-off is the backstop
    assert exit_loop.attempts_left(leg) == 0


def test_repeated_rejects_back_off(desk):
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    gaps, last = [], None
    for _ in range(5):
        while True:
            d.positions_view(Account.PAPER, REL_CE, 250)
            d.book(REL_CE, 41.00, 41.10)
            out = d.exits()
            if cmds(out, Place):
                break
            d.advance(0.05)
        (pl,) = cmds(out, Place)
        if last is not None:
            gaps.append(round(d.clock.mono() - last, 2))
        d.reject(d.state.orders[pl.order_pk], "RMS: margin check failed")
        last = d.clock.mono()
    assert gaps[0] == pytest.approx(0.25, abs=0.06) and gaps[-1] > 1.5      # 0.25, 0.5, 1, 2 s


def test_verify_flat_closes_after_stop_sending(make_desk):
    d = make_desk(start="14:30:00")
    st, leg = open_and_exit(d)
    d.clock.advance_to("15:24:59")
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.exits(), Place)
    d.advance(0.3)
    d.fill(d.state.orders[pl.order_pk], avg=41.00)            # flat by the bot's own fills
    assert leg.open_qty == 0
    d.clock.advance_to("15:25:01")
    out = d.exits()                                            # VERIFY_FLAT: a post-fill read is needed first
    assert leg.state is LegState.VERIFY_FLAT and cmds(out, Place) == []
    d.advance(1.5)
    out = d.exits()
    assert cmds(out, FetchPositions) == [FetchPositions(Account.PAPER, Priority.EXIT)]
    d.positions_view(Account.PAPER, REL_CE, 0)                # requested after last fill + 2 s: it counts
    d.exits()
    assert st.state is StructState.CLOSED and d.closed == [st.structure_id]
    row = d.store.trade(st.structure_id)
    assert row["qty"] == 250 and row["exit_reason"] == "HARD_SL" and row["pnl_status"] == "FINAL"
    assert row["entry_avg"] == 42.10 and row["exit_avg"] == 41.00 and row["net"] < 0


def test_cover_buys_exactly_the_short(desk):
    d = desk
    st, leg = open_and_exit(d)
    d.advance(2.5)
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.exits(), Place)
    d.fill(d.state.orders[pl.order_pk], avg=41.00)
    d.advance(0.3)
    d.book(REL_CE, 41.00, 41.10)
    plan = CoverPlan(REL_CE, 500, leg.leg_id)                 # recon found a bot-explained short of 2 lots
    out = d.call(exit_loop.cover, plan)
    (pl,) = cmds(out, Place)
    c = d.state.orders[pl.order_pk]
    assert (c.role, c.side, c.qty) == (Role.COVER, Side.BUY, 500)
    assert c.limit_px == exits.cover_limit(d.cache.last(REL_CE), 500, TICK, 0) == 41.20
    assert cmds(d.call(exit_loop.cover, plan), Place) == []   # in flight: a repeated plan adds nothing
    d.fill(c, filled=250, avg=41.10)                           # half covered
    d.advance(0.3)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.exits(), Place)
    c2 = d.state.orders[pl.order_pk]
    assert (c2.role, c2.qty, c2.band_idx) == (Role.COVER, 250, 1)          # the remainder only, one band up
    d.fill(c2, avg=41.20)
    d.advance(0.3)
    d.book(REL_CE, 41.00, 41.10)
    out = d.exits()
    assert [p for p in cmds(out, Place) if d.state.orders[p.order_pk].role is Role.COVER] == []
    assert sum(o.filled_qty for o in leg.leg_orders() if o.role is Role.COVER) == 500
    assert cmds(d.call(exit_loop.cover, CoverPlan(REL_CE, 250, 999_999)), Place) == []   # unknown leg: refused


def test_cover_hangs_off_any_leg_and_leaves_no_memory(desk):
    """A cover plan may name a leg that is not exiting (here OPEN; also one whose structure closed): the exit pass
    runs its ladder on the exit loop's cadence, without waiting for recon's next plan, and once it is done the leg
    keeps no exit-loop memory, so the leg's own exit later starts fresh (band 0, its own 'exiting since')."""
    d = desk
    _, st, o, _ = enter(d)
    d.advance(0.3)
    d.fill(o, avg=42.10)
    leg = d.state.legs[o.leg_id]
    assert st.state is StructState.OPEN and leg.open_qty == 250
    d.advance(0.3)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.call(exit_loop.cover, CoverPlan(REL_CE, 500, leg.leg_id)), Place)
    c = d.state.orders[pl.order_pk]
    assert (c.role, c.side, c.qty) == (Role.COVER, Side.BUY, 500)
    d.fill(c, filled=250, avg=41.10)                           # half covered; the host leg is still OPEN
    d.advance(0.3)
    d.book(REL_CE, 41.00, 41.10)
    (pl,) = cmds(d.exits(), Place)                              # the exit pass steps the cover itself
    c2 = d.state.orders[pl.order_pk]
    assert (c2.role, c2.qty, c2.band_idx) == (Role.COVER, 250, 1)
    d.fill(c2, avg=41.20)
    for _ in range(2):                                          # done, then the leg's memory is dropped
        d.advance(0.3)
        d.book(REL_CE, 41.00, 41.10)
        assert cmds(d.exits(), Place) == []
    assert leg.leg_id not in d.state.exit_memory
    assert leg.state is LegState.OPEN and leg.open_qty == 250   # COVER fills never change the bot's own qty

    d.begin()
    with d.store.tx():
        positions.begin_exit(d.store, st, leg, "HARD_SL", Urgency.PROTECTIVE, now_mono=d.now_mono,
                             now_wall=d.now_wall)
    d.advance(2.5)
    d.positions_view(Account.PAPER, REL_CE, 250)
    d.book(REL_CE, 41.00, 41.10)
    out = d.exits()
    (x,) = [d.state.orders[p.order_pk] for p in cmds(out, Place)]
    assert (x.role, x.side, x.qty, x.band_idx) == (Role.EXIT, Side.SELL, 250, 0)
    assert not alerts(out, "EXIT_STUCK")


def test_parent_exiting_at_once_still_has_its_placebo(desk):
    """§10.6: every PAPER:<s> entry has its placebo, also one whose levels fail I8 and that goes straight to
    EXITING(ANOMALY_EXIT): it was opened at the decision (F-18), so the fill leaves placebo_status OPENED and sends no
    new ENTRY. A parent left with None would lower G1's coverage unseen."""
    d = desk
    _, parent, o, out = enter(d)
    (po,) = [d.state.orders[p.order_pk] for p in cmds(out, Place) if p.account is Account.SHADOW]
    assert (po.symbol, po.role) == (REL_PE, Role.ENTRY) and parent.placebo_status == "OPENED"
    d.exit_profiles = {STRAT: ExitProfile(hard_sl_pct=50.0)}     # outside EXIT_BOUNDS: the frozen levels are refused
    d.advance(0.3)
    out = d.fill(o, avg=42.10)
    leg = d.state.legs[o.leg_id]
    assert parent.state is StructState.EXITING and leg.exit_reason == "ANOMALY_EXIT"
    assert parent.placebo_status == "OPENED"
    assert not [p for p in cmds(out, Place) if d.state.orders[p.order_pk].role is Role.ENTRY]
