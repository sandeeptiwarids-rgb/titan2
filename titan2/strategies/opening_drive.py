"""opening_drive v1 (spec §10.3, brief E2(a)): follow a strong, high-volume first 5-minute bar. Stock options, PAPER.

The 09:15 bar (09:15:00-09:19:59.999) is built by market/bars.py from WS quote ticks: open = the exchange day open,
high/low = LTP extremes including the open, close = the last LTP before 09:20:00, volume = cumulative volume_traded
(pre-open included, [V] by the 15:36 bar check). The bar builder marks it invalid (FEED_GAP) when the feed dropped
or ticks were lost during the bar, or no tick arrived; the feed counts as down until its first connect, so a late
start is invalid too. That bar validity is the ground truth: an invalid bar skips the day (SKIP:INVALID_BAR). The
host closes the 09:15 bars before it calls on_timer at or after 09:20:00.

A WS reconnect (on_feed_reset) taints the day only when the strategy can place it inside the opening bar, i.e. its
latest timer fell in [bar_start, emit_until): its own fail-closed copy of the bar builder's rule. The host delivers
resets even before StartSignals, when no timer has run yet; such a reset cannot be placed and leaves the day to the
bar's validity (review F-17: a pre-market reconnect used to skip the whole session).

Signal (all must hold), with ret = close/open - 1:
- UP when ret >= +2.5%, DOWN when ret <= -2.5%;
- volume >= 3 x the mean volume of the previous session's last 20 HIST bars (the backtest's avgV window crosses the
  day boundary); fewer than 20 such bars -> SKIP:NO_HISTORY;
- VSURGE retrace (backtest_bs): UP needs (high - close)/(high - open) <= 0.65; DOWN needs (close - low)/(open - low)
  <= 0.65.

Emission: one batch at the first on_timer with 09:20:00 <= now < 09:20:10, sorted by |ret| descending, at most one
intent per symbol per day: ITM1, trigger_px = close, ttl 10 s, recheck on the underlying LTP UP [close x 0.995,
close x 1.010] and DOWN [close x 0.990, close x 1.005]. Exits use the default ExitProfile.

Evidence: low-confidence paper hypothesis (BS model +2.27%, DG analogue -0.24%). The backtested exits (TP8/SL30/
lock to BE+1%) violate X4/X5 and are not run; adopting any other exit later is a new registered version.
"""

from __future__ import annotations

from datetime import date, datetime
from types import MappingProxyType
from typing import Sequence

from titan2.model import Direction, ExitProfile, Intent, Recheck, StrikePref
from titan2.strategies.base import EPS, MarketView, Strategy, ist_day, ist_tod, make_intent, parse_tod


