"""U01: titan2.model (depth walk, tick math, tags, Quote, Leg quantities) and titan2.core.events."""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from titan2 import model as m
from titan2.core import events as ev
from titan2.model import RECEIPT_SKEW_S, Account, Level, Role, Side

IST = timezone(timedelta(hours=5, minutes=30))
WALL = datetime(2026, 10, 12, 9, 20, tzinfo=IST)


def quote(bids=(), asks=(), recv_mono=100.0, ltp=None):
    return m.Quote("NFO:X26OCT100CE", 1, bids, asks, ltp, None, None, None, recv_mono, WALL, "WS")


# ---------------------------------------------------------------------------------------------------------------
# walk
# ---------------------------------------------------------------------------------------------------------------
def test_walk_limits():
    asks = (Level(100.0, 50), Level(100.5, 100), Level(101.0, 200))
    # BUY takes asks <= limit, best first
    w = m.walk(asks, 120, 100.5, Side.BUY)
    assert w.fillable_qty == 120 and w.fillable == 120
    assert w.avg_px == pytest.approx((50 * 100.0 + 70 * 100.5) / 120)
    assert w.worst_px == 100.5
    # a limit between levels stops at the first level beyond it
    w = m.walk(asks, 120, 100.45, Side.BUY)
    assert (w.fillable_qty, w.avg_px, w.worst_px) == (50, 100.0, 100.0)
    # nothing at or under the limit: nothing fillable, no prices
    assert m.walk(asks, 120, 99.95, Side.BUY) == m.WalkResult(None, 0, None)
    # limit None = unbounded; short depth reports what is fillable
    w = m.walk(asks, 400, None, Side.BUY)
    assert w.fillable_qty == 350 and w.worst_px == 101.0
    assert w.avg_px == pytest.approx((50 * 100.0 + 100 * 100.5 + 200 * 101.0) / 350)

    bids = (Level(99.5, 100), Level(99.0, 100))
    # SELL takes bids >= limit
    w = m.walk(bids, 150, 99.2, Side.SELL)
    assert (w.fillable_qty, w.avg_px, w.worst_px) == (100, 99.5, 99.5)
    w = m.walk(bids, 150, None, Side.SELL)
    assert w.fillable_qty == 150 and w.worst_px == 99.0
    assert w.avg_px == pytest.approx((100 * 99.5 + 50 * 99.0) / 150)
    assert m.walk(bids, 150, 99.6, Side.SELL).fillable_qty == 0
    # the limit is inclusive, also when it carries float noise (0.1 + 0.2 != 0.3)
    assert m.walk((Level(0.3, 10),), 10, 0.1 + 0.2, Side.BUY).fillable_qty == 10
    assert m.walk((Level(0.3, 10),), 10, 0.1 + 0.2, Side.SELL).fillable_qty == 10
    # unsorted input and junk levels are cleaned and walked best first
    messy = [(101.0, 200), (0.0, 0), Level(100.0, 50), (100.5, 0), (math.nan, 5), Level(-1.0, 9)]
    w = m.walk(messy, 60, None, Side.BUY)
    assert (w.fillable_qty, w.worst_px) == (60, 101.0)
    assert m.walk([(99.0, 10), (99.5, 10)], 10, None, Side.SELL).avg_px == 99.5
    # degenerate inputs fail closed
    assert m.walk(asks, 0, None, Side.BUY) == m.WalkResult(None, 0, None)
    assert m.walk(asks, 10, math.nan, Side.BUY) == m.WalkResult(None, 0, None)
    assert m.walk((), 10, None, Side.SELL) == m.WalkResult(None, 0, None)
    assert m.walk(asks, 10, None, "BUY").fillable_qty == 10      # plain-string side is accepted
    with pytest.raises(TypeError):
        m.walk(asks, 10.0, None, Side.BUY)
    with pytest.raises(ValueError):
        m.walk(asks, 10, None, "HOLD")


