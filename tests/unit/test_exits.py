"""U09: titan2.core.exits — frozen levels (§7.1), realizable bid (§7.3), decide() rules 0-9 (§7.4) and the exact
order prices (§5.7). Worked example: DETAIL §7.6 (2026-04-01 rates, E = 32.00, Q = 500)."""

from __future__ import annotations

import ast
import copy
import math
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from titan2.charges import OrderFills, net_pnl, rates_for
from titan2.clock import ist_datetime
from titan2.core import exits
from titan2.core.exits import (
    LOCK_ARMED, NO_BID, QUOTE_STALE, STOP_BREACH_PENDING, STOP_MOVED, EodTimes, Exit, Marks, NoAction, RefreshQuote,
    RefreshState, band_reject_limit, compute_exit_params, cover_limit, decide, entry_limit, exit_limit,
    exit_params_from_fills, exit_params_problems, is_band_reject, realizable_bid, selftest_limit, sell_limit,
)
from titan2.model import (
    EXIT_REASONS, Account, ExitProfile, Instrument, Leg, LegState, Order, OrderState, Quote, Role, Side, Urgency,
    floor_tick, is_tick_multiple, make_tag,
)
from titan2.settings import BANDS, DEFAULTS

SESSION = date(2026, 10, 12)
R26 = rates_for(SESSION)
TICK = 0.05
INST = Instrument("NFO:RELIANCE26OCT2900CE", 1001, "RELIANCE26OCT2900CE", "NFO", "RELIANCE", "NSE:RELIANCE", "CE",
                  2900.0, date(2026, 10, 27), 500, TICK)
EOD = EodTimes.for_session(SESSION, DEFAULTS)
FILL_WALL = ist_datetime(SESSION, "09:30:00")
P = compute_exit_params(ExitProfile(), 32.0, 500, 30.81, R26, FILL_WALL)     # DETAIL §7.6: C_in = 30.81
PROTECTIVE, NORMAL = Urgency.PROTECTIVE, Urgency.NORMAL


def at(hms: str):
    return ist_datetime(SESSION, hms)


def make_leg(p=P, qty=500, stop="hard"):
    pk = make_tag(Role.ENTRY, Account.PAPER, SESSION, 1, 1)
    order = Order(order_pk=pk, leg_id=1, structure_id=1, account=Account.PAPER, book="PAPER:opening_drive",
                  role=Role.ENTRY, side=Side.BUY, symbol=INST.symbol, qty=qty, limit_px=32.25, attempt=1,
                  band_idx=None, state=OrderState.COMPLETE, filled_qty=qty, avg_fill_px=p.entry_avg)
    leg = Leg(leg_id=1, structure_id=1, leg_no=1, account=Account.PAPER, book="PAPER:opening_drive", session=SESSION,
              seq_no=1, inst=INST, side="LONG", state=LegState.OPEN, orders=[pk],
              stop_px=p.hard_sl_px if stop == "hard" else stop, entry_fill_wall=p.entry_fill_wall)
    return leg.bind_orders({pk: order})


def book(px: float, qty: int = 1000):
    return [(px, qty)]


class Sim:
    """One OPEN leg evaluated the way the engine does it: decide(), then apply the returned marks to the leg."""

    def __init__(self, p=P, start="09:31:00"):
        self.p = p
        self.leg = make_leg(p)
        self.mono = 1000.0
        self.wall = at(start)
        self.forced = None

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)

    def quote(self, bids=(), asks=None, ltp=None, src="WS", age=0.0, last_trade_age=0.0) -> Quote:
        if asks is None:
            asks = [(round(bids[0][0] + 0.10, 2), 5000)] if bids else [(40.0, 5000)]
        return Quote(INST.symbol, INST.token, tuple(bids), tuple(asks), ltp, 1000,
                     self.wall - timedelta(seconds=last_trade_age), None, self.mono - age,
                     self.wall - timedelta(seconds=age), src)

    def step(self, q, refresh=None):
        d = decide(self.leg, q, refresh, self.mono, self.wall, self.p, EOD, self.forced)
        d.marks.apply_to(self.leg)
        return d


