"""U03: titan2.charges, the dated RateSets and the single P&L function (spec §8.1, worked example DETAIL §7.6)."""

from __future__ import annotations

import ast
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from titan2 import charges as ch
from titan2.charges import (
    AUTO_SQUAREOFF, CHARGE_FIELDS, MANUAL, RATESETS, ZERO_CHARGES, OrderFills, RateSet, breakeven_px, exercise_stt,
    k_exit, mtm_net, net_pnl, order_charges, rates_for, round_money, round_trip_charges,
)
from titan2.model import Charges, Fill, Side

IST = timezone(timedelta(hours=5, minutes=30))
WALL = datetime(2026, 10, 12, 9, 25, tzinfo=IST)
R26 = rates_for(date(2026, 10, 12))          # in force today (2026-04-01 set)
R25 = rates_for(date(2025, 6, 2))            # the 2024-10-01 set
FIXTURE = Path(__file__).parent / "fixtures" / "contract_note.json"


def buy(qty, px, kind=None):
    return OrderFills(Side.BUY, qty, qty * px, kind)


def sell(qty, px, kind=None):
    return OrderFills(Side.SELL, qty, qty * px, kind)


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_brokerage_once_per_executed_order():
    # one order filled in three pieces pays one brokerage
    fills = [Fill("T2EP26101200101", f"trade:{i}", "TRADE", q, 32.0, WALL, None) for i, q in enumerate((100, 150, 250))]
    one = OrderFills.from_fills(Side.BUY, fills)
    assert (one.filled_qty, one.value) == (500, 16000.0)
    assert order_charges(one, R26).brokerage == 20.0
    # two executed orders pay two; a missed IOC (nothing filled) pays none and is not counted
    pnl = net_pnl([one, OrderFills(Side.SELL, 0, 0.0), sell(500, 33.0)], R26)
    assert pnl.charges.brokerage == 40.0
    assert pnl.n_executed_orders == 2
    # the same quantity split over two orders costs exactly one more brokerage with its GST (Rs 23.60)
    split = net_pnl([buy(250, 32.0), buy(250, 32.0)], R26).charges.total
    assert split - order_charges(buy(500, 32.0), R26).total == pytest.approx(23.60, abs=0.02)


def test_unfilled_zero():
    for o in (OrderFills(Side.BUY, 0, 0.0), OrderFills(Side.SELL, 0, 0.0),
              OrderFills(Side.SELL, 0, 0.0, AUTO_SQUAREOFF), OrderFills(Side.SELL, 0, 0.0, MANUAL)):
        c = order_charges(o, R26)
        assert c == ZERO_CHARGES and c.total == 0.0
    pnl = net_pnl([OrderFills(Side.BUY, 0, 0.0), OrderFills(Side.BUY, 0, 0.0)], R26)
    assert (pnl.gross, pnl.net, pnl.n_executed_orders, pnl.charges) == (0.0, 0.0, 0, ZERO_CHARGES)
    assert net_pnl([], R26) == pnl
    # an IOC that missed before the filled retry adds nothing to the trade
    assert net_pnl([OrderFills(Side.BUY, 0, 0.0), buy(500, 32.0), sell(500, 33.13)], R26) == \
        net_pnl([buy(500, 32.0), sell(500, 33.13)], R26)


def test_stt_sell_015_stamp_buy():
    assert (R26.version, R26.stt_sell, R26.stt_exercise, R26.stamp_buy) == ("2026-04-01", 0.0015, 0.0015, 0.00003)
    s = order_charges(sell(400, 25.0), R26)       # Rs 10,000 sold
    b = order_charges(buy(400, 25.0), R26)        # Rs 10,000 bought
    assert (s.stt, s.stamp) == (15.0, 0.0)        # STT 0.15% on the sell side only
    assert (b.stt, b.stamp) == (0.0, 0.30)        # stamp 0.003% on the buy side only
    # the other components do not depend on the side
    assert (s.brokerage, s.txn, s.sebi, s.ipft, s.gst) == (b.brokerage, b.txn, b.sebi, b.ipft, b.gst)
    # before the Budget 2026-27 hike the sell STT was 0.10%
    assert order_charges(sell(400, 25.0), rates_for(date(2026, 3, 31))).stt == 10.0


