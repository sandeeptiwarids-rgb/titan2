"""Regression tests for the post-build review finding "an accepted order that two tag lookups miss becomes
NOT_PLACED" (titan2.core.orders, with titan2.core.recon's post-fill rule).

Kite can accept an order whose answer is lost (a timeout) and keep it out of orders() for seconds. Two lookup misses
at +1/+3 s made it NOT_PLACED: the exit loop then sold the position again (a naked short) and an entry was VOIDed
while the broker held it. Now the tag is looked up at +1/+3/+7/+15 s before NOT_PLACED, and the NOT_PLACED moment
counts like a fill for recon's post-fill rule, so the next exit is capped only by a broker read made after it.
"""

from __future__ import annotations

import itertools
import math
from datetime import date, datetime, time as dtime

import pytest

from titan2 import store as store_mod
from titan2.clock import FakeClock
from titan2.core import orders, positions, recon
from titan2.core.events import FetchPositions, LookupResult, LookupTag, PlaceResult, PositionsSnapshot, Priority, Timer
from titan2.core.orders import OrderTimers
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, BrokerOrder, BrokerPosition, DecisionQuote, Direction, ExitProfile, Instrument,
    Intent, Leg, Order, OrderState, Policy, Recheck, Role, StrikePref, StructState, StructureType, Urgency,
)
from titan2.store import Store

SESSION = date(2026, 10, 12)
QTY = 500
INST = Instrument("NFO:RELIANCE26OCT2900CE", 12345, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", "NSE:RELIANCE", "CE",
                  2900.0, date(2026, 10, 27), 500, 0.05)
ENTRY_DQ = DecisionQuote(bid=41.90, ask=42.15, mid=42.025, age_ms=180, src="REST", ltp=42.10, walk_px=42.15)
ENTRY_LIMIT = 42.25
EXIT_DQ = DecisionQuote(bid=45.00, ask=45.20, mid=45.10, age_ms=90, src="WS", ltp=45.10, walk_px=45.00)
EXIT_LIMIT = 44.90
_ids = itertools.count(1)


@pytest.fixture
def store(tmp_path):
    with store_mod.core_thread():
        s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        yield s
        s.close()


@pytest.fixture
def clock():
    return FakeClock("2026-10-12T09:20:00+05:30")