def is_exit(d, reason, urgency):
    return isinstance(d, Exit) and d.reason == reason and d.urgency is urgency


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
def test_sl_tp_on_rb_not_ltp():
    E = P.entry_avg
    s = Sim()
    # killer k10: LTP +6% while the realizable bid is -1% -> no lock, no take profit
    d = s.step(s.quote(bids=[(31.70, 200), (31.65, 300)], ltp=E * 1.06))
    assert isinstance(d, NoAction)
    assert d.rb == pytest.approx((31.70 * 200 + 31.65 * 300) / 500)
    assert not s.leg.lock_armed and s.leg.stop_px == P.hard_sl_px
    # LTP far above take profit, bid below arm: still just holding
    s.advance(0.25)
    d = s.step(s.quote(bids=book(33.0), ltp=40.0))
    assert isinstance(d, NoAction) and not s.leg.lock_armed
    # best bid above tp, but selling 500 averages 36.70 < tp 36.80: no take profit (the lock arms)
    s.advance(0.25)
    d = s.step(s.quote(bids=[(37.0, 200), (36.5, 300)], ltp=37.0))
    assert isinstance(d, NoAction) and d.rb == pytest.approx(36.70) and LOCK_ARMED in d.notes and not d.thin
    # thin book: only 100 shown at 37.30; the other 400 are valued at 37.30 x 0.98 -> rb 36.70 < tp
    s.advance(0.25)
    d = s.step(s.quote(bids=[(37.30, 100)], ltp=38.0))
    assert isinstance(d, NoAction) and d.thin
    assert d.rb == pytest.approx((37.30 * 100 + 400 * 37.30 * 0.98) / 500)
    # the full qty sells at >= tp: TAKE_PROFIT, NORMAL urgency, whatever the LTP says
    s.advance(0.25)
    d = s.step(s.quote(bids=book(36.85), ltp=30.0))
    assert is_exit(d, "TAKE_PROFIT", NORMAL) and d.rb == pytest.approx(36.85)

    s = Sim()
    # LTP below the hard stop while the bids hold: no breach at all
    d = s.step(s.quote(bids=book(31.5), ltp=20.0))
    assert isinstance(d, NoAction) and s.leg.breach_first_mono is None
    # the bids go through the stop while the LTP still shows the entry price: HARD_SL after the debounce
    s.advance(0.25)
    d = s.step(s.quote(bids=book(27.0), ltp=32.0))
    assert isinstance(d, NoAction) and STOP_BREACH_PENDING in d.notes
    s.advance(0.5)
    d = s.step(s.quote(bids=book(27.1), ltp=32.0))
    assert is_exit(d, "HARD_SL", PROTECTIVE)

    # the same bid script gives the same decisions with any LTP (or none)
    script = [book(31.5), book(33.6), book(35.2), book(34.0), book(33.9), book(33.95)]
    runs = []
    for ltp in (None, 0.05, 32.0, 99.0):
        s = Sim()
        out = []
        for bids in script:
            d = s.step(s.quote(bids=bids, ltp=ltp))
            out.append((type(d).__name__, getattr(d, "reason", None), d.rb, s.leg.stop_px))
            s.advance(0.5)
        runs.append(out)
    assert all(r == runs[0] for r in runs)
    assert runs[0][-1][:2] == ("Exit", "LOCK_STOP")

    # and decide()/realizable_bid() never read .ltp at all
    tree = ast.parse(Path(exits.__file__).read_text(encoding="utf-8"))
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in ("decide", "realizable_bid")):
        assert not [n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "ltp"], fn.name


def test_ratchet_never_lowers():
    be, floor_px = P.be_px, P.floor_px
    s = Sim()
    path = [32.5, 33.0, 33.5, 33.3, 35.0, 34.3, 34.8, 36.0, 35.5, 36.2, 35.2]
    stops = []
    for rb in path:
        d = s.step(s.quote(bids=book(rb)))
        assert isinstance(d, NoAction), (rb, d)
        stops.append(s.leg.stop_px)
        s.advance(0.5)
    assert stops == sorted(stops)                                  # never lowers
    assert stops[:2] == [P.hard_sl_px, P.hard_sl_px]
    assert stops[2] == pytest.approx(floor_px)                     # armed at 33.50: stop = floor (BE + 3%)
    assert s.leg.lock_armed                                        # stays armed after rb fell below arm (33.30)
    assert stops[4] == pytest.approx(be + 0.70 * (35.0 - be))      # DETAIL §7.6: 34.15 at peak 35.00
    assert round(stops[4], 2) == 34.15
    assert stops[-1] == pytest.approx(be + 0.70 * (36.2 - be))
    assert s.leg.peak_rb == 36.2 and s.leg.trough_rb == 32.5

    # the next breach is a LOCK_STOP, and selling the qty there is still net positive (cost-aware lock)
    stop = s.leg.stop_px
    d = s.step(s.quote(bids=book(34.95)))
    assert isinstance(d, NoAction) and STOP_BREACH_PENDING in d.notes and s.leg.stop_px == stop
    s.advance(0.5)
    d = s.step(s.quote(bids=book(34.95)))
    assert is_exit(d, "LOCK_STOP", PROTECTIVE)
    pnl = net_pnl([OrderFills(Side.BUY, 500, 16000.0), OrderFills(Side.SELL, 500, 500 * 34.95)], R26)
    assert pnl.net > 0

    # a reloaded armed leg whose stored stop lags the floor is lifted to the floor at once (never a weaker lock)
    s = Sim()
    s.leg.lock_armed, s.leg.stop_px = True, P.hard_sl_px
    d = s.step(s.quote(bids=book(33.5)))
    assert isinstance(d, NoAction) and s.leg.stop_px == pytest.approx(floor_px) and STOP_MOVED in d.notes

    # a stored stop above the current trail is kept; a corrupt one never drops under the hard stop
    for stored, expect in ((35.5, 35.5), (10.0, P.hard_sl_px), (float("nan"), P.hard_sl_px), (None, P.hard_sl_px)):
        s = Sim()
        s.leg.stop_px = stored
        if stored == 35.5:
            s.leg.lock_armed, s.leg.peak_rb = True, 36.2
        s.step(s.quote(bids=book(35.9 if stored == 35.5 else 31.0)))
        assert s.leg.stop_px == pytest.approx(expect)

    # seeded random walks: the stop is monotone up to the exit, never below hard_sl, and >= floor once armed
    for seed in range(20):
        rng = random.Random(seed)
        s, rb, last = Sim(), 32.0, 0.0
        for _ in range(400):
            rb = round(min(max(rb + rng.choice((-0.15, -0.10, -0.05, 0.05, 0.10, 0.15)), 20.0), 40.0), 2)
            d = s.step(s.quote(bids=book(rb)))
            assert s.leg.stop_px >= last and s.leg.stop_px >= P.hard_sl_px
            if s.leg.lock_armed:
                assert s.leg.stop_px >= floor_px
            last = s.leg.stop_px
            if isinstance(d, Exit):
                assert d.reason in ("HARD_SL", "LOCK_STOP", "TAKE_PROFIT", "TIME_STOP")
                break
            s.advance(0.5)


