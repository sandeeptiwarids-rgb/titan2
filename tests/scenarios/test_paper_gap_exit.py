"""Scenario (review round 2, paper model 2): a stop into a one-second gap, and the lone bid there.

One second of a gapped bid book (a lone thin bid 19% under the entry, past the stop's deep-breach bypass) stops the
trade out at once (HARD_SL, no debounce), and the first exit IOC is priced 2 ticks under that bid. The exchange
matches it, so LIVE loses the gap. Paper model 1 rounded the IOC's share of that bid (half of it, at 0.5
participation) down to whole lots, so a bid under 2 lots never filled it: the exit retried once the touch was back
and PAPER booked a scratch (-Rs 146 here, where the first IOC on the exchange loses about Rs 3,100). The share's
fraction of a lot now fills with that probability, by the order's own seeded draw (review probe p04). For a lone
one-lot bid that is a probability of 0.5: paper still misses it half the time, where a live IOC usually takes it.
This scenario's draw is one of the misses, so paper still books the scratch: the bias paper model 2 leaves.

This scenario's first exit draws about 0.75: outside a one-lot bid's share (0.5), inside a 1.6-lot bid's (0.8). A
1.6-lot level (960 units on a 600 lot) is a book NSE cannot show, since every level is whole lots, so the second test
only shows the draw at work on a fractional share. Runs on PAPER and LIVE; FakeKite gives a LIVE order its PAPER
twin's draw, so both runs book the same trade: the LIVE run here mirrors paper, it is no model of the exchange.
"""

from __future__ import annotations

import math

import pytest

from titan2.model import Level
from titan2.settings import DEFAULTS

from fakes.scenario import enter, fill_draw_of, leg_of, orders_of, state_of, wait_for

pytestmark = pytest.mark.scenario

TICK = 0.05
P = DEFAULTS.PAPER_PARTICIPATION


def _stop_into_a_gap(dual, lots: float):
    """Enter SONACOMS, show a lone bid of `lots` lots 19% under the entry for one second, then the old book again.
    Returns (runner, the first EXIT order row, the trade row, the gap bid, the lot)."""
    r = dual.runner()
    i, s = enter(r, "NSE:SONACOMS")
    sid = s["structure_id"]
    leg = leg_of(r, sid)
    sym, lot = leg.inst.symbol, leg.inst.lot_size
    p = r.engine.state.structures[sid].exit_params
    bids, asks = r.market.true_levels(sym)
    r.run(2)
    gap = round(int(p.entry_avg * 0.81 / TICK) * TICK, 2)              # 19% under the entry
    assert gap < p.hard_sl_px - p.profile.stop_confirm_bypass_pct / 100 * p.entry_avg   # a deep breach
    r.market.depth(sym, [Level(gap, int(lots * lot))], asks)          # the gapped book, for one second
    r.run(1.0)
    r.market.depth(sym, bids, asks)                                    # the touch is back
    wait_for(r, lambda: state_of(r, i) == "CLOSED", 30, "the close")
    first = orders_of(r, sid, "EXIT")[0]
    (tr,) = r.trades(r.book())
    assert tr["exit_reason"] == "HARD_SL" and first["limit_px"] <= gap      # the first IOC is priced into the gap
    assert math.isclose(tr["net"], r.net_from_fills(sid), abs_tol=0.005)
    return r, first, tr, gap, lot


def test_a_lone_one_lot_gap_bid_is_still_missed_when_the_draw_is_over_its_share(dual):
    """The known bias left by paper model 2 (review of paper model 2, probe test_probe_lone_bid): the exchange's IOC
    would usually sell the lone one-lot bid and book the gap's loss. Paper fills it only when the order's draw is
    under participation x 1 lot (0.5): this order's draw is not, so paper misses the bid and books a scratch near the
    touch, as paper model 1 always did. About half of such stops end this way; the other half sell the bid."""
    _r, first, tr, gap, _lot = _stop_into_a_gap(dual, 1.0)
    assert fill_draw_of(first) >= 1.0 * P                 # outside the one-lot bid's share of 0.5
    assert first["filled_qty"] == 0 and tr["exit_avg"] > gap + 0.5 * (tr["entry_avg"] - gap)   # sold near the touch


def test_a_thin_bid_in_the_gap_fills_the_stop_inside_its_share(dual):
    """The draw at work: inside a 1.6-lot bid's share (0.8) the first IOC sells the bid and the gap's loss is booked.
    NSE shows whole lots only, so on a real book a one-lot order's share at 0.5 is a whole lot (filled for sure) or
    half a lot (the case above)."""
    _r, first, tr, gap, lot = _stop_into_a_gap(dual, 1.6)
    assert fill_draw_of(first) < 1.6 * P                  # inside the 1.6-lot bid's share of 0.8 ...
    # ... so the first IOC sells the lone bid, as the exchange's does: the gap's loss is booked (paper model 1:
    #   unfilled, then a scratch at the touch)
    assert (first["filled_qty"], first["avg_fill_px"], tr["exit_avg"], tr["qty"]) == (lot, gap, gap, lot)
    assert tr["net"] < -(tr["entry_avg"] - gap) * lot