class Rig:
    """The engine's side of the order contract: one order index, per-order timers, one store.tx() per event."""

    def __init__(self, s: Store, clk: FakeClock, account: Account = Account.LIVE) -> None:
        self.s, self.clk, self.account = s, clk, account
        self.orders: dict[str, Order] = {}
        self.legs: dict[int, Leg] = {}
        self.timers: dict[str, OrderTimers] = {}

    @property
    def mono(self) -> float:
        return self.clk.mono()

    @property
    def wall(self) -> datetime:
        return self.clk.now_wall()

    def t(self, o: Order) -> OrderTimers:
        return self.timers.setdefault(o.order_pk, OrderTimers())

    def ev(self, o: Order, event) -> orders.Effects:
        with self.s.tx():
            return orders.handle(self.s, o, self.legs[o.leg_id], event, self.t(o), self.mono, self.wall)

    def entry(self):
        it = Intent(intent_id=f"r{next(_ids):011d}", strategy="opening_drive", strategy_version="1.0abc",
                    underlying="NSE:RELIANCE", direction=Direction.UP, structure=StructureType.LONG_CE,
                    strike_pref=StrikePref.ATM, expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0,
                    recheck=Recheck(-math.inf, math.inf), ttl_s=10.0, created_mono=self.mono,
                    created_wall=self.wall, reason={"why": "test"})
        with self.s.tx():
            self.s.insert_intent(it, SESSION)
            st = positions.reserve(self.s, it, book=f"{self.account}:opening_drive", account=self.account,
                                   policy=Policy.FULL, now_mono=self.mono, now_wall=self.wall)
            leg, o, _ = positions.begin_entry(self.s, self.orders, st, inst=INST, qty=QTY, limit_px=ENTRY_LIMIT,
                                              decision_quote=ENTRY_DQ, decision={"limit": ENTRY_LIMIT},
                                              now_mono=self.mono, now_wall=self.wall, role=Role.ENTRY,
                                              live_allowed=True)
        self.legs[leg.leg_id] = leg
        return st, leg, o

    def found(self, o: Order, status: str, filled: int, avg: float | None, bid: str) -> BrokerOrder:
        return BrokerOrder(o.account, bid, o.order_pk, o.symbol, o.symbol.split(":")[1], "NFO", "MIS", o.side,
                           "LIMIT", "IOC", o.qty, filled, avg, status, status, None, self.wall, None, "API")

    def exiting(self):
        st, leg, o = self.entry()
        self.ev(o, PlaceResult(o.account, o.order_pk, "OK", "B-entry"))
        self.ev(o, LookupResult(o.account, o.order_pk, True, self.found(o, "COMPLETE", QTY, 42.15, "B-entry")))
        with self.s.tx():
            positions.on_entry_terminal(self.s, st, leg, profile=ExitProfile(), now_mono=self.mono,
                                        now_wall=self.wall)
            positions.begin_exit(self.s, st, leg, "TAKE_PROFIT", Urgency.NORMAL, now_mono=self.mono,
                                 now_wall=self.wall)
        with self.s.tx():
            x, _ = orders.create_order(self.s, self.orders, leg, role=Role.EXIT, qty=QTY, limit_px=EXIT_LIMIT,
                                       now_mono=self.mono, now_wall=self.wall, decision=EXIT_DQ, band_idx=0)
        return st, leg, x

    def lookup_misses(self, o: Order, gaps) -> None:
        """Advance by each gap, run the order's timer (it must send a LookupTag) and answer 'not found'."""
        for gap in gaps:
            self.clk.advance(gap)
            fx = self.ev(o, Timer(self.wall, self.mono))
            assert [c for c in fx.commands if isinstance(c, LookupTag)], (gap, fx.commands)
            self.ev(o, LookupResult(o.account, o.order_pk, True, None))

    def miss_until_terminal(self, o: Order, limit_s: float = 120.0) -> int:
        """Step the order's timer every 0.25 s and answer every LookupTag 'not found' until the order is terminal;
        returns how many lookups that took (whatever the schedule is)."""
        n, end = 0, self.mono + limit_s
        while not o.is_terminal:
            assert self.mono < end, f"{o.order_pk} still {o.state} after {limit_s} s of lookup misses"
            self.clk.advance(0.25)
            fx = self.ev(o, Timer(self.wall, self.mono))
            for _ in [c for c in fx.commands if isinstance(c, LookupTag)]:
                n += 1
                self.ev(o, LookupResult(o.account, o.order_pk, True, None))
        return n


def _positions(net: int, req: float) -> PositionsSnapshot:
    row = BrokerPosition(Account.LIVE, INST.symbol, INST.tradingsymbol, "NFO", "MIS", net, QTY, QTY - net,
                         QTY * 42.15, (QTY - net) * 45.0)
    return PositionsSnapshot(Account.LIVE, True, (row,), req, req + 0.2, None)


def _view(rig: Rig, leg: Leg) -> recon.SymbolView:
    bot = recon.BotView(Account.LIVE, SESSION, dtime(15, 26), (leg,))
    return recon.symbol_view(bot, INST.symbol)


def test_two_lookup_misses_no_longer_make_an_accepted_exit_not_placed(store, clock):
    rig = Rig(store, clock)
    st, leg, x = rig.exiting()
    rig.ev(x, PlaceResult(x.account, x.order_pk, "AMBIGUOUS", None, "ReadTimeout", "Read timed out."))
    assert x.state is OrderState.UNKNOWN
    rig.lookup_misses(x, (1.0, 2.0))                  # +1 s and +3 s: the old NOT_PLACED point
    assert x.state is OrderState.UNKNOWN and x.lookups == 2
    rig.lookup_misses(x, (4.0,))                      # +7 s
    assert x.state is OrderState.UNKNOWN
    # +15 s: orders() finally shows the order Kite accepted at once: it is adopted, filled; nothing is sold again
    clock.advance(8.0)
    assert [c for c in rig.ev(x, Timer(rig.wall, rig.mono)).commands if isinstance(c, LookupTag)]
    fx = rig.ev(x, LookupResult(x.account, x.order_pk, True, rig.found(x, "COMPLETE", QTY, 45.0, "B-exit")))
    assert x.state is OrderState.COMPLETE and fx.became_terminal and leg.open_qty == 0
    assert not store.events(kind="INVARIANT_VIOLATION")