def test_stop_debounce_bypass():
    # one breached snapshot only marks the breach; re-reading that same snapshot never confirms it
    s = Sim()
    q1 = s.quote(bids=book(27.0))                     # 27.00 <= hard_sl 27.20, above the bypass line 26.24
    d = s.step(q1)
    assert isinstance(d, NoAction) and STOP_BREACH_PENDING in d.notes
    assert (s.leg.breach_first_mono, s.leg.breach_quote_mono) == (s.mono, q1.recv_mono)
    s.advance(0.6)
    d = s.step(q1)
    assert isinstance(d, NoAction) and STOP_BREACH_PENDING not in d.notes
    # a distinct later snapshot that still breaches, after confirm_ms -> exit
    d = s.step(s.quote(bids=book(27.1)))
    assert is_exit(d, "HARD_SL", PROTECTIVE)

    # a distinct snapshot inside confirm_ms waits; at exactly 400 ms the exit fires (float-safe)
    s = Sim()
    s.step(s.quote(bids=book(27.0)))
    s.advance(0.1)
    q2 = s.quote(bids=book(27.0))
    assert isinstance(s.step(q2), NoAction)
    s.advance(0.3)
    assert is_exit(s.step(q2), "HARD_SL", PROTECTIVE)

    # recovery above the stop clears the marks; a later breach starts a new debounce
    s = Sim()
    s.step(s.quote(bids=book(27.0)))
    s.advance(0.2)
    d = s.step(s.quote(bids=book(27.5)))
    assert isinstance(d, NoAction) and s.leg.breach_first_mono is None and s.leg.breach_quote_mono is None
    s.advance(0.5)
    d = s.step(s.quote(bids=book(27.0)))
    assert isinstance(d, NoAction) and STOP_BREACH_PENDING in d.notes

    # frozen WS: nothing new by confirm_max_ms -> RefreshQuote (NoAction while in flight); the REST snapshot confirms
    s = Sim()
    q1 = s.quote(bids=book(27.0))
    s.step(q1)
    s.advance(1.0)
    assert isinstance(s.step(q1), NoAction)
    s.advance(0.5)
    assert isinstance(s.step(q1), RefreshQuote)
    assert type(s.step(q1, RefreshState(in_flight=True))) is NoAction
    s.advance(0.3)
    rest = s.quote(bids=book(27.05), src="REST")
    assert is_exit(s.step(rest, RefreshState(last_ok_mono=rest.recv_mono)), "HARD_SL", PROTECTIVE)

    # a deep breach (rb <= stop - 3% of E = 26.24) exits on the first snapshot; just above it is debounced
    s = Sim()
    assert is_exit(s.step(s.quote(bids=book(26.20))), "HARD_SL", PROTECTIVE)
    s = Sim()
    assert isinstance(s.step(s.quote(bids=book(26.30))), NoAction)

    # confirm_ms = 0: the next distinct snapshot (a later receipt time) confirms at once
    s = Sim(p=compute_exit_params(replace(ExitProfile(), stop_confirm_ms=0), 32.0, 500, 30.81, R26, FILL_WALL))
    q1 = s.quote(bids=book(27.0))
    s.step(q1)
    assert isinstance(s.step(replace(q1, bids=q1.bids)), NoAction)        # same receipt time: not distinct
    s.advance(0.001)
    assert is_exit(s.step(s.quote(bids=book(27.0))), "HARD_SL", PROTECTIVE)

    # a reload (breach marks are monotonic times, never persisted) restarts the debounce, never skips it
    s = Sim()
    s.step(s.quote(bids=book(27.0)))
    s.leg.breach_first_mono = s.leg.breach_quote_mono = None
    s.advance(0.5)
    d = s.step(s.quote(bids=book(27.0)))
    assert isinstance(d, NoAction) and STOP_BREACH_PENDING in d.notes