def test_gst_and_sqoff_59():
    o = sell(500, 33.13)
    normal = order_charges(o, R26)
    raw_gst = R26.gst * (R26.brokerage_per_order + R26.txn * o.value + R26.sebi * o.value + R26.ipft * o.value)
    assert normal.gst == round_money(raw_gst) == 4.66
    assert normal.sqoff_fee == 0.0
    sq = order_charges(sell(500, 33.13, AUTO_SQUAREOFF), R26)
    assert sq.sqoff_fee == 50.0
    assert round(sq.gst - normal.gst, 2) == 9.0           # GST applies to the square-off fee too
    assert round(sq.total - normal.total, 2) == 59.0      # Rs 50 + 18% GST per auto square-off order
    assert order_charges(sell(500, 33.13, MANUAL), R26) == normal
    # a leg squared off by the broker nets exactly Rs 59 less than the same exit made by the bot
    bot = net_pnl([buy(500, 32.0), o], R26).net
    broker = net_pnl([buy(500, 32.0), sell(500, 33.13, AUTO_SQUAREOFF)], R26).net
    assert round(bot - broker, 2) == 59.0


def _net_if_sold(entries, qty, px, r):
    return net_pnl([*entries, OrderFills(Side.SELL, qty, qty * px)], r).net


@pytest.mark.parametrize("entries", [
    [buy(500, 32.0)],                                   # DETAIL §7.6 worked example
    [buy(65, 101.35)],                                  # one NIFTY lot (65)
    [buy(300, 12.05), buy(200, 12.10)],                 # two entry orders
    [buy(30, 412.35)],                                  # one BANKNIFTY lot (30), expensive premium
])
@pytest.mark.parametrize("r", [R26, R25], ids=["2026-04-01", "2024-10-01"])
def test_breakeven_equals_root(entries, r):
    qty = sum(o.filled_qty for o in entries)
    value = math.fsum(o.value for o in entries)
    c_in = net_pnl(entries, r).charges.total
    be = breakeven_px(value, qty, c_in, r)
    # net_pnl is zero at be (up to the 2-dp rounding of each charge) and changes sign around it
    assert abs(_net_if_sold(entries, qty, be, r)) <= 0.05
    assert _net_if_sold(entries, qty, be - 0.01, r) < 0 < _net_if_sold(entries, qty, be + 0.01, r)
    # an independent bisection on net_pnl finds the same root
    lo, hi = value / qty * 0.5, value / qty * 2.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _net_if_sold(entries, qty, mid, r) < 0 else (lo, mid)
    assert be == pytest.approx(lo, abs=1e-3)


def test_contract_note_fixture():
    note = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if note["placeholder"]:
        pytest.skip("awaiting Q5: a real Zerodha F&O contract note dated on/after 2026-04-01 to pin the RateSet")
    r = rates_for(date.fromisoformat(note["trade_date"]))
    assert r.version == note["expect_rates_version"]
    orders = [OrderFills(Side(o["side"]), o["filled_qty"], o["value"], o["external_kind"]) for o in note["orders"]]
    model = net_pnl(orders, r).charges
    tol = note["tolerance_rs_per_order"] * len(orders)
    for name in CHARGE_FIELDS:
        assert getattr(model, name) == pytest.approx(note["charges"][name], abs=tol), name
    assert model.total == pytest.approx(math.fsum(note["charges"].values()), abs=tol)


# ---------------------------------------------------------------------------------------------------------------
# Worked example and the cross-check against the verified reference cost model
# ---------------------------------------------------------------------------------------------------------------
def test_worked_example_detail_7_6():
    entry = buy(500, 32.0)
    c_in = order_charges(entry, R26)
    assert c_in == Charges(brokerage=20.0, stt=0.0, txn=5.68, sebi=0.02, ipft=0.0, gst=4.63, stamp=0.48, sqoff_fee=0.0)
    assert c_in.total == 30.81
    assert k_exit(R26) == pytest.approx(0.00192043, abs=5e-9)
    be = breakeven_px(16000.0, 500, c_in.total, R26)
    assert round(be, 2) == 32.17
    # the lock-floor exit at 33.13 nets about Rs 479, about 3% of the premium
    pnl = net_pnl([entry, sell(500, 33.13)], R26)
    assert (pnl.gross, pnl.charges.total, pnl.net) == (565.0, 86.23, 478.77)
    assert mtm_net([entry], 500, 33.13, R26) == pnl.net