# ---------------------------------------------------------------------------------------------------------------
# tick math
# ---------------------------------------------------------------------------------------------------------------
def test_tick_floor_ceil():
    assert m.floor_tick(100.07, 0.05) == 100.05
    assert m.ceil_tick(100.07, 0.05) == 100.1
    assert m.floor_tick(100.05, 0.05) == 100.05 and m.ceil_tick(100.05, 0.05) == 100.05
    # float noise around exact multiples (0.15 / 0.05 = 2.9999999999999996) must not drop a tick
    assert m.floor_tick(0.15, 0.05) == 0.15 and m.ceil_tick(0.15, 0.05) == 0.15
    assert m.floor_tick(0.3, 0.05) == 0.3 and m.ceil_tick(0.3, 0.05) == 0.3
    assert m.floor_tick(0.1 + 0.2, 0.05) == 0.3
    # spec usages: exit floor ref_bid x 0.88, entry cap mid x 1.01, self-test bid x 0.97
    assert m.floor_tick(32.0 * 0.88, 0.05) == 28.15 and m.ceil_tick(32.0 * 0.88, 0.05) == 28.2
    assert m.floor_tick(42.125 * 1.01, 0.05) == 42.5
    assert m.floor_tick(1.0 * 0.97, 0.05) == 0.95
    # genuinely below a multiple stays below
    assert m.floor_tick(99.99999, 0.05) == 99.95
    assert m.floor_tick(-0.03, 0.05) == -0.05            # exit_limit relies on max(tick, floor_tick(raw))
    assert m.floor_tick(1234.567, 0.01) == 1234.56 and m.ceil_tick(1234.561, 0.01) == 1234.57
    for x in (0.05, 0.35, 1.15, 17.85, 249.95, 1000.05):
        assert m.is_tick_multiple(x, 0.05), x
        assert m.floor_tick(x, 0.05) == x and m.ceil_tick(x, 0.05) == x
    assert not m.is_tick_multiple(100.07, 0.05)
    assert not m.is_tick_multiple(math.nan, 0.05) and not m.is_tick_multiple(1.0, 0.0)
    assert not m.is_tick_multiple(1.0, -0.05) and not m.is_tick_multiple(math.inf, 0.05)
    for bad in ((1.0, 0.0), (1.0, -0.05), (math.nan, 0.05), (math.inf, 0.05), (1.0, math.nan)):
        with pytest.raises(ValueError):
            m.floor_tick(*bad)
        with pytest.raises(ValueError):
            m.ceil_tick(*bad)


# ---------------------------------------------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------------------------------------------
def test_tag_roundtrip_and_parse_rejects_foreign():
    session = date(2026, 10, 12)
    assert m.make_tag(Role.ENTRY, Account.PAPER, session, 1, 1) == "T2EP26101200101"   # the spec example
    for role in (Role.ENTRY, Role.EXIT, Role.COVER, Role.SELFTEST):
        for account in Account:
            for seq_no, attempt in ((1, 1), (35, 36), (36, 2), (1296, 1295), (m.SEQ_MAX, m.ATTEMPT_MAX)):
                tag = m.make_tag(role, account, session, seq_no, attempt)
                assert len(tag) == 15 and tag.isalnum() and tag == tag.upper() and tag.isascii()
                p = m.parse_tag(tag)
                assert p == m.ParsedTag(role, account, session, seq_no, attempt)
                assert p.tag == tag
                assert m.is_bot_tag(tag, account)
                assert not m.is_bot_tag(tag, next(a for a in Account if a is not account))
    assert m.make_tag("EXIT", "LIVE", date(2099, 12, 31), 46655, 1295) == "T2XL991231ZZZZZ"

    good = "T2EP26101200101"
    foreign = [
        None, 123, b"T2EP26101200101", "", "SQOFF", "EX123456789", "2510120001234",
        good.lower(), good + "\n", " " + good, good[:-1], good + "1",
        "T3EP26101200101", "T2ZP26101200101", "T2EQ26101200101",       # prefix, role letter, account letter
        "T2EP26131200101", "T2EP26103200101", "T2EP26022900101",       # month 13, day 32, 2026-02-29
        "T2EP26101200001", "T2EP26101200000", "T2EP261012000z1",       # seq 0, attempt 0, lowercase digit
        "T2EP2610120_101", "T2EP26101200 01", "T2EP２６101200101",      # underscore, space, full-width digits
    ]
    for tag in foreign:
        assert m.parse_tag(tag) is None, tag
        assert not m.is_bot_tag(tag, Account.PAPER)

    for bad in [(Role.EXTERNAL, Account.LIVE, session, 1, 1), (Role.ENTRY, Account.LIVE, session, 0, 1),
                (Role.ENTRY, Account.LIVE, session, m.SEQ_MAX + 1, 1), (Role.ENTRY, Account.LIVE, session, 1, 0),
                (Role.ENTRY, Account.LIVE, session, 1, m.ATTEMPT_MAX + 1), (Role.ENTRY, Account.LIVE, session, True, 1),
                (Role.ENTRY, Account.LIVE, date(1999, 1, 1), 1, 1), (Role.ENTRY, Account.LIVE, "2026-10-12", 1, 1)]:
        with pytest.raises(ValueError):
            m.make_tag(*bad)
    assert m.external_order_pk("1234567890") == "EX1234567890"
    assert m.parse_tag(m.external_order_pk("1234567890123")) is None


