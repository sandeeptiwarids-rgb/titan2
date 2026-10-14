"""price_spike_down v1: a sharp downward move in a stock that holds for 12 s. Stock options (ATM PE), PAPER.

A PAPER experiment added on 2026-10-12 at the user's request, against brief E1: E1 deleted v1's PRICE_SPIKE_DOWN
(it won 1 of 6 trades in the 06-10..07-14 regime, an anecdotal sample), and price_spike_up never trades DOWN for
that reason. This strategy tests the idea on its own G1 trial and scorecard, PAPER like every strategy until G1.

It is the exact, signed mirror of price_spike_up v1 (spec §10.4): the same baselines, volume confirmation, window,
cooldown and thresholds, with every move measured downward. The downward move from a baseline is
(baseline - ltp) / baseline x 100, so a fall is positive and a rise is negative.

Per stock, on every underlying tick (recv_mono for intervals, recv_wall for clock-of-day):
1. Volume delta d = max(0, cum - prev_cum) once prev_cum > 0; a positive d joins the last-50 history.
2. Baselines: A resets once older than 60 s; B starts 30 s after A and resets once older than 60 s.
3. With a pending sustain go to 6. In cooldown or outside 09:20-14:55, skip detection (nothing is queued).
4. m = max(mA, mB) of the downward moves, signed: only a downward move counts. mB needs B at least 5 s old.
5. Trigger when m >= 1.0% (before 09:45) or 0.6% (from 09:45) and volume confirms (fewer than 3 deltas, or their
   average is 0, or d >= 0.5 x average).
6. held = signed downward move from the trigger baseline; peak = max(peak, held), the move at the trough. At
   t0 + 12 s the sustain passes only if held >= max(0.35, 1.0 before 09:55), held > 0 and the retrace from the
   trough, 1 - held/peak, is <= 65%. Pass or fail, the 90 s cooldown starts. A pass emits Intent(DOWN, ATM,
   trigger_px = ltp, recheck [ltp x 0.995, baseline x 0.9965], 10 s): the mirror of price_spike_up's
   [baseline x 1.0035, ltp x 1.005], so the stock may fall another 0.5% before the order but must stay 0.35% under
   the baseline.

A move that reverses up through its baseline fails (held <= 0): the mirror of the brief §4.1 #9 fix, where v1's
abs() passed a reversed move. Never UP, never flipped (E3); no scorer, OI, ML or lot scaling (E4). The reason keys
are price_spike_up's (m, held, peak, retrace, vol_ratio, baseline, detect_px), with m, held and peak the downward
moves. The gateway's placebo for each PAPER entry is the opposite option type: a CE on the same snapshot.
"""

# Mirror of titan2/strategies/price_spike_up.py v1 (2026-10-12), itself ported from agentic_trader/kite_ticker.py
#   @b4694f3 — changes: every move is measured downward ((baseline - ltp) / baseline), so the trigger, the sustain,
#   the trough retrace and the recheck band are the upward rules mirrored; direction DOWN (ATM PE) only. Not a port
#   of v1's PRICE_SPIKE_DOWN: its abs() move and abs() sustain are not reproduced. Self-contained on purpose: a
#   strategy's version and REGISTRY.lock entry hash only its own module and strategies/base.py.

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, time
from types import MappingProxyType
from typing import Sequence

from titan2.model import Direction, ExitProfile, Intent, Recheck, StrikePref, UnderlyingTick
from titan2.strategies.base import (
    EPS, MARKET_OPEN, MarketView, Strategy, in_window, ist_day, ist_tod, make_intent, parse_tod,
)


@dataclass(slots=True)
class _Pending:
    baseline: float
    t0: float
    trigger_px: float
    peak: float                                # the largest downward move seen: the move at the trough
    m: float
    vol_ratio: float


@dataclass(slots=True)
class _Sym:
    day: date
    deltas: deque = field(default_factory=deque)
    prev_cum: int = 0
    a: tuple[float, float] | None = None       # baseline A: (price, recv_mono)
    b: tuple[float, float] | None = None       # baseline B, offset 30 s from A
    pending: _Pending | None = None
    cooldown_from: float | None = None
    last_t: float | None = None


def _num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) < float("inf")


def _down_pct(baseline: float, ltp: float) -> float:
    """The downward move from baseline to ltp in percent: positive when the stock fell, negative when it rose."""
    return (baseline - ltp) / baseline * 100.0