# Values printed by the reference model scratchpad/wf2/market_structure/cost_model.py (verified 2026-10-10):
# leg_charges(px, qty, side) with today's regime and with use_regime_of_date=True on 2025-06-02. Its "exch" is the
# NSE transaction charge including IPFT; it does not round.
#   (px, qty, side): (stt, exch, sebi, stamp, gst, total) for 2026, and (stt, total) for 2025
REFERENCE = {
    (32.0, 500, "BUY"): ((0.0, 5.6848, 0.016, 0.48, 4.626144, 30.806944), (0.0, 30.806944)),
    (32.17, 500, "SELL"): ((24.1275, 5.715001, 0.016085, 0.0, 4.631595, 54.490181), (16.085, 46.447681)),
    (100.0, 65, "BUY"): ((0.0, 2.30945, 0.0065, 0.195, 4.016871, 26.527821), (0.0, 26.527821)),
    (100.0, 65, "SELL"): ((9.75, 2.30945, 0.0065, 0.0, 4.016871, 36.082821), (6.5, 32.832821)),
    (27.0, 650, "SELL"): ((26.325, 6.235515, 0.01755, 0.0, 4.725552, 57.303617), (17.55, 48.528617)),
    (412.35, 30, "BUY"): ((0.0, 4.395239, 0.01237, 0.371115, 4.39337, 29.172094), (0.0, 29.172094)),
    (412.35, 30, "SELL"): ((18.55575, 4.395239, 0.01237, 0.0, 4.39337, 47.356729), (12.3705, 41.171479)),
    (5.05, 1800, "SELL"): ((13.635, 3.229677, 0.00909, 0.0, 4.182978, 41.056745), (9.09, 36.511745)),
}


@pytest.mark.parametrize("key", list(REFERENCE))
def test_matches_reference_cost_model(key):
    px, qty, side = key
    (stt, exch, sebi, stamp, gst, total), (stt25, total25) = REFERENCE[key]
    half = 0.005 + 1e-9                     # one 2-dp rounding (an exact half rounds up), plus float slack
    c = order_charges(OrderFills(Side(side), qty, px * qty), R26)
    assert c.brokerage == 20.0
    assert c.stt == pytest.approx(stt, abs=half)
    assert c.txn + c.ipft == pytest.approx(exch, abs=2 * half)
    assert c.sebi == pytest.approx(sebi, abs=half)
    assert c.stamp == pytest.approx(stamp, abs=half)
    assert c.gst == pytest.approx(gst, abs=half)
    assert c.total == pytest.approx(total, abs=6 * half)
    c25 = order_charges(OrderFills(Side(side), qty, px * qty), R25)
    assert c25.stt == pytest.approx(stt25, abs=half)
    assert c25.total == pytest.approx(total25, abs=6 * half)


def test_exercise_stt_matches_reference():
    # reference expiry_settlement(intrinsic, qty, "LONG") charges = 20 x 1.18 brokerage + exercise STT
    assert exercise_stt(40.0, 65, R26) == 3.90 == round(27.5 - 23.6, 2)
    assert exercise_stt(12.5, 500, R26) == 9.38            # 9.375 rounds half up
    assert exercise_stt(40.0, 65, R25) == 3.25             # 0.125% before 2026-04-01
    assert exercise_stt(0.0, 65, R26) == exercise_stt(-3.0, 65, R26) == exercise_stt(40.0, 0, R26) == 0.0


# ---------------------------------------------------------------------------------------------------------------
# Rate sets
# ---------------------------------------------------------------------------------------------------------------
def test_rates_for_boundaries():
    assert rates_for(date(2026, 3, 31)).version == "2024-10-01"
    assert rates_for(date(2026, 4, 1)).version == "2026-04-01"
    assert rates_for(datetime(2026, 4, 1, 9, 20, tzinfo=IST)).version == "2026-04-01"
    assert rates_for(date(2024, 10, 1)).version == "2024-10-01"
    with pytest.raises(ValueError):
        rates_for(date(2024, 9, 30))
    with pytest.raises(TypeError):
        rates_for("2026-04-01")