def test_stale_no_bid_protective():
    good = book(33.0)
    s = Sim()
    # receipt age 3.5 s -> RefreshQuote; the stale bids (at take-profit level) feed nothing
    d = s.step(s.quote(bids=book(37.0), age=3.5))
    assert isinstance(d, RefreshQuote) and QUOTE_STALE in d.notes and d.rb is None
    assert s.leg.peak_rb is None and not s.leg.lock_armed
    # a refresh already in flight: wait for it
    assert type(s.step(s.quote(bids=good, age=3.5), RefreshState(in_flight=True))) is NoAction
    # the refresh failed after the quote went stale -> protective exit now (before 5 s)
    q = s.quote(bids=good, age=3.5)                    # stale since mono - 0.5
    assert is_exit(s.step(q, RefreshState(last_fail_mono=s.mono - 0.1)), "STALE_QUOTE_PROTECTIVE", PROTECTIVE)
    # a failure from before the quote went stale is not this refresh: ask again
    assert isinstance(s.step(q, RefreshState(last_fail_mono=s.mono - 1.0)), RefreshQuote)
    # older than 5 s -> protective exit even while a refresh is still in flight
    d = s.step(s.quote(bids=good, age=5.01), RefreshState(in_flight=True))
    assert is_exit(d, "STALE_QUOTE_PROTECTIVE", PROTECTIVE)
    # no snapshot at all is infinitely old
    assert is_exit(s.step(None), "STALE_QUOTE_PROTECTIVE", PROTECTIVE)
    # the age is receipt time, never last-trade time: a fresh snapshot of an untraded contract is usable
    assert isinstance(s.step(s.quote(bids=good, last_trade_age=900.0)), NoAction)
    # stale is checked before the empty-book rule
    assert isinstance(s.step(s.quote(bids=[], age=3.5)), RefreshQuote)

    # empty bid side: REST first, then NO_BID_PROTECTIVE after no_bid_exit_s (10 s)
    s = Sim()
    d = s.step(s.quote(bids=[]))
    t0 = s.mono
    assert isinstance(d, RefreshQuote) and NO_BID in d.notes and s.leg.no_bid_since_mono == t0
    assert type(s.step(s.quote(bids=[]), RefreshState(in_flight=True))) is NoAction
    s.advance(0.4)
    rest = s.quote(bids=[], src="REST")
    ok = RefreshState(last_ok_mono=rest.recv_mono)
    assert isinstance(s.step(rest, ok), NoAction)
    s.advance(5.0)
    assert isinstance(s.step(s.quote(bids=[]), ok), NoAction)
    s.advance(4.6)                                     # exactly 10 s after the first empty snapshot
    d = s.step(s.quote(bids=[]), ok)
    assert is_exit(d, "NO_BID_PROTECTIVE", PROTECTIVE) and s.leg.no_bid_since_mono == t0

    # WS alone never decides: with no REST answer the rule keeps asking, even after 12 s
    s = Sim()
    for _ in range(25):
        d = s.step(s.quote(bids=[]))
        s.advance(0.5)
    assert isinstance(d, RefreshQuote)
    # ... but a REST refresh that failed counts as tried: a REST outage cannot hold the leg open forever
    t0 = s.leg.no_bid_since_mono
    assert is_exit(s.step(s.quote(bids=[]), RefreshState(last_fail_mono=t0 + 1.0)), "NO_BID_PROTECTIVE", PROTECTIVE)

    # bids coming back clear the mark
    s = Sim()
    s.step(s.quote(bids=[]))
    s.advance(1.0)
    assert isinstance(s.step(s.quote(bids=good)), NoAction) and s.leg.no_bid_since_mono is None


def test_no_snapshot_after_restart_refreshes_first():
    """G5 / §9.6 step 7: a leg restored after a restart has no snapshot yet; its age runs from last_fill_mono (the
    process start, §9.2). REST first; protective once the refresh fails or STALE_PROTECTIVE_S passes."""
    s = Sim()
    s.leg.last_fill_mono = s.mono                      # Engine.load re-anchors it to the process start
    stop, armed = s.leg.stop_px, s.leg.lock_armed
    d = s.step(None)
    assert isinstance(d, RefreshQuote) and QUOTE_STALE in d.notes and d.rb is None
    assert (s.leg.stop_px, s.leg.lock_armed) == (stop, armed)          # nothing restored is touched
    assert type(s.step(None, RefreshState(in_flight=True))) is NoAction
    # the REST refresh failed after the restart: protective now
    assert is_exit(s.step(None, RefreshState(last_fail_mono=s.mono)), "STALE_QUOTE_PROTECTIVE", PROTECTIVE)
    # no answer at all: protective once STALE_PROTECTIVE_S has passed since the anchor
    s.advance(4.9)
    assert type(s.step(None, RefreshState(in_flight=True))) is NoAction
    s.advance(0.2)
    assert is_exit(s.step(None, RefreshState(in_flight=True)), "STALE_QUOTE_PROTECTIVE", PROTECTIVE)
    # the REST snapshot arrives in time: evaluated as usual
    s = Sim()
    s.leg.last_fill_mono = s.mono
    s.step(None)
    s.advance(0.4)
    rest = s.quote(bids=book(33.0), src="REST")
    assert isinstance(s.step(rest, RefreshState(last_ok_mono=rest.recv_mono)), NoAction)
    # forced and EOD rules still come first
    s = Sim()
    s.leg.last_fill_mono, s.forced = s.mono, "KILL_SWITCH"
    assert is_exit(s.step(None), "KILL_SWITCH", PROTECTIVE)