# ---------------------------------------------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------------------------------------------
def test_quote_mid_none_one_side():
    only_bids = quote(bids=[Level(10.0, 75)])
    assert only_bids.bid == 10.0 and only_bids.ask is None
    assert only_bids.mid() is None and only_bids.spread_pct() is None
    only_asks = quote(asks=[Level(10.5, 75)])
    assert only_asks.bid is None and only_asks.ask == 10.5 and only_asks.mid() is None
    empty = quote()
    assert (empty.bid, empty.ask, empty.mid(), empty.spread_pct()) == (None, None, None, None)
    crossed = quote(bids=[Level(10.6, 75)], asks=[Level(10.5, 75)])
    assert crossed.mid() is None and crossed.spread_pct() is None
    locked = quote(bids=[Level(10.5, 75)], asks=[Level(10.5, 75)])
    assert locked.mid() == 10.5 and locked.spread_pct() == 0.0
    # zero-filled levels (thin Kite depth) do not count as a side
    zeros = quote(bids=[Level(0.0, 0), Level(0.0, 0)], asks=[Level(10.5, 75)])
    assert zeros.bid is None and zeros.mid() is None
    normal = quote(bids=[Level(9.9, 75)], asks=[Level(10.1, 150)], ltp=50.0)
    assert normal.mid() == pytest.approx(10.0)
    assert normal.spread_pct() == pytest.approx(2.0)                # (10.1 - 9.9) / 10.0 * 100; LTP ignored


def test_quote_book_normalized_and_age_fails_closed():
    q = quote(bids=[(9.8, 10, 1), Level(9.9, 20), (0.0, 0), (9.7, -5)], asks=[[10.2, 5], (10.1, 7), (10.3, 0)])
    assert q.bids == (Level(9.9, 20), Level(9.8, 10, 1))
    assert q.asks == (Level(10.1, 7), Level(10.2, 5))
    assert isinstance(q.bids, tuple) and q.bid == 9.9 and q.ask == 10.1
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.bids = ()
    with pytest.raises(TypeError):
        quote(bids=[{"price": 9.9, "quantity": 20}])
    assert quote(bids=None).bids == ()
    q = quote(bids=[Level(9.9, 20)], recv_mono=100.0)
    assert q.age(103.5) == pytest.approx(3.5)
    assert q.age(100.0) == 0.0
    assert q.age(99.0) == 0.0                 # received while the event was handled: the newest data, age 0
    assert q.age(100.0 - RECEIPT_SKEW_S) == 0.0
    assert q.age(100.0 - RECEIPT_SKEW_S - 0.01) == math.inf   # further "in the future" is a clock bug: stale
    assert quote(recv_mono=math.nan).age(100.0) == math.inf
    assert q == quote(bids=[(9.9, 20)], recv_mono=100.0)