class PriceSpikeDown(Strategy):
    NAME = "price_spike_down"
    LOGIC_VERSION = 1
    PARAMS = MappingProxyType({
        "spike_pct_open": 1.0, "open_until": "09:45:00", "spike_pct": 0.6, "base_window_s": 60,
        "base_b_offset_s": 30, "base_b_min_age_s": 5, "vol_confirm_mult": 0.5, "vol_hist": 50, "vol_min_hist": 3,
        "sustain_s": 12, "hold_pct": 0.35, "early_end": "09:55:00", "early_min_hold_pct": 1.0,
        "retrace_max_pct": 65, "cooldown_s": 90, "window": ("09:20:00", "14:55:00"), "strike": "ATM", "ttl_s": 10,
        "recheck_chase_pct": 0.5,
    })
    EXIT_PROFILE = ExitProfile()
    UNIVERSE = "STOCKS"
    WINDOW = ("09:20:00", "14:55:00")

    def __init__(self, version: str) -> None:
        super().__init__(version)
        p = self.PARAMS
        self._open_until = parse_tod(p["open_until"])
        self._early_end = parse_tod(p["early_end"])
        self._window = (parse_tod(p["window"][0]), parse_tod(p["window"][1]))
        self._syms: dict[str, _Sym] = {}

    def on_tick(self, t: UnderlyingTick, mv: MarketView) -> Sequence[Intent]:
        p = self.PARAMS
        tod, day = ist_tod(t.recv_wall), ist_day(t.recv_wall)
        now, ltp = t.recv_mono, t.ltp
        if tod < MARKET_OPEN or not (_num(ltp) and ltp > 0 and _num(now)):
            return ()
        if t.symbol not in mv.universe():
            self._syms.pop(t.symbol, None)         # an ineligible symbol keeps no baseline or pending sustain
            return ()
        st = self._syms.get(t.symbol)
        if st is None or st.day != day:
            st = self._syms[t.symbol] = _Sym(day, deque(maxlen=p["vol_hist"]))
        if st.last_t is not None and now < st.last_t:
            return ()                              # out-of-order tick
        st.last_t = now

        d = self._volume_delta(st, t.cum_volume)
        self._update_baselines(st, ltp, now)
        if st.pending is not None:
            return self._sustain(st, t, tod)
        if st.cooldown_from is not None and now - st.cooldown_from < p["cooldown_s"]:
            return ()
        if in_window(tod, self._window):
            self._detect(st, ltp, now, tod, d)
        return ()

    def on_feed_reset(self) -> None:
        # A reconnect gap makes every baseline and pending sustain meaningless, and the first cumulative volume
        # after it would count the whole gap as one delta. Cooldowns and the delta history stay.
        for st in self._syms.values():
            st.a = st.b = st.pending = None
            st.prev_cum = 0

    def _volume_delta(self, st: _Sym, cum: object) -> int:
        if not isinstance(cum, int) or isinstance(cum, bool) or cum <= 0:
            return 0
        d = max(0, cum - st.prev_cum) if st.prev_cum > 0 else 0
        st.prev_cum = cum
        if d > 0:
            st.deltas.append(d)
        return d

    def _update_baselines(self, st: _Sym, ltp: float, now: float) -> None:
        p = self.PARAMS
        if st.a is None or now - st.a[1] > p["base_window_s"]:
            st.a = (ltp, now)
        if st.b is None:
            if now - st.a[1] >= p["base_b_offset_s"]:
                st.b = (ltp, now)
        elif now - st.b[1] > p["base_window_s"]:
            st.b = (ltp, now)

    def _detect(self, st: _Sym, ltp: float, now: float, tod: time, d: int) -> None:
        p = self.PARAMS
        base = st.a[0]
        m = _down_pct(base, ltp)
        if st.b is not None and now - st.b[1] >= p["base_b_min_age_s"]:
            m_b = _down_pct(st.b[0], ltp)
            if m_b > m:                            # signed: the baseline that shows the larger DOWN move
                m, base = m_b, st.b[0]
        theta = p["spike_pct_open"] if tod < self._open_until else p["spike_pct"]
        if m < theta - EPS:
            return
        hist = st.deltas
        avg = sum(hist) / len(hist) if hist else 0.0
        known = len(hist) >= p["vol_min_hist"] and avg > 0
        if known and d < p["vol_confirm_mult"] * avg:
            return                                 # a spike on dry volume
        st.pending = _Pending(baseline=base, t0=now, trigger_px=ltp, peak=m, m=m,
                              vol_ratio=d / avg if known else 1.0)

    def _sustain(self, st: _Sym, t: UnderlyingTick, tod: time) -> Sequence[Intent]:
        p, pend = self.PARAMS, st.pending
        held = _down_pct(pend.baseline, t.ltp)
        pend.peak = max(pend.peak, held)
        if t.recv_mono - pend.t0 < p["sustain_s"]:
            return ()
        st.pending, st.cooldown_from = None, t.recv_mono      # pass or fail, the cooldown starts
        need = max(p["hold_pct"], p["early_min_hold_pct"] if tod < self._early_end else 0.0)
        retrace = (1.0 - held / pend.peak) * 100.0 if pend.peak > 0 else 0.0
        passed = held >= need - EPS and held > 0 and (pend.peak <= 0 or retrace <= p["retrace_max_pct"] + EPS)
        if not passed or not in_window(tod, self._window):
            return ()
        return (make_intent(
            type(self), self.version, underlying=t.symbol, direction=Direction.DOWN,
            strike_pref=StrikePref(p["strike"]), trigger_px=t.ltp,
            recheck=Recheck(t.ltp * (1 - p["recheck_chase_pct"] / 100.0), pend.baseline * (1 - p["hold_pct"] / 100.0)),
            ttl_s=float(p["ttl_s"]), now_mono=t.recv_mono, now_wall=t.recv_wall,
            reason={"m": round(pend.m, 4), "held": round(held, 4), "peak": round(pend.peak, 4),
                    "retrace": round(retrace, 2), "vol_ratio": round(pend.vol_ratio, 3),
                    "baseline": pend.baseline, "detect_px": pend.trigger_px},
        ),)