def test_time_stop_unlocked_only():
    assert P.time_stop_at == at("10:15:00")           # fill 09:30 + 45 min, wall clock
    s = Sim(start="10:14:59")
    assert isinstance(s.step(s.quote(bids=book(32.5))), NoAction)
    s.advance(1.0)
    assert is_exit(s.step(s.quote(bids=book(32.5))), "TIME_STOP", NORMAL)

    # once the lock is armed there is no time stop (default time_stop_when_locked=False)
    s = Sim(start="10:10:00")
    s.step(s.quote(bids=book(33.6)))
    assert s.leg.lock_armed
    s.advance(600.0)
    assert isinstance(s.step(s.quote(bids=book(33.6))), NoAction)

    # a profile with time_stop_when_locked=True exits locked legs too
    p2 = compute_exit_params(replace(ExitProfile(), time_stop_when_locked=True), 32.0, 500, 30.81, R26, FILL_WALL)
    s = Sim(p=p2, start="10:10:00")
    s.step(s.quote(bids=book(33.6)))
    s.advance(600.0)
    assert is_exit(s.step(s.quote(bids=book(33.6))), "TIME_STOP", NORMAL)

    # the wall clock decides, not the monotonic clock
    s = Sim(start="10:14:00")
    s.wall = at("10:15:00")
    assert is_exit(s.step(s.quote(bids=book(32.5))), "TIME_STOP", NORMAL)