# ---------------------------------------------------------------------------------------------------------------
# enums, constants, small helpers
# ---------------------------------------------------------------------------------------------------------------
def test_enums_format_as_plain_values():
    assert f"ACCOUNT:{Account.LIVE}" == "ACCOUNT:LIVE"
    assert str(Role.EXIT) == "EXIT" and "%s" % Side.BUY == "BUY" and "{:>6}".format(Side.BUY) == "   BUY"
    assert Account("PAPER") is Account.PAPER and Account.PAPER == "PAPER" and {"PAPER": 1}[Account.PAPER] == 1
    assert json.dumps({"a": m.OrderState.COMPLETE}) == '{"a": "COMPLETE"}'
    assert m.OrderState.COMPLETE.is_terminal and not m.OrderState.WORKING.is_terminal
    assert {s.value for s in m.TERMINAL_ORDER} == {"COMPLETE", "CANCELLED", "REJECTED", "NOT_PLACED"}
    assert {s.value for s in m.TERMINAL_STRUCT} == {"CLOSED", "CLOSED_EXTERNAL", "VOID"}
    assert m.LegState.VOID.is_terminal and not m.StructState.RESERVED.is_terminal
    assert dict(m.ROLE_SIDE) == {Role.ENTRY: Side.BUY, Role.EXIT: Side.SELL, Role.COVER: Side.BUY,
                                 Role.SELFTEST: Side.BUY}
    assert len(m.EXIT_REASONS) == 15 and len(set(m.EXIT_REASONS)) == 15
    assert m.Charges(20.0, 1.5, 0.36, 0.01, 0.0, 3.67, 0.0, 0.0).total == 25.54
    assert m.Recheck(-math.inf, 101.0).contains(100.0) and not m.Recheck(99.0, 101.0).contains(101.5)
    assert not m.Recheck(-math.inf, math.inf).contains(None) and not m.Recheck(0, 1).contains(math.nan)
    iv = m.InvariantViolation("I3", "oversold", scope="LEG:42")
    assert iv.leg_id == 42 and iv.code == "I3" and "I3" in str(iv)
    assert m.InvariantViolation("I1").scope == "GLOBAL" and m.InvariantViolation("I1").leg_id is None
    assert m.ExitProfile().hard_sl_pct == 15.0 and m.ExitProfile().no_bid_exit_s == 10.0


# ---------------------------------------------------------------------------------------------------------------
# Leg quantities derived from orders
# ---------------------------------------------------------------------------------------------------------------
INST = m.Instrument("NFO:RELIANCE26OCT2900CE", 111, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", "NSE:RELIANCE",
                    "CE", 2900.0, date(2026, 10, 27), 500, 0.05)


def order(pk, role, side, filled, leg_id=7, qty=500):
    return m.Order(pk, leg_id, 3, Account.PAPER, "PAPER:od", role, side, INST.symbol, qty, 10.0, 1, 0,
                   filled_qty=filled)


def test_leg_quantities_derived_from_bound_orders():
    leg = m.Leg(7, 3, 1, Account.PAPER, "PAPER:od", date(2026, 10, 12), 1, INST)
    assert leg.side == "LONG" and leg.state is m.LegState.PLANNED and leg.orders == []
    with pytest.raises(RuntimeError):
        leg.open_qty                                     # unbound never guesses 0
    index: dict[str, m.Order] = {}
    assert leg.bind_orders(index) is leg
    assert (leg.entry_filled, leg.exit_filled, leg.open_qty) == (0, 0, 0)

    def add(o):
        index[o.order_pk] = o
        leg.orders.append(o.order_pk)

    add(order("T2EP26101200101", Role.ENTRY, Side.BUY, 500))
    assert (leg.entry_filled, leg.exit_filled, leg.open_qty) == (500, 0, 500)
    add(order("T2XP26101200101", Role.EXIT, Side.SELL, 200))
    add(order("EX99", Role.EXTERNAL, Side.SELL, 100))
    assert (leg.entry_filled, leg.exit_filled, leg.open_qty) == (500, 300, 200)
    index["T2XP26101200101"].filled_qty = 400        # the shared index is live: later fills show up at once
    assert leg.open_qty == 0
    add(order("T2CP26101200101", Role.COVER, Side.BUY, 100))
    assert leg.open_qty == 0                         # a cover never creates sellable quantity
    add(order("EX100", Role.EXTERNAL, Side.BUY, 50))
    assert leg.open_qty == 0
    add(order("T2XP26101200102", Role.EXIT, Side.SELL, 100))
    assert leg.open_qty == -100                      # oversold is reported, not clamped (I3 catches it)
    # plain-string roles (as loaded from rows) count the same
    assert m.leg_fill_totals([order("a", "ENTRY", "BUY", 75), order("b", "EXIT", "SELL", 25),
                              order("c", "SELFTEST", "BUY", 75)]) == (150, 25)
    # an order missing from the index, or owned by another leg, is a leg-scope invariant violation
    leg.orders.append("T2EP26101200199")
    with pytest.raises(m.InvariantViolation) as exc:
        leg.open_qty
    assert exc.value.leg_id == 7
    leg.orders.pop()
    add(order("T2EP26101200201", Role.ENTRY, Side.BUY, 500, leg_id=8))
    with pytest.raises(m.InvariantViolation):
        leg.entry_filled
    # the binding is not a field: never compared, stored or copied
    assert "_order_index" not in {f.name for f in dataclasses.fields(m.Leg)}
    copy = dataclasses.replace(leg)
    assert copy == leg
    with pytest.raises(RuntimeError):
        copy.open_qty


