"""Regression tests for two post-build review findings (broker API lens) on titan2.core.orders and core.engine.

1. A timed-out place that Kite's OMS takes late. NOT_PLACED is the bot's own conclusion, never a broker status:
   - an AMBIGUOUS order stays UNKNOWN, looked up every 5 s, until orders.LATE_ACCEPT_S (30 s) after the answer, so no
     second attempt goes out on its leg (I4) and a late report adopts it; clean misses in that window never raise
     ORDER_STUCK, failing lookups still do; an in-process reload keeps the window;
   - a BUY given up as NOT_PLACED that the broker reports taken after all (its row is immutable and its structure
     VOID) is adopted into a RECOVERED:<account> structure that exits at once, and the broker's later reports reach the
     adopted row: never an unmanaged long (Engine._adopt_late_fill).
2. The LIVE self-test claimed "tag and order updates work" without checking either: it now passes only when a WS
   order update carried the order's tag and an orders() read (LookupTag) found it by its tag.
"""

from __future__ import annotations

import dataclasses

import pytest

from test_engine import INST, LOT, NIFTY_SPOT, Rig, make_live
from test_orders_positions import Rig as OrderRig
from titan2.core import orders
from titan2.core.events import (
    FetchQuotes, LookupResult, LookupTag, OrderSnapshot, OrdersSnapshot, Place, PlaceResult, QuoteResult,
)
from titan2.model import Account, BrokerOrder, LegState, OrderState, Quote, Role, Side, StructState

READ_TIMEOUT = ("ReadTimeout", "HTTPSConnectionPool(host='api.kite.trade'): Read timed out.")


# ---------------------------------------------------------------------------------------------------------------
# 1a. orders: the late-accept window
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def orig(store, clock):
    return OrderRig(store, clock)


@pytest.fixture
def store(tmp_path):
    from titan2 import store as store_mod
    from titan2.store import Store
    with store_mod.core_thread():
        s = Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
        yield s
        s.close()


@pytest.fixture
def clock():
    from titan2.clock import FakeClock
    return FakeClock("2026-10-12T09:20:00+05:30")


def _miss(rig: OrderRig, o, gap: float) -> list:
    """Advance gap seconds, run the order's timer and answer its LookupTags 'not found'; the commands it sent."""
    rig.clk.advance(gap)
    fx = rig.tick(o)
    for c in fx.commands:
        if isinstance(c, LookupTag):
            rig.ev(o, LookupResult(o.account, o.order_pk, True, None))
    return fx.commands