def test_exit_limit_bands_cap_lpp():
    q = Sim().quote(bids=[(40.00, 300), (39.90, 300), (39.80, 1000)])
    # fresh pricing for 500: sweep = 39.90 (the worst level needed); one limit per band, never above the bid
    limits = [exit_limit(q, 500, TICK, b) for b in range(len(BANDS))]
    assert limits == [39.80, 39.80, 39.60, 39.20, 38.40, 36.80, 35.20]
    assert limits == sorted(limits, reverse=True)
    # an out-of-range band is clamped, never refused (no exit is blocked by a bad index)
    assert exit_limit(q, 500, TICK, 99) == 35.20 and exit_limit(q, 500, TICK, -1) == 39.80

    # a deep sweep is capped at 12% under the fresh bid (I13: limit >= floor_tick(bid x 0.88))
    deep = Sim().quote(bids=[(40.00, 100), (30.00, 1000)])
    assert exit_limit(deep, 500, TICK, 0) == 35.20
    # protective pricing (the last known quote): sweep = ref_bid
    thin = Sim().quote(bids=[(40.00, 100), (38.00, 1000)])
    assert exit_limit(thin, 500, TICK, 3) == 37.90
    assert exit_limit(thin, 500, TICK, 3, protective=True) == 39.20
    # tiny premium: never below one tick
    assert exit_limit(Sim().quote(bids=[(0.10, 5000)]), 500, TICK, 0) == 0.10
    # no bids, nothing to sell, or no quote -> no price, so no order into an empty book
    empty = Sim().quote(bids=[])
    assert exit_limit(empty, 500, TICK, 0) is None and exit_limit(None, 500, TICK, 3, protective=True) is None
    assert exit_limit(q, 0, TICK, 0) is None
    assert sell_limit(40.0, 39.9, 0.0, 0) is None and sell_limit(float("nan"), 39.9, TICK, 0) is None

    # every limit, over random books: on the tick grid, <= the best bid, >= floor_tick(bid x 0.88)
    rng = random.Random(11)
    for _ in range(500):
        best = round(rng.randrange(2, 4000) * TICK, 2)
        levels = sorted({round(best - k * TICK * rng.randrange(1, 40), 2) for k in range(rng.randrange(1, 6))},
                        reverse=True)
        bids = [(px, rng.randrange(1, 3000)) for px in levels if px > 0]
        quote = Sim().quote(bids=bids)
        qty, band = rng.randrange(1, 6000), rng.randrange(0, 7)
        for lim in (exit_limit(quote, qty, TICK, band), exit_limit(quote, qty, TICK, band, protective=True)):
            assert is_tick_multiple(lim, TICK)
            assert floor_tick(quote.bid * 0.88, TICK) <= lim <= quote.bid + 1e-9

    # LPP / price-band rejects keep the band and re-price at max(prev_limit, floor_tick(fresh_bid - 2 ticks))
    assert is_band_reject("RMS: Price outside the LPP range") and is_band_reject("Circuit limit breached")
    assert is_band_reject("order price is out of the price band") and is_band_reject("Market protection")
    assert not is_band_reject("Insufficient funds") and not is_band_reject(None) and not is_band_reject("")
    assert band_reject_limit(39.20, 40.50, TICK) == 40.40
    assert band_reject_limit(39.20, 39.00, TICK) == 39.20
    assert band_reject_limit(39.20, None, TICK) == 39.20
    # a sub-rupee bid: bid - 2 ticks would sit more than 12% under it; I13's bound (sell_limit's floor) holds
    assert band_reject_limit(0.40, 0.60, TICK) == 0.55 and band_reject_limit(0.20, 0.40, TICK) == 0.40
    assert band_reject_limit(0.05, 0.25, TICK) == 0.25          # ceil_tick(0.22) = 0.25; floor_tick(0.15) = 0.15

    # ENTRY BUY: min(ask + 2 ticks, floor_tick(mid x 1.01)); refuse below the ask or when the walk falls short
    e = Sim().quote(bids=[(42.00, 1000)], asks=[(42.15, 300), (42.20, 300), (42.30, 1000)])
    assert entry_limit(e, TICK) == 42.25 and entry_limit(e, TICK, 500) == 42.25
    e2 = Sim().quote(bids=[(42.00, 1000)], asks=[(42.15, 300), (42.30, 1000)])
    assert entry_limit(e2, TICK, 300) == 42.25 and entry_limit(e2, TICK, 500) is None
    mid_cap = Sim().quote(bids=[(40.00, 1000)], asks=[(40.65, 1000)])          # floor_tick(40.325 x 1.01) = 40.70
    assert entry_limit(mid_cap, TICK) == 40.70
    assert entry_limit(Sim().quote(bids=[(40.00, 1000)], asks=[(41.00, 1000)]), TICK) is None   # 40.90 < ask
    assert entry_limit(Sim().quote(bids=[], asks=[(41.0, 100)]), TICK) is None                 # one side missing
    assert entry_limit(Sim().quote(bids=[(41.0, 100)], asks=[(40.0, 100)]), TICK) is None    # crossed: no mid
    assert entry_limit(e, TICK, 0) is None and entry_limit(None, TICK) is None
    for qe in (e, e2, mid_cap):
        lim = entry_limit(qe, TICK)
        assert is_tick_multiple(lim, TICK) and qe.ask <= lim <= floor_tick(qe.mid() * 1.01, TICK)

    # COVER BUY mirrors the ladder, capped at floor_tick(ref_ask x 1.12)
    c = Sim().quote(bids=[(9.90, 1000)], asks=[(10.00, 300), (10.10, 1000)])
    assert cover_limit(c, 500, TICK, 0) == 10.20
    assert cover_limit(c, 500, TICK, 6) == 11.20
    assert cover_limit(c, 500, TICK, 3, protective=True) == 10.20
    assert cover_limit(Sim().quote(bids=[(9.9, 10)], asks=[]), 500, TICK, 0) is None

    # SELFTEST BUY sits strictly under the best bid, so it can never fill
    assert selftest_limit(Sim().quote(bids=[(40.00, 10)]), TICK) == 38.80
    assert selftest_limit(Sim().quote(bids=[(0.10, 10)]), TICK) == 0.05
    assert selftest_limit(Sim().quote(bids=[(0.05, 10)]), TICK) is None
    assert selftest_limit(Sim().quote(bids=[]), TICK) is None