def test_not_placed_after_four_misses_counts_as_a_possible_fill(store, clock):
    rig = Rig(store, clock)
    st, leg, x = rig.exiting()
    decided = leg.exiting_since_mono
    before_fill = _positions(QTY, rig.mono + 0.1)     # the read the exit was capped by: requested before the SELL
    rig.ev(x, PlaceResult(x.account, x.order_pk, "AMBIGUOUS", None, "ReadTimeout", "Read timed out."))
    rig.lookup_misses(x, (1.0, 2.0, 4.0))
    assert x.state is OrderState.UNKNOWN
    late_read = _positions(QTY, rig.mono)             # a read made while the order was unresolved (+7 s)
    assert rig.miss_until_terminal(x) >= 1             # +15 s at the earliest: four misses or more
    assert x.state is OrderState.NOT_PLACED and x.filled_qty == 0 and leg.open_qty == QTY
    assert x.lookups >= orders.NOT_PLACED_LOOKUPS and rig.mono - x.submitted_mono >= orders.NOT_PLACED_AFTER_S
    np_at = rig.mono
    assert leg.last_fill_mono == np_at                # the NOT_PLACED moment is a possible fill (§9.2 post-fill)

    # the next exit attempt may not be capped by any read made before NOT_PLACED + 2 s, even one made after the
    #   exit decision: the order may exist and have sold the qty already
    sv = _view(rig, leg)
    assert sv.last_fill_mono == np_at and not sv.in_flight
    for snap in (before_fill, late_read):
        cap = recon.broker_cap(sv, QTY, snap, None, None, np_at + 0.5, account=Account.LIVE, decided_mono=decided)
        assert cap.wait and cap.commands == ()          # too early to ask: a read now could not count either
    cap = recon.broker_cap(sv, QTY, late_read, None, np_at, np_at + 2.0, account=Account.LIVE, decided_mono=decided)
    assert cap.wait and cap.commands == (FetchPositions(Account.LIVE, Priority.EXIT),)
    # a read requested >= NOT_PLACED + 2 s decides: flat (the hidden SELL filled) -> nothing to sell
    cap = recon.broker_cap(sv, QTY, _positions(0, np_at + 2.0), None, np_at, np_at + 2.3, account=Account.LIVE,
                           decided_mono=decided)
    assert (cap.qty, cap.source) == (0, "POSITIONS")
    # ... or still long (it never reached the book) -> the exit may sell it
    cap = recon.broker_cap(sv, QTY, _positions(QTY, np_at + 2.0), None, np_at, np_at + 2.3, account=Account.LIVE,
                           decided_mono=decided)
    assert (cap.qty, cap.source) == (QTY, "POSITIONS")
    # the symbol is in grace for FRESH_FILL_GRACE_S after it: recon infers no close meanwhile
    assert recon.in_grace(sv, np_at + 4.9) and not recon.in_grace(sv, np_at + 5.1)


def test_not_placed_entry_waits_for_the_late_lookups_too(store, clock):
    rig = Rig(store, clock)
    st, leg, o = rig.entry()
    rig.ev(o, PlaceResult(o.account, o.order_pk, "AMBIGUOUS", None, "ConnectionError", "Connection aborted."))
    rig.lookup_misses(o, (1.0, 2.0))
    assert o.state is OrderState.UNKNOWN                # the structure stays ENTERING: not VOIDed while unknown
    clock.advance(4.0)
    rig.ev(o, Timer(rig.wall, rig.mono))
    fx = rig.ev(o, LookupResult(o.account, o.order_pk, True, rig.found(o, "COMPLETE", QTY, 42.15, "B-late")))
    assert o.state is OrderState.COMPLETE and fx.became_terminal and leg.open_qty == QTY
    with store.tx():
        positions.on_entry_terminal(store, st, leg, profile=ExitProfile(), now_mono=rig.mono, now_wall=rig.wall)
    assert st.state is StructState.OPEN               # managed: stop, take profit and EOD flatten apply


def test_lookup_schedule_constants():
    assert orders.LOOKUP_SCHEDULE_S == (1.0, 3.0, 7.0, 15.0)
    assert (orders.NOT_PLACED_LOOKUPS, orders.NOT_PLACED_AFTER_S) == (4, 15.0)
    at, acc = [], 0.0
    for gap in orders.THROTTLE_BACKOFF_S:
        acc += gap
        at.append(acc)
    assert tuple(at) == orders.LOOKUP_SCHEDULE_S     # a throttled answer reaches the same lookup times
