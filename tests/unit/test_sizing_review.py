"""Regression tests for two post-build review findings on titan2.core.sizing.size().

M2: the DEPTH term counted bid quantity at any price, so stub bids far under the touch passed "3x the order on the
    exit side". Only levels within LIQ_MAX_SLIP_VS_MID_PCT of the mid count now (sizing.near_qty).
M3: DAY_BUDGET saw only the book's realized + MTM. It now also subtracts the stop-outs of the positions already open,
    and for a LIVE book takes the LIVE account's daily-stop room too. Since the review of round 3 the day term is the
    realized net and each open position counts by its stop-out measured from its entry (sizing.remaining_stop_risk):
    an open gain was counted twice, refused by min(day net, 0) and charged again as risk still to lose.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from titan2.charges import rates_for, round_trip_charges
from titan2.core import exits, sizing
from titan2.model import ExitProfile, Instrument, Level, Policy, Quote, Refusal, Sized
from titan2.settings import DEFAULTS

IST = timezone(timedelta(hours=5, minutes=30))
WALL = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
R26 = rates_for(date(2026, 10, 12))
LOT = 500
INST = Instrument("NFO:XYZ26OCT2900CE", 1001, "XYZ26OCT2900CE", "NFO", "XYZ", "NSE:XYZ", "CE", 2900.0,
                  date(2026, 10, 27), LOT, 0.05)
PROFILE = ExitProfile()


def _quote(bids, asks) -> Quote:
    return Quote(INST.symbol, 1001, tuple(bids), tuple(asks), None, 1000, WALL - timedelta(seconds=5), None,
                 1000.0, WALL, "WS")


def _risk(px: float, lots: int = 1) -> float:
    return lots * (LOT * px * (PROFILE.hard_sl_pct + DEFAULTS.STOP_SLIP_ALLOW_PCT) / 100
                   + round_trip_charges(px, LOT, R26))


# ---------------------------------------------------------------------------------------------------------------
# M2: exit-side depth counts only bids near the mid
# ---------------------------------------------------------------------------------------------------------------
def test_depth_ignores_stub_bids_far_under_the_touch():
    # one real lot at the touch, then stubs at Rs 25.00 (-22%) and Rs 0.05; deep tight asks
    stub = _quote(bids=[(31.95, 500), (25.00, 500), (0.05, 500)], asks=[(32.00, 5000)])
    limit = exits.entry_limit(stub, INST.tick)
    assert limit == 32.10
    rb3, _ = exits.realizable_bid(stub, 3 * LOT)
    assert round(rb3, 2) == 19.0                     # what "3 x the order on the bid side" is worth here
    assert sizing.size(INST, stub, limit, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL) == Refusal("SIZE", "DEPTH")

    # the same touch with real bids one and two ticks under it passes (exactly 3 lots near the mid: DEPTH binds)
    real = _quote(bids=[(31.95, 500), (31.90, 500), (31.85, 500)], asks=[(32.00, 5000)])
    assert sizing.size(INST, real, limit, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL) == Sized(
        500, 1, "DEPTH", pytest.approx(_risk(32.10)))


def test_depth_ask_side_counts_only_near_the_mid_too():
    far_asks = _quote(bids=[(31.95, 5000)], asks=[(32.00, 500), (40.00, 5000)])
    limit = exits.entry_limit(far_asks, INST.tick)
    assert sizing.size(INST, far_asks, limit, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL) == Refusal("SIZE", "DEPTH")


def test_near_qty_band():
    mid = 31.975                                     # bid 31.95 / ask 32.00; 1% band = 31.655 .. 32.29475
    bids = (Level(31.95, 100), Level(31.66, 200), Level(31.65, 400), Level(31.60, 800))
    asks = (Level(32.00, 100), Level(32.29, 200), Level(32.30, 400))
    assert sizing.near_qty(bids, mid, 1.0) == 300            # 31.65 is outside the band; the 4th level never counts
    assert sizing.near_qty(asks, mid, 1.0) == 300
    assert sizing.near_qty(bids, mid, 1.0, n=1) == 100
    assert sizing.near_qty(bids, None, 1.0) == 0 and sizing.near_qty((), mid, 1.0) == 0
    assert sizing.near_qty(bids, float("nan"), 1.0) == 0 and sizing.near_qty(bids, mid, float("nan")) == 0
    # a crossed or one-sided book has no mid: no depth at all
    crossed = _quote(bids=[(32.05, 5000)], asks=[(32.00, 5000)])
    assert sizing.size(INST, crossed, 32.10, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL) == Refusal("SIZE", "DEPTH")
    for bad in (0.0, -1.0, float("nan"), None):
        s = replace(DEFAULTS, LIQ_MAX_SLIP_VS_MID_PCT=bad)
        q = _quote(bids=[(31.95, 5000)], asks=[(32.00, 5000)])
        assert sizing.size(INST, q, 32.10, PROFILE, s, 0.0, R26, Policy.FULL) == Refusal("SIZE", "INVALID_SETTING"), bad


# ---------------------------------------------------------------------------------------------------------------
# M3: DAY_BUDGET sees open positions and the LIVE account
# ---------------------------------------------------------------------------------------------------------------
DEEP = _quote(bids=[(31.95, 5000)], asks=[(32.00, 5000)])
DEEP_48 = _quote(bids=[(48.75, 5000)], asks=[(48.80, 5000)])


def test_day_budget_takes_the_live_account_room():
    limit = exits.entry_limit(DEEP, INST.tick)
    # LIVE book flat today; the LIVE account already lost Rs 9,900 (another LIVE strategy): Rs 100 of room left
    r = sizing.size(INST, DEEP, limit, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL, live_book=True,
             account_realized_net=-9900.0, account_open_risk_rs=0.0)
    assert r == Refusal("SIZE", "DAY_BUDGET")
    # the account's open LIVE positions count against its room as well
    r = sizing.size(INST, DEEP, limit, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL, live_book=True,
             account_realized_net=-4000.0, account_open_risk_rs=5000.0)
    assert r == Refusal("SIZE", "DAY_BUDGET")
    # with room on the account the usual terms decide
    ok = sizing.size(INST, DEEP, limit, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL, live_book=True,
                     account_realized_net=0.0, account_open_risk_rs=0.0)
    assert ok == Sized(500, 1, "LOTS", pytest.approx(_risk(limit)))
    # unknown account P&L or open risk refuses a LIVE book (fails closed); a profit never adds room
    for kw in ({"account_realized_net": None}, {"account_realized_net": float("nan")},
               {"account_realized_net": 0.0, "account_open_risk_rs": None},
               {"account_realized_net": 0.0, "account_open_risk_rs": -1.0}):
        assert sizing.size(INST, DEEP, limit, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL, live_book=True,
                    **kw) == Refusal("SIZE", "LIVE_DAY_PNL_UNKNOWN"), kw
    big = replace(DEFAULTS, MAX_LOTS=10, MAX_LOSS_PER_TRADE_RS=1e9, MAX_PREMIUM_PER_TRADE_RS=1e9)
    very_deep = _quote(bids=[(31.95, 100_000)], asks=[(32.00, 100_000)])
    r = sizing.size(INST, very_deep, limit, PROFILE, big, 0.0, R26, Policy.FULL, live_book=True,
             account_realized_net=50_000.0, account_open_risk_rs=0.0)
    assert r.binding == "DAY_BUDGET" and r.lots == int(DEFAULTS.LIVE_ACCOUNT_DAILY_STOP_RS // _risk(limit)) == 3


def test_day_budget_subtracts_the_open_positions_remaining_stop_risk():
    # position A: one lot bought at 48.80 (stop risk ~4,980 with charges), Rs 100 under water; book realized -9,000.
    #   A counts by its stop-out from its entry (the Rs 100 it is down is part of it)
    a_stop_out = sizing.remaining_stop_risk(_risk(48.80), -100.0)
    assert a_stop_out == pytest.approx(_risk(48.80))
    realized = -9000.0
    # without A's stop-out B fits (the old behaviour) ...
    assert isinstance(sizing.size(INST, DEEP_48, 48.80, PROFILE, DEFAULTS, realized, R26, Policy.FULL), Sized)
    # ... but A and B stopping out together would land near -18,970 against the 15,000 stop: refused
    r = sizing.size(INST, DEEP_48, 48.80, PROFILE, DEFAULTS, realized, R26, Policy.FULL, open_risk_rs=a_stop_out)
    assert r == Refusal("SIZE", "DAY_BUDGET")
    # the budget is floor((DAILY_STOP_RS + min(realized, 0) - open risk) / per_lot_risk)
    big = replace(DEFAULTS, MAX_LOTS=10, MAX_LOSS_PER_TRADE_RS=1e9, MAX_PREMIUM_PER_TRADE_RS=1e9)
    nifty = replace(INST, lot_size=65)
    q = _quote(bids=[(99.95, 100_000)], asks=[(100.0, 100_000)])
    per_lot = 65 * 100.0 * 0.20 + round_trip_charges(100.0, 65, R26)
    for open_risk in (0.0, 2500.0, 6000.0):
        r = sizing.size(nifty, q, 100.0, PROFILE, big, -6000.0, R26, Policy.FULL, open_risk_rs=open_risk)
        assert r.binding == "DAY_BUDGET" and r.lots == int((15_000.0 - 6000.0 - open_risk) // per_lot), open_risk
    # unknown open risk refuses a FULL book; QUALITY books have no day budget
    for bad in (None, float("nan"), float("inf"), -1.0, True):
        assert sizing.size(INST, DEEP, 32.10, PROFILE, DEFAULTS, 0.0, R26, Policy.FULL,
                    open_risk_rs=bad) == Refusal("SIZE", "OPEN_RISK_UNKNOWN"), bad
    assert isinstance(sizing.size(INST, DEEP, 32.10, PROFILE, DEFAULTS, None, R26, Policy.QUALITY, open_risk_rs=None,
                           live_book=True), Sized)


def test_remaining_stop_risk_feeds_open_risk():
    # A position counts by its stop-out measured from its entry. A leg sized at Rs 4,980 of stop risk counts Rs 4,980
    #   Rs 100 under water (that loss is part of its stop-out) and Rs 2,000 in profit alike: a gain neither adds room
    #   nor is charged as still to lose. This pinned 4,880 and 6,980 before the review of round 3: the day net's
    #   min(.., 0) gave the gain no room, and counting it as still to lose charged it a second time. A deep loser
    #   past its stop counts its loss now (the day term is the realized net, so that loss is in no other term).
    assert sizing.remaining_stop_risk(4980.0, -100.0) == 4980.0
    assert sizing.remaining_stop_risk(4980.0, 2000.0) == 4980.0
    assert sizing.remaining_stop_risk(4980.0, -6000.0) == 6000.0
    assert sizing.remaining_stop_risk(4980.0, 0.0) == 4980.0          # an entry still working counts in full
    # a raised or locked stop takes what it moved off the stop-out, never below 0; the loss now still counts
    assert sizing.remaining_stop_risk(4980.0, 2000.0, 3000.0) == pytest.approx(1980.0)
    assert sizing.remaining_stop_risk(4980.0, 2600.0, 6000.0) == 0.0
    assert sizing.remaining_stop_risk(4980.0, -700.0, 6000.0) == 700.0
    for bad in ((None, 0.0), (4980.0, None), (float("nan"), 0.0), (-1.0, 0.0), (True, 0.0), (4980.0, 0.0, None),
                (4980.0, 0.0, -1.0), (4980.0, 0.0, float("inf"))):
        assert sizing.remaining_stop_risk(*bad) is None, bad
    # summed over the open legs it is what DAY_BUDGET subtracts from the realized day
    open_risk = sum(sizing.remaining_stop_risk(r, m) for r, m in ((4980.0, -100.0), (1000.0, 0.0)))
    r = sizing.size(INST, DEEP_48, 48.80, PROFILE, DEFAULTS, -9000.0, R26, Policy.FULL, open_risk_rs=open_risk)
    assert r == Refusal("SIZE", "DAY_BUDGET")