# ---------------------------------------------------------------------------------------------------------------
# Extra tests
# ---------------------------------------------------------------------------------------------------------------
def test_frozen_levels_worked_example():
    assert P.k_exit == pytest.approx(0.00192043, abs=1e-8)
    assert [round(x, 2) for x in (P.be_px, P.hard_sl_px, P.arm_px, P.floor_px, P.tp_px)] == [
        32.17, 27.20, 33.45, 33.13, 36.80]
    assert (P.entry_avg, P.qty, P.entry_charges, P.rates_version) == (32.0, 500, 30.81, "2026-04-01")
    assert exit_params_problems(P) == []
    # be_px really is the net-zero exit price (to the paisa of charge rounding)
    flat = net_pnl([OrderFills(Side.BUY, 500, 16000.0), OrderFills(Side.SELL, 500, 500 * P.be_px)], R26)
    assert abs(flat.net) <= 0.02
    # selling at the lock floor nets about 3% of the premium (DETAIL §7.6: ~Rs 479)
    at_floor = net_pnl([OrderFills(Side.BUY, 500, 16000.0), OrderFills(Side.SELL, 500, 500 * P.floor_px)], R26)
    assert at_floor.net == pytest.approx(479, abs=1.0)
    # the same levels from the entry fills; two entry orders pay two brokerages, so BE rises
    assert exit_params_from_fills(ExitProfile(), [OrderFills(Side.BUY, 500, 16000.0)], R26, FILL_WALL) == P
    two = exit_params_from_fills(ExitProfile(), [OrderFills(Side.BUY, 200, 6400.0), OrderFills(Side.BUY, 300, 9600.0)],
                                 R26, FILL_WALL)
    assert two.qty == 500 and two.entry_avg == pytest.approx(32.0) and two.be_px > P.be_px
    with pytest.raises(ValueError):
        exit_params_from_fills(ExitProfile(), [OrderFills(Side.SELL, 500, 16000.0)], R26, FILL_WALL)
    for bad in ((0.0, 500, 30.81), (32.0, 0, 30.81), (32.0, 500.0, 30.81), (32.0, 500, -1.0), (float("nan"), 500, 1)):
        with pytest.raises(ValueError):
            compute_exit_params(ExitProfile(), *bad, R26, FILL_WALL)
    with pytest.raises(ValueError):
        compute_exit_params(ExitProfile(), 32.0, 500, 30.81, R26, FILL_WALL.replace(tzinfo=None))
    # wall times are frozen at whole milliseconds (the stored precision), so a reload reproduces them exactly
    p_us = compute_exit_params(replace(ExitProfile(), time_stop_min=10.0001), 32.0, 500, 30.81, R26,
                               FILL_WALL + timedelta(microseconds=123456))
    assert p_us.entry_fill_wall == FILL_WALL + timedelta(milliseconds=123)
    assert p_us.time_stop_at.microsecond % 1000 == 0
    assert p_us.time_stop_at - p_us.entry_fill_wall == pytest.approx(timedelta(minutes=10.0001), abs=timedelta(
        milliseconds=1))


def test_forced_and_eod_come_first():
    for reason in sorted(exits.FORCED_REASONS):
        s = Sim()
        s.forced = reason
        assert reason in EXIT_REASONS
        assert is_exit(s.step(None), reason, PROTECTIVE)                     # even with no quote at all
        assert is_exit(s.step(s.quote(bids=book(36.9))), reason, PROTECTIVE)  # and over a take profit
    s = Sim()
    s.forced = "NOT_A_REASON"
    assert is_exit(s.step(s.quote(bids=book(33.0))), "ANOMALY_EXIT", PROTECTIVE)  # a requested exit is never dropped
    s = Sim()
    s.forced = "EOD_FLATTEN"                                                      # a real reason keeps its name
    assert is_exit(s.step(s.quote(bids=book(33.0))), "EOD_FLATTEN", PROTECTIVE)
    s = Sim()
    s.forced = ""                                                                 # an empty flag is no flag
    assert isinstance(s.step(s.quote(bids=book(33.0))), NoAction)
    late = compute_exit_params(ExitProfile(), 32.0, 500, 30.81, R26, at("15:00:00"))   # time stop 15:45
    s = Sim(p=late, start="15:14:59")
    assert isinstance(s.step(s.quote(bids=book(33.0))), NoAction)
    s.advance(1.0)
    assert is_exit(s.step(s.quote(bids=book(36.9))), "EOD_FLATTEN", PROTECTIVE)  # EOD beats take profit
    assert is_exit(s.step(None), "EOD_FLATTEN", PROTECTIVE)                       # and beats staleness


def test_untrustworthy_params_exit_protectively():
    for bad in (None, replace(P, hard_sl_px=float("nan")), replace(P, tp_px=float("inf")),
                replace(P, hard_sl_px=P.entry_avg), replace(P, floor_px=P.be_px - 0.01),
                replace(P, tp_px=P.arm_px - 0.01), replace(P, profile=replace(P.profile, stop_confirm_ms=float("nan"))),
                replace(P, profile=replace(P.profile, hard_sl_pct=40.0)), replace(P, qty=0)):
        assert exit_params_problems(bad)
        s = Sim()
        s.p = bad
        assert is_exit(s.step(s.quote(bids=book(32.5))), "ANOMALY_EXIT", PROTECTIVE)
    # another contract's book never drives this leg's stop
    s = Sim()
    other = replace(s.quote(bids=book(20.0)), symbol="NFO:RELIANCE26OCT2900PE")
    assert is_exit(s.step(other), "ANOMALY_EXIT", PROTECTIVE) and s.leg.peak_rb is None


def test_realizable_bid_walks_open_qty():
    q = Sim().quote(bids=[(10.0, 100), (9.9, 100)])
    assert realizable_bid(q, 150) == (pytest.approx((10.0 * 100 + 9.9 * 50) / 150), False)
    rb, thin = realizable_bid(q, 500)
    assert thin and rb == pytest.approx((10.0 * 100 + 9.9 * 100 + 300 * 9.9 * 0.98) / 500)
    assert realizable_bid(Sim().quote(bids=[]), 500) == (None, False)
    assert realizable_bid(None, 500) == (None, False) and realizable_bid(q, 0) == (None, False)
    # decide values the leg's open qty, not one lot or the top level
    s = Sim()
    d = s.step(s.quote(bids=[(37.0, 100), (33.0, 1000)]))
    assert d.rb == pytest.approx((37.0 * 100 + 33.0 * 400) / 500) and not isinstance(d, Exit)