def test_order_request_problem():
    good = m.OrderRequest(Account.PAPER, "T2EP26101200101", Role.ENTRY, INST.symbol, INST.tradingsymbol, "NFO",
                          INST.token, Side.BUY, 1000, 12.35, 500, 0.05)
    assert m.order_request_problem(good) is None
    assert m.order_request_problem(dataclasses.replace(good, tag="T2XP26101200101", role=Role.EXIT,
                                                       side=Side.SELL)) is None
    bad = {
        "side": dict(side=Side.SELL),
        "external": dict(role=Role.EXTERNAL),
        "tag role": dict(tag="T2XP26101200101"),
        "tag account": dict(tag="T2EL26101200101"),
        "tag foreign": dict(tag="MANUAL"),
        "exchange": dict(exchange="NSE"),
        "lot multiple": dict(qty=750),
        "zero qty": dict(qty=0),
        "float qty": dict(qty=1000.0),
        "lot": dict(lot_size=0),
        "tick multiple": dict(limit_px=12.37),
        "limit": dict(limit_px=0.0),
        "nan limit": dict(limit_px=math.nan),
        "tick": dict(tick=0.0),
        "enum": dict(account="DEMO"),
    }
    for name, change in bad.items():
        assert m.order_request_problem(dataclasses.replace(good, **change)) is not None, name


# ---------------------------------------------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------------------------------------------
def test_events_are_frozen_messages():
    t = ev.Timer(WALL, 1000.25)
    assert isinstance(t, ev.Event) and not isinstance(t, ev.Command)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.now_mono = 0.0
    assert ev.PlaceResult(Account.LIVE, "T2EL26101200101", "OK", "250101000001").throttled is False
    snap = ev.OrderSnapshot(Account.LIVE, "250101000001", "T2EL26101200101", "COMPLETE", 75, 10.05, None, None,
                            "WS", 1000.5)
    assert snap.status in ev.ORDER_STATUSES and snap.source in ev.SNAPSHOT_SOURCES
    req = m.OrderRequest(Account.LIVE, "T2EL26101200101", Role.ENTRY, INST.symbol, INST.tradingsymbol, "NFO",
                         INST.token, Side.BUY, 500, 10.05, 500, 0.05)
    place = ev.Place(Account.LIVE, req.tag, req)
    assert isinstance(place, ev.Command) and isinstance(place, ev.BROKER_COMMANDS)
    assert not isinstance(ev.FetchQuotes("i1", ("NFO:X",), ev.Priority.EXIT), ev.BROKER_COMMANDS)
    assert ev.Priority.EXIT < ev.Priority.PAPER < ev.Priority.BACKGROUND and ev.Priority.EXIT == 0
    assert ev.FetchHistorical("h1", "NSE:RELIANCE", 738561, WALL, WALL).interval == "5minute"
    cn = ev.ContractNoteOrder("250101000001", "NFO", INST.tradingsymbol, Side.SELL, 500, 11.0)
    assert (cn.product, cn.order_type, cn.variety) == ("MIS", "LIMIT", "regular")
    assert ev.Exit(3).code == 3 and ev.Shutdown("SIGTERM").reason == "SIGTERM"
    assert isinstance(ev.RecoveryStart(), ev.Event) and isinstance(ev.QuotesDirty(), ev.Event)
    # the prestart self-check runs off the core thread and reports back as an event (§9.5 step 13)
    run = ev.RunSelfcheck()
    assert isinstance(run, ev.Command) and run.phase == "prestart" and not isinstance(run, ev.BROKER_COMMANDS)
    done = ev.SelfcheckResult("prestart", False, error="RuntimeError: boom")
    assert isinstance(done, ev.Event) and done.report is None and not done.ok
    assert ev.Subscribe((111,), "full").mode in ev.FEED_MODES
    assert ev.QuoteResult("i1", False, ("NFO:X",), error="TIMEOUT").missing == ()
    # every event/command is a slotted, frozen dataclass
    for obj in vars(ev).values():
        if isinstance(obj, type) and issubclass(obj, (ev.Event, ev.Command)) and obj not in (ev.Event, ev.Command):
            assert dataclasses.is_dataclass(obj) and obj.__dataclass_params__.frozen, obj
            assert "__slots__" in vars(obj), obj
    assert not hasattr(ev.QuotesDirty(), "__dict__") and not hasattr(t, "__dict__")