def test_an_ambiguous_place_is_looked_up_for_30s_and_a_late_find_is_adopted(orig, clock):
    rig = orig
    st, leg = rig.exiting()
    x, _ = rig.exit_order(leg, leg.open_qty)
    rig.ev(x, PlaceResult(x.account, x.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    answered = rig.mono
    for gap in (1.0, 2.0, 4.0, 8.0):                      # +1/+3/+7/+15 s: the scheduled lookups miss
        _miss(rig, x, gap)
    assert x.state is OrderState.UNKNOWN and x.lookups == 4  # +15 s no longer ends it: the OMS may still take it
    # the OMS registered the SELL after its client timeout; the +20 s lookup finds it: adopted, nothing sold twice
    clock.advance(5.0)
    fx = rig.tick(x)
    assert [c for c in fx.commands if isinstance(c, LookupTag)] and not fx.halts
    fx = rig.ev(x, LookupResult(x.account, x.order_pk, True, rig.found(x, "COMPLETE", x.qty, 44.95, "B-late")))
    assert x.state is OrderState.COMPLETE and x.broker_order_id == "B-late" and fx.became_terminal
    assert leg.open_qty == 0 and rig.mono - answered == pytest.approx(20.0)


def test_not_placed_comes_only_after_the_late_window_and_never_as_stuck(orig, clock):
    rig = orig
    st, leg, o, _ = rig.entry()
    rig.ev(o, PlaceResult(o.account, o.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    answered, halts = rig.mono, []
    while not o.is_terminal and rig.mono - answered < 60.0:
        clock.advance(0.25)
        fx = rig.tick(o)
        halts += fx.halts
        if [c for c in fx.commands if isinstance(c, LookupTag)]:
            rig.ev(o, LookupResult(o.account, o.order_pk, True, None))
    assert o.state is OrderState.NOT_PLACED and rig.mono - answered == pytest.approx(30.0)  # LATE_ACCEPT_S
    assert o.lookups == 7 and not halts and not o.stuck  # clean misses are the window at work, not a stuck order


def test_failing_lookups_still_stick_30s_after_the_last_answer(orig, clock):
    rig = orig
    st, leg, o, _ = rig.entry()
    rig.ev(o, PlaceResult(o.account, o.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    _miss(rig, o, 1.0)
    _miss(rig, o, 2.0)                                      # the last clean answer, at +3 s
    last_ok, stuck = rig.mono, []
    for _ in range(60):
        clock.advance(0.5)
        fx = rig.tick(o)
        if [c for c in fx.commands if isinstance(c, LookupTag)]:
            rig.ev(o, LookupResult(o.account, o.order_pk, False, None))     # the API errors from now on
        if fx.halts:
            stuck.append(rig.mono - last_ok)
    assert stuck == [pytest.approx(orders.LOOKUP_STUCK_AFTER_S)] and o.state is OrderState.UNKNOWN and o.stuck


def test_a_rollback_reload_keeps_the_late_window(orig, clock):
    rig = orig
    st, leg, o, _ = rig.entry()
    sub = o.submitted_mono
    rig.ev(o, PlaceResult(o.account, o.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    clock.advance(10.0)
    t, first = orders.timers_after_reload(o, rig.mono, restart=False)     # the core loop's rollback + reload
    assert first == [] and t.late_until == pytest.approx(sub + orders.PLACE_TIMEOUT_S + orders.LATE_ACCEPT_S)
    rig.timers[o.order_pk] = t
    while not o.is_terminal and rig.mono - sub < 60.0:
        _miss(rig, o, 0.25)
    assert o.state is OrderState.NOT_PLACED and rig.mono >= sub + orders.PLACE_TIMEOUT_S + orders.LATE_ACCEPT_S
    # a process restart cannot know how long ago the order went out: fresh lookups at +1/+3/+7/+15 s and a whole
    #   late-accept window from the restart (decision B2; review round 2: it had none, NOT_PLACED at +15 s)
    st2, leg2, o2, _ = rig.entry(underlying="NSE:INFY")
    rig.ev(o2, PlaceResult(o2.account, o2.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    t2, _ = orders.timers_after_reload(o2, rig.mono)
    assert t2.late_until == pytest.approx(rig.mono + orders.LATE_ACCEPT_S)
    assert t2.lookups_due == [rig.mono + s for s in (*orders.LOOKUP_SCHEDULE_S, 20.0, 25.0, 30.0)]


# ---------------------------------------------------------------------------------------------------------------
# 1b. engine: a BUY given up as NOT_PLACED that the broker fills later is adopted and exited
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path):
    from titan2 import store as store_mod
    with store_mod.core_thread():
        r = Rig(tmp_path)
        r.tick()
        yield r
        r.store.close()


def _give_up(rig: Rig, pk: str) -> None:
    """The place timed out; every tag lookup misses until the order is NOT_PLACED (its structure VOID)."""
    o = rig.eng.state.orders[pk]
    rig.run(PlaceResult(o.account, pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    for _ in range(400):
        if rig.eng.state.orders[pk].is_terminal:
            return
        for c in rig.tick(0.25):
            if isinstance(c, LookupTag) and c.order_pk == pk:
                rig.run(LookupResult(c.account, pk, True, None))
    raise AssertionError(f"{pk} was never given up")


def _late(rig: Rig, pk: str, status: str, filled: int, px: float | None, bid: str = "B-late",
          source: str = "WS") -> list:
    return rig.run(OrderSnapshot(Account.PAPER, bid, pk, status, filled, px, None, None, source, rig.clock.mono()))


def _row(pk: str, status: str, filled: int, px: float | None, bid: str = "B-late") -> BrokerOrder:
    return BrokerOrder(Account.PAPER, bid, pk, INST.symbol, INST.tradingsymbol, "NFO", "MIS", Side.BUY, "LIMIT",
                       "IOC", LOT, filled, px, status, status, None, None, None, "API")


def test_a_not_placed_entry_filled_late_is_adopted_and_exited(rig):
    rig.book(41.90, 42.15)
    pk = rig.intent()
    _give_up(rig, pk)
    gone = rig.eng.state.orders[pk]
    assert gone.state is OrderState.NOT_PLACED
    assert rig.eng.state.structures[gone.structure_id].state is StructState.VOID
    rig.book(42.30, 42.50)
    out = _late(rig, pk, "COMPLETE", LOT, 42.20)          # the OMS took the timed-out BUY after all, and it filled
    (rec,) = [s for s in rig.eng.state.structures.values() if s.book == "RECOVERED:PAPER"]
    assert rec.strategy == "_recovered" and rec.intent.reason["late_fill_of"] == pk
    (leg,) = rig.eng.state.legs_of(rec)
    x = leg.leg_orders()[0]
    assert (x.role, x.state, x.broker_order_id, x.filled_qty) == (Role.ENTRY, OrderState.COMPLETE, "B-late", LOT)
    assert x.order_pk != pk and rig.eng.state.orders[pk].state is OrderState.NOT_PLACED     # I9: never rewritten
    assert leg.state is LegState.EXITING and leg.exit_reason == "ANOMALY_EXIT"             # exits at once ...
    assert [c for c in out if isinstance(c, Place) and c.req.role is Role.EXIT and c.req.qty == LOT]   # ... a SELL
    assert [a for a in rig.alerts("LATE_FILL", "CRITICAL") if a.dedup_key == f"late_fill:{pk}"]
    assert rig.events("LATE_FILL")[-1]["payload"]["adopted"] == x.order_pk
    # the broker's later reports of that order (tag of the given-up attempt) reach the adopted row: no conflict
    n_events = len(rig.events("INVARIANT_VIOLATION"))
    for source in ("POLL", "WS"):
        _late(rig, pk, "COMPLETE", LOT, 42.20, source=source)
    rig.run(OrdersSnapshot(Account.PAPER, True, (_row(pk, "COMPLETE", LOT, 42.20),), rig.clock.mono(),
                           rig.clock.mono()))
    assert len([s for s in rig.eng.state.structures.values() if s.book == "RECOVERED:PAPER"]) == 1
    assert len(rig.events("INVARIANT_VIOLATION")) == n_events and not rig.halts("INVARIANT")


def test_an_orders_read_showing_a_given_up_entry_adopts_it(rig):
    rig.book(41.90, 42.15)
    pk = rig.intent()
    _give_up(rig, pk)
    rig.exit_loop.act = False
    rig.run(OrdersSnapshot(Account.PAPER, True, (_row(pk, "WORKING", 0, None, "B-w"),), rig.clock.mono(),
                           rig.clock.mono()))                 # the late order is still working at the exchange
    (rec,) = [s for s in rig.eng.state.structures.values() if s.book == "RECOVERED:PAPER"]
    (x,) = rig.eng.state.legs_of(rec)[0].leg_orders()
    assert (x.state, x.broker_order_id) == (OrderState.WORKING, "B-w") and rec.state is StructState.ENTERING
    rig.book(42.30, 42.50)
    _late(rig, pk, "CANCELLED", LOT, 42.20, bid="B-w")        # its fill arrives under the old tag
    assert x.state is OrderState.CANCELLED and x.filled_qty == LOT
    leg = rig.eng.state.legs_of(rec)[0]
    assert leg.state is LegState.EXITING and leg.exit_reason == "ANOMALY_EXIT" and leg.open_qty == LOT


def test_an_unfilled_late_report_adopts_nothing(rig):
    pk = rig.intent()
    _give_up(rig, pk)
    for status in ("CANCELLED", "REJECTED"):
        _late(rig, pk, status, 0, None, bid=f"B-{status}")
    assert not [s for s in rig.eng.state.structures.values() if s.book == "RECOVERED:PAPER"]
    assert not [a for a in rig.alerts() if (a.dedup_key or "").startswith("late_fill:")] and not rig.events("LATE_FILL")


def test_a_late_fill_that_cannot_be_adopted_is_alerted_once(rig):
    rig.exit_loop.act = False                             # the first adopted long stays open: it holds RELIANCE
    rig.book(41.90, 42.15)
    first = rig.intent()
    _give_up(rig, first)
    _late(rig, first, "COMPLETE", LOT, 42.20, bid="B-1")
    second = rig.intent()
    _give_up(rig, second)
    for _ in range(3):
        _late(rig, second, "COMPLETE", LOT, 42.20, bid="B-2")
    assert len([s for s in rig.eng.state.structures.values() if s.book == "RECOVERED:PAPER"]) == 1
    (blocked,) = [e for e in rig.events("LATE_FILL") if e["payload"]["adopted"] is None]
    assert blocked["order_pk"] == second
    assert len([a for a in rig.alerts("LATE_FILL", "CRITICAL") if "cannot be adopted" in a.text]) == 1


# ---------------------------------------------------------------------------------------------------------------
# 2. the LIVE self-test proves the tag round trip it claims
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def live(tmp_path):
    from titan2 import store as store_mod
    with store_mod.core_thread():
        r = Rig(tmp_path, start="2026-10-12T09:15:00+05:30", allow_live=True, instruments=True)
        r.tick()
        make_live(r)
        yield r
        r.store.close()


def _selftest_place(rig: Rig, until: str = "09:15:30") -> tuple[Place, object]:
    spot = Quote(NIFTY_SPOT, 256265, (), (), 25010.0, None, None, None, rig.clock.mono(), rig.clock.now_wall(), "WS")
    out = rig.until(until, step=1.0, before=lambda: rig.quotes.put(dataclasses.replace(
        spot, recv_mono=rig.clock.mono(), recv_wall=rig.clock.now_wall())))
    (fq,) = [c for c in out if isinstance(c, FetchQuotes)]
    opt = rig.inst_store.by_symbol(fq.symbols[0])
    rig.book(120.00, 120.50, opt)
    (place,) = [c for c in rig.run(QuoteResult(fq.request_id, True, fq.symbols)) if isinstance(c, Place)]
    return place, opt


def _found(rig: Rig, place: Place, opt, tag: str | None) -> BrokerOrder:
    o = rig.eng.state.orders[place.order_pk]
    return BrokerOrder(Account.LIVE, o.broker_order_id, tag, opt.symbol, opt.tradingsymbol, "NFO", "MIS", Side.BUY,
                       "LIMIT", "IOC", place.req.qty, 0, None, "CANCELLED", "CANCELLED", None, rig.clock.now_wall(),
                       None, "API")


def _outcomes(rig: Rig) -> list[str]:
    return [e["payload"]["outcome"] for e in rig.events("SELFTEST")]


def test_selftest_fails_when_neither_the_ws_nor_orders_echo_the_tag(live):
    rig = live
    for attempt, until in ((1, "09:15:30"), (2, "09:17:00")):
        place, opt = _selftest_place(rig, until)
        bid = rig.place_ok(place.order_pk)
        rig.clock.advance(3.2)                            # no WS update ever comes; the +3 s poll has no tag
        out = rig.run(OrderSnapshot(Account.LIVE, bid, None, "CANCELLED", 0, None, None, None, "POLL",
                                    rig.clock.mono()))
        assert rig.eng.state.orders[place.order_pk].state is OrderState.CANCELLED
        assert "PASSED" not in _outcomes(rig)             # cancelled unfilled in time is not enough ...
        (lt,) = [c for c in out if isinstance(c, LookupTag)]
        rig.run(LookupResult(Account.LIVE, place.order_pk, True, None))     # ... and orders() does not show the tag
        rig.until(f"09:{15 if attempt == 1 else 17}:{45 if attempt == 1 else 15}", step=0.5)
        assert _outcomes(rig).count("FAILED") == attempt
    assert not [a for a in rig.alerts("SELFTEST") if "passed" in a.text]
    (failed, _) = [e for e in rig.events("SELFTEST") if e["payload"]["outcome"] == "FAILED"]
    assert "no WS order update carried its tag" in failed["payload"]["detail"]
    assert "orders() never showed its tag" in failed["payload"]["detail"]
    assert rig.halts("LIVE_DISABLED_TODAY") and not rig.eng.live_preconditions("opening_drive").selftest_passed


def test_selftest_needs_the_ws_echo_too(live):
    rig = live
    place, opt = _selftest_place(rig)
    bid = rig.place_ok(place.order_pk)
    rig.clock.advance(3.2)
    rig.run(OrderSnapshot(Account.LIVE, bid, place.order_pk, "CANCELLED", 0, None, None, None, "POLL",
                          rig.clock.mono()))              # the tag comes back on a poll, never on the WS
    rig.run(LookupResult(Account.LIVE, place.order_pk, True, _found(rig, place, opt, place.order_pk)))
    rig.until("09:15:45", step=0.5)
    assert _outcomes(rig)[-1] == "FAILED" and "PASSED" not in _outcomes(rig)
    (failed,) = [e for e in rig.events("SELFTEST") if e["payload"]["outcome"] == "FAILED"]
    assert "no WS order update carried its tag" in failed["payload"]["detail"]
    assert "orders()" not in failed["payload"]["detail"]


def test_selftest_passes_on_a_ws_echo_and_an_orders_read(live):
    rig = live
    place, opt = _selftest_place(rig)
    bid = rig.place_ok(place.order_pk)
    rig.clock.advance(0.8)
    rig.run(OrderSnapshot(Account.LIVE, bid, place.order_pk, "CANCELLED", 0, None, None, None, "WS",
                          rig.clock.mono()))
    assert "PASSED" not in _outcomes(rig)
    m = rig.clock.mono()
    rig.run(OrdersSnapshot(Account.LIVE, True, (_found(rig, place, opt, place.order_pk),), m, m))
    assert _outcomes(rig)[-1] == "PASSED" and rig.eng.live_preconditions("opening_drive").selftest_passed
    (passed,) = [e for e in rig.events("SELFTEST") if e["payload"]["outcome"] == "PASSED"]
    assert passed["payload"]["latency_ms"] == pytest.approx(800, abs=1) and passed["payload"]["ws_tag"] is True
    assert "tag came back in a WS order update and in orders()" in rig.alerts("SELFTEST")[-1].text


def test_a_selftest_given_up_but_filled_late_is_exited_and_disables_live(live):
    rig = live
    place, opt = _selftest_place(rig)
    rig.run(PlaceResult(Account.LIVE, place.order_pk, "AMBIGUOUS", None, *READ_TIMEOUT))
    for _ in range(400):                                  # every tag lookup misses: given up after the late window
        if rig.eng.state.orders[place.order_pk].is_terminal:
            break
        for c in rig.tick(0.25):
            if isinstance(c, LookupTag) and c.order_pk == place.order_pk:
                rig.run(LookupResult(Account.LIVE, place.order_pk, True, None))
    assert rig.eng.state.orders[place.order_pk].state is OrderState.NOT_PLACED and "FAILED" in _outcomes(rig)
    rig.book(120.00, 120.50, opt)
    rig.run(OrderSnapshot(Account.LIVE, "B-st", place.order_pk, "COMPLETE", place.req.qty, place.req.limit_px, None,
                          None, "WS", rig.clock.mono()))   # the OMS took it after all, and it filled
    (rec,) = [s for s in rig.eng.state.structures.values() if s.book == "RECOVERED:LIVE"]
    leg = rig.eng.state.legs_of(rec)[0]
    assert leg.leg_orders()[0].role is Role.SELFTEST and leg.open_qty == place.req.qty
    assert leg.state is LegState.EXITING and leg.exit_reason == "SELFTEST_FILLED"
    assert rig.halts("LIVE_DISABLED_TODAY") and "FILLED" in _outcomes(rig)