def test_decide_is_pure_and_marks_roundtrip():
    s = Sim()
    s.step(s.quote(bids=book(33.6)))                                  # armed: lock stop 33.17, peak set
    before = copy.copy(Marks.of(s.leg))
    s.advance(0.25)
    q = s.quote(bids=book(33.1))                                      # a shallow lock-stop breach: debounced
    d1 = decide(s.leg, q, None, s.mono, s.wall, P, EOD, None)
    d2 = decide(s.leg, q, None, s.mono, s.wall, P, EOD, None)
    assert d1 == d2 and Marks.of(s.leg) == before                     # decide never mutates the leg
    d1.marks.apply_to(s.leg)
    assert Marks.of(s.leg) == d1.marks and s.leg.breach_first_mono == s.mono
    assert STOP_MOVED not in d1.notes and LOCK_ARMED not in d1.notes
    # a flat leg needs nothing from the quote rules
    flat = Sim()
    flat.leg.leg_orders()[0].filled_qty = 0
    assert type(flat.step(flat.quote(bids=book(20.0)))) is NoAction


def test_eod_times_from_settings():
    assert (EOD.flatten, EOD.backstop, EOD.stop_sending) == (at("15:15:00"), at("15:22:00"), at("15:25:00"))
    assert (EOD.stale_refresh_s, EOD.stale_protective_s) == (3.0, 5.0)
    tight = EodTimes.for_session(SESSION, replace(DEFAULTS, STALE_REFRESH_S=2.0, STALE_PROTECTIVE_S=4.0,
                                                  EOD_FLATTEN=at("15:00:00").time()))
    assert tight.flatten == at("15:00:00") and tight.stale_refresh_s == 2.0
    with pytest.raises(ValueError):
        EodTimes(at("15:15:00").replace(tzinfo=None), at("15:22:00"), at("15:25:00"), 3.0, 5.0)
    with pytest.raises(ValueError):
        EodTimes(at("15:15:00"), at("15:22:00"), at("15:25:00"), 5.0, 3.0)
    # the tightened staleness limits are the ones decide() uses
    s = Sim()
    d = decide(s.leg, s.quote(bids=book(33.0), age=2.5), None, s.mono, s.wall, P, tight, None)
    assert isinstance(d, RefreshQuote)


def test_pm_sonacoms_frozen_ws_rest_debounce():
    """Postmortem d (SONACOMS): WS frozen at 14.0 while REST shows 11.5 -> HARD_SL, debounced through REST
    snapshots; with REST down the leg exits protectively by 5 s."""
    p = compute_exit_params(ExitProfile(), 14.0, 500, 30.0, R26, FILL_WALL)   # hard_sl 11.90, bypass line 11.48
    s = Sim(p=p)
    ws = s.quote(bids=book(14.0))
    assert isinstance(s.step(ws), NoAction)
    s.advance(3.2)                                                    # WS frozen: no new packets
    assert isinstance(s.step(ws), RefreshQuote)
    s.advance(0.3)
    rest1 = s.quote(bids=book(11.5), src="REST")
    d = s.step(rest1, RefreshState(last_ok_mono=rest1.recv_mono))
    assert isinstance(d, NoAction) and STOP_BREACH_PENDING in d.notes
    s.advance(1.5)
    assert isinstance(s.step(rest1, RefreshState(last_ok_mono=rest1.recv_mono)), RefreshQuote)
    s.advance(0.3)
    rest2 = s.quote(bids=book(11.5), src="REST")
    assert is_exit(s.step(rest2, RefreshState(last_ok_mono=rest2.recv_mono)), "HARD_SL", PROTECTIVE)

    # REST down: the refresh fails -> protective exit at once; a hung refresh -> protective exit at 5 s
    s = Sim(p=p)
    ws = s.quote(bids=book(14.0))
    s.step(ws)
    s.advance(3.2)
    assert isinstance(s.step(ws), RefreshQuote)
    s.advance(0.2)
    assert is_exit(s.step(ws, RefreshState(last_fail_mono=s.mono)), "STALE_QUOTE_PROTECTIVE", PROTECTIVE)
    s = Sim(p=p)
    ws = s.quote(bids=book(14.0))
    s.step(ws)
    s.advance(4.9)
    assert type(s.step(ws, RefreshState(in_flight=True))) is NoAction
    s.advance(0.11)
    assert is_exit(s.step(ws, RefreshState(in_flight=True)), "STALE_QUOTE_PROTECTIVE", PROTECTIVE)
    assert not math.isnan(s.leg.stop_px)