def _price(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x < float("inf")


def _count(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


class OpeningDrive(Strategy):
    NAME = "opening_drive"
    LOGIC_VERSION = 1
    PARAMS = MappingProxyType({
        "bar_start": "09:15:00", "min_ret_pct": 2.5, "min_vol_mult": 3.0, "prev_bars": 20, "retrace_max": 0.65,
        "strike": "ITM1", "ttl_s": 10, "recheck_lo_pct": 0.5, "recheck_hi_pct": 1.0,
        "emit_at": "09:20:00", "emit_until": "09:20:10",
    })
    EXIT_PROFILE = ExitProfile()
    UNIVERSE = "STOCKS"
    WINDOW = ("09:20:00", "09:20:10")

    def __init__(self, version: str) -> None:
        super().__init__(version)
        p = self.PARAMS
        self._bar_start = parse_tod(p["bar_start"])
        self._emit_at = parse_tod(p["emit_at"])
        self._emit_until = parse_tod(p["emit_until"])
        self._day: date | None = None
        self._done = False                       # today's batch was evaluated (once, at the first timer >= emit_at)
        self._tainted = False                    # a WS reconnect inside today's opening bar (on_feed_reset)
        self._last_wall: datetime | None = None  # the latest timer seen (on_feed_reset has no time of its own)
        self.skips: dict[str, str] = {}          # symbol -> why today's batch skipped it ("*" = all); logs only

    def on_timer(self, now: datetime, mv: MarketView) -> Sequence[Intent]:
        day, tod = ist_day(now), ist_tod(now)
        if day != self._day:
            self._day, self._done, self._tainted, self.skips = day, False, False, {}
        self._last_wall = now
        if self._done or tod < self._emit_at:
            return ()
        self._done = True                        # one batch per day: a missed window is not caught up
        if tod >= self._emit_until:
            self.skips = {"*": "SKIP:WINDOW_MISSED"}
            return ()
        if self._tainted or not mv.feed_ok():
            self.skips = {"*": "SKIP:FEED"}
            return ()
        signals = []
        for u in sorted(mv.universe()):
            sig = self._signal(u, day, mv)
            if isinstance(sig, str):
                self.skips[u] = sig
            else:
                signals.append(sig)
        signals.sort(key=lambda s: (-abs(s[2]), s[0]))
        now_mono = mv.now_mono()
        return [self._intent(sig, now, now_mono) for sig in signals]

    def on_feed_reset(self) -> None:
        # Only a reset placed inside the opening bar taints the day (module docstring); one with no timer seen yet is
        #   left to the bar builder, which invalidates the bar for any gap overlapping it.
        w = self._last_wall
        if w is not None and self._bar_start <= ist_tod(w) < self._emit_until:
            self._tainted = True

    def _signal(self, u: str, day: date, mv: MarketView) -> tuple | str:
        p = self.PARAMS
        bar = next((b for b in mv.bars(u, 3)
                    if b.source == "LIVE" and ist_day(b.start) == day and ist_tod(b.start) == self._bar_start), None)
        if bar is None:
            return "SKIP:NO_BAR"
        if not bar.valid:
            return "SKIP:INVALID_BAR"
        o, h, lo, c, v = bar.open, bar.high, bar.low, bar.close, bar.volume
        if not (all(_price(x) for x in (o, h, lo, c)) and lo <= min(o, c) and h >= max(o, c) and _count(v)):
            return "SKIP:BAD_BAR"
        n = p["prev_bars"]
        prev = mv.prev_session_bars(u, n)[-n:]
        if len(prev) < n:
            return "SKIP:NO_HISTORY"
        if not all(b.valid and b.source == "HIST" and ist_day(b.start) < day and _count(b.volume) for b in prev):
            return "SKIP:BAD_HISTORY"
        total = sum(b.volume for b in prev)
        if total <= 0:
            return "SKIP:BAD_HISTORY"
        ret_pct = (c / o - 1.0) * 100.0
        if ret_pct >= p["min_ret_pct"] - EPS:
            direction, retrace = Direction.UP, (h - c) / (h - o)
        elif ret_pct <= -p["min_ret_pct"] + EPS:
            direction, retrace = Direction.DOWN, (c - lo) / (o - lo)
        else:
            return "SKIP:RET"
        if v * n < p["min_vol_mult"] * total:    # volume >= 3 x mean, in exact integer-sized arithmetic
            return "SKIP:VOLUME"
        if retrace > p["retrace_max"] + EPS:
            return "SKIP:RETRACE"
        return (u, direction, ret_pct, bar, v * n / total, retrace)

    def _intent(self, sig: tuple, now: datetime, now_mono: float) -> Intent:
        p = self.PARAMS
        u, direction, ret_pct, bar, vol_mult, retrace = sig
        c = bar.close
        # UP may chase 1.0% up and give back 0.5%; DOWN is the mirror image
        down_pct, up_pct = ((p["recheck_lo_pct"], p["recheck_hi_pct"]) if direction is Direction.UP
                            else (p["recheck_hi_pct"], p["recheck_lo_pct"]))
        return make_intent(
            type(self), self.version, underlying=u, direction=direction, strike_pref=StrikePref(p["strike"]),
            trigger_px=c, recheck=Recheck(c * (1 - down_pct / 100.0), c * (1 + up_pct / 100.0)),
            ttl_s=float(p["ttl_s"]), now_mono=now_mono, now_wall=now,
            reason={"ret_pct": round(ret_pct, 4), "vol_mult": round(vol_mult, 3), "retrace": round(retrace, 4),
                    "open": bar.open, "high": bar.high, "low": bar.low, "close": c, "volume": bar.volume},
        )