def test_ratesets_well_formed():
    starts = [r.effective_from for r in RATESETS]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert all(r.version == r.effective_from.isoformat() for r in RATESETS)
    # the 2026-04-01 set is exactly the spec table (§8.1)
    r = RATESETS[-1]
    assert (r.brokerage_per_order, r.stt_sell, r.stt_exercise, r.txn, r.sebi, r.ipft, r.stamp_buy, r.gst,
            r.sqoff_fee) == (20.0, 0.0015, 0.0015, 0.0003553, 1e-6, 0.0, 0.00003, 0.18, 50.0)
    # 2024-10-01: transaction charge plus IPFT equals the reference model's 0.03553% exchange charge
    assert RATESETS[0].txn + RATESETS[0].ipft == pytest.approx(0.0003553, abs=1e-12)
    assert ch.RATES_VERIFIED_ON >= r.effective_from
    for bad in ({"stt_sell": 1.5}, {"txn": float("nan")}, {"gst": -0.18}, {"sqoff_fee": float("inf")},
                {"brokerage_per_order": True}, {"version": ""}):
        args = dict(version="x", effective_from=date(2027, 1, 1), brokerage_per_order=20.0, stt_sell=0.0015,
                    stt_exercise=0.0015, txn=0.0003553, sebi=1e-6, ipft=0.0, stamp_buy=0.00003, gst=0.18,
                    sqoff_fee=50.0)
        args.update(bad)
        with pytest.raises(ValueError):
            RateSet(**args)


# ---------------------------------------------------------------------------------------------------------------
# net_pnl, mtm_net, round trip
# ---------------------------------------------------------------------------------------------------------------
def test_net_pnl_sums_components():
    orders = [buy(300, 12.05), buy(200, 12.10), sell(500, 13.0)]
    pnl = net_pnl(orders, R26)
    assert pnl.gross == round(6500.0 - 3615.0 - 2420.0, 2) == 465.0
    each = [order_charges(o, R26) for o in orders]
    for name in CHARGE_FIELDS:
        assert getattr(pnl.charges, name) == round(sum(getattr(c, name) for c in each), 2)
    assert pnl.net == round(pnl.gross - pnl.charges.total, 2)
    assert (pnl.n_executed_orders, pnl.rates_version) == (3, "2026-04-01")
    # a losing trade is negative, rounded to 2 dp
    loss = net_pnl([buy(65, 101.35), sell(65, 86.15)], R26)
    assert loss.gross == -988.0 and loss.net < loss.gross and loss.net == round(loss.net, 2)


def test_mtm_net_is_net_if_sold_at_rb():
    entry = buy(500, 32.0)
    # open: identical to booking one more SELL at the realizable bid
    assert mtm_net([entry], 500, 31.4, R26) == net_pnl([entry, sell(500, 31.4)], R26).net
    # a walk average off the tick grid is fine
    assert mtm_net([entry], 500, 31.4333, R26) == net_pnl([entry, OrderFills(Side.SELL, 500, 500 * 31.4333)], R26).net
    # partly exited: only the open quantity is valued at rb
    done = [entry, sell(200, 34.0)]
    assert mtm_net(done, 300, 33.0, R26) == net_pnl([*done, sell(300, 33.0)], R26).net
    # no bids: MTM unknown, never a number
    assert mtm_net([entry], 500, None, R26) is None
    # flat: the realized net, no quote needed
    closed = [entry, sell(500, 33.13)]
    assert mtm_net(closed, 0, None, R26) == net_pnl(closed, R26).net == 478.77
    for bad_rb in (float("nan"), float("inf"), 0.0, -1.0, True):
        with pytest.raises(ValueError):
            mtm_net([entry], 500, bad_rb, R26)
    with pytest.raises(ValueError):
        mtm_net([entry], -1, 31.0, R26)


def test_round_trip_charges_one_lot():
    rt = round_trip_charges(100.0, 65, R26)
    assert rt == round(order_charges(buy(65, 100.0), R26).total + order_charges(sell(65, 100.0), R26).total, 2)
    assert rt == pytest.approx(26.527821 + 36.082821, abs=0.08)   # reference model
    for px, lot in ((0.0, 65), (-1.0, 65), (float("nan"), 65), (100.0, 0), (100.0, 65.0), (100.0, True)):
        with pytest.raises(ValueError):
            round_trip_charges(px, lot, R26)


