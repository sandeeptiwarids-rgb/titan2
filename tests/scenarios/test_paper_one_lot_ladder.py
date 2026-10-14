"""Scenario (review fix, strategy_g1 lens): a 1-lot exit into a ladder of one-lot bids fills on its first IOC.

fill_from_depth used to take floor(level qty x participation / lot) lots per level, so at participation 0.5 a level
holding one lot gave nothing. Every position is one lot (MAX_LOTS is 1), so an exit into the usual stock-option
ladder of one-lot bids failed band after band and only filled at a deeper level holding two lots or more: here three
IOCs and four ticks under the touch, where a live IOC sells into the bid at once. Participation now applies to the
depth walked so far: two one-lot bids earn the lot. Since paper model 2 (2026-10-14) the touch's half lot fills it
when the order's draw is under 0.5. Runs on PAPER and LIVE (FakeKite shares fill_from_depth and the draw).
"""

from __future__ import annotations

import pytest

from titan2.model import floor_tick

from fakes.scenario import enter, fill_draw_of, leg_of, orders_of, state_of, wait_for

pytestmark = pytest.mark.scenario

TICK = 0.05


def test_one_lot_bid_ladder_exits_on_the_first_ioc(dual):
    r = dual.runner()
    i, s = enter(r, "NSE:HINDPETRO")
    sid = s["structure_id"]
    leg = leg_of(r, sid)
    sym, lot = leg.inst.symbol, leg.inst.lot_size
    hard_sl = r.engine.state.structures[sid].exit_params.hard_sl_px
    top = round(floor_tick(hard_sl, TICK) - TICK, 2)              # the touch drops under the hard stop
    bids = [(round(top - k * TICK, 2), lot) for k in range(4)] + [(round(top - 4 * TICK, 2), 3 * lot)]
    r.market.depth(sym, bids, [(round(top + 0.5, 2), 4 * lot)])
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 30, "the exit")
    exits = orders_of(r, sid, "EXIT")
    assert [(o["band_idx"], o["filled_qty"]) for o in exits] == [(0, lot)], exits
    # the touch's half lot when the order's draw is under 0.5, else the second one-lot bid completes the lot
    assert exits[0]["avg_fill_px"] == bids[0 if fill_draw_of(exits[0]) < 0.5 else 1][0]
    (tr,) = r.trades(r.book())
    assert tr["thin_book"] == 0                                    # no under-filled exit