# ---------------------------------------------------------------------------------------------------------------
# Inputs, rounding and the no-last-traded-price rule
# ---------------------------------------------------------------------------------------------------------------
def test_invalid_inputs_raise():
    for args in ((Side.BUY, -1, 0.0), (Side.BUY, True, 10.0), (Side.BUY, 10, float("nan")), (Side.BUY, 10, 0.0),
                 (Side.BUY, 0, 5.0), (Side.BUY, 10, -5.0), (Side.BUY, 10.0, 50.0), ("HOLD", 1, 1.0)):
        with pytest.raises(ValueError):
            OrderFills(*args)
    with pytest.raises(ValueError):
        OrderFills(Side.SELL, 1, 1.0, "AUTO_SQUARE_OFF")       # a typo must not silently drop the Rs 59
    assert OrderFills("SELL", 1, 1).side is Side.SELL          # plain strings from DB rows are accepted
    with pytest.raises(ValueError):
        OrderFills.from_fills(Side.BUY, [Fill("T2EP26101200101", "cum:0", "CUM", 0, 32.0, WALL, None)])
    with pytest.raises(ValueError):
        OrderFills.from_fills(Side.BUY, [Fill("T2EP26101200101", "cum:5", "CUM", 5, -1.0, WALL, None)])
    assert OrderFills.from_fills(Side.BUY, []) == OrderFills(Side.BUY, 0, 0.0)
    with pytest.raises(TypeError):
        order_charges((Side.BUY, 1, 1.0), R26)
    for args in ((16000.0, 0, 30.81), (16000.0, 500, -1.0), (float("inf"), 500, 30.81), (0.0, 500, 30.81),
                 (16000.0, 500.0, 30.81)):
        with pytest.raises(ValueError):
            breakeven_px(*args, R26)
    with pytest.raises(ValueError):
        exercise_stt(float("nan"), 65, R26)


def test_round_money_half_up():
    assert round_money(2.675) == 2.68          # binary 2.67499..., still the decimal half
    assert round_money(0.015) == 0.02
    assert round_money(0.195) == 0.20
    assert round_money(-2.675) == -2.68
    assert round_money(4.626144) == 4.63
    z = round_money(-0.001)
    assert z == 0.0 and math.copysign(1.0, z) == 1.0
    for bad in (float("nan"), float("inf"), None, "1.0"):
        with pytest.raises(ValueError):
            round_money(bad)


def test_order_part_shares_one_order_exactly():
    """One broker order closing the legs of several books (a 15:26 square-off of a contract two books hold) is booked
    as OrderParts: value and every charge component split by quantity with cumulative rounding, so the parts add up
    exactly to the order, brokerage and the square-off fee are paid once, and the order counts as executed once."""
    whole = OrderFills(Side.SELL, 750, 250 * 40.00 + 300 * 39.95 + 200 * 39.90, AUTO_SQUAREOFF)    # 29965.00
    for cuts in ((0, 250, 750), (0, 375, 750), (0, 250, 500, 750), (0, 1, 750)):
        parts = [ch.OrderPart(whole, a, b) for a, b in zip(cuts, cuts[1:])]
        each = [order_charges(p, R26) for p in parts]
        c = order_charges(whole, R26)
        for name in CHARGE_FIELDS:
            assert round_money(math.fsum(getattr(x, name) for x in each)) == getattr(c, name), (cuts, name)
        assert round_money(math.fsum(p.value for p in parts)) == round_money(whole.value)
        assert net_pnl(parts, R26) == net_pnl([whole], R26)
        assert round_money(math.fsum(net_pnl([p], R26).net for p in parts)) == net_pnl([whole], R26).net
        assert [net_pnl([p], R26).n_executed_orders for p in parts] == [1] + [0] * (len(parts) - 1)
    a, b = ch.OrderPart(whole, 0, 375), ch.OrderPart(whole, 375, 750)
    assert order_charges(a, R26).brokerage + order_charges(b, R26).brokerage == 20.0
    assert order_charges(a, R26).sqoff_fee == order_charges(b, R26).sqoff_fee == 25.0
    assert (a.side, a.filled_qty, a.external_kind) == (Side.SELL, 375, AUTO_SQUAREOFF)
    for bad in ((0, 0), (375, 375), (-1, 10), (0, 751), (10, 5), (0.0, 10)):
        with pytest.raises(ValueError):
            ch.OrderPart(whole, *bad)
    with pytest.raises(TypeError):
        ch.OrderPart((Side.SELL, 750, 29965.0), 0, 10)


def test_no_last_traded_price_input():
    tree = ast.parse(Path(ch.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
    assert not [n for n in names if "ltp" in n.lower() or "last_price" in n.lower()]


def test_contract_note_fixture_schema():
    note = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(note["placeholder"], bool)
    assert rates_for(date.fromisoformat(note["trade_date"])).version == note["expect_rates_version"] == "2026-04-01"
    assert set(note["charges"]) == set(CHARGE_FIELDS)
    assert note["tolerance_rs_per_order"] > 0 and note["orders"]
    for o in note["orders"]:
        OrderFills(Side(o["side"]), o["filled_qty"], o["value"], o["external_kind"])
