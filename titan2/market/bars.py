"""5-minute bars from underlying ticks, the previous session's HIST bars, and the 15:36 bar check (spec §10.3, §9.7).

Bars (signals thread only; not thread-safe)
- The grid starts at the session open taken from the SessionCalendar and steps 5 minutes. Nothing assumes a close
  time (NSE F&O has closed at 15:40 since 2026-08-03): pass the calendar's close and the last bar ends there.
- A tick belongs to the bar containing its recv_wall (quote-mode ticks carry no exchange time). Ticks outside
  [open, close) build no bar but still update the symbol's volume baseline.
- The session's first bar (the 09:15 bar, opening_drive's input): open = the exchange day open (tick ohlc.open),
  high/low = LTP extremes including that open, close = the last LTP before 09:20:00, volume = the cumulative
  volume_traded at that tick, the pre-open auction's volume included. That this matches Kite's 09:15 candle was a
  [V] note, and the first full bar check failed it (2026-10-14): none of 23 first bars matched Kite's volume within
  0.1% and 10 were more than 5% off, while open and close matched exactly and later bars match; most likely Kite's
  candle leaves the pre-open auction out. The 15:36 check therefore stores both volumes, signed, and the pre-open
  volume (the cumulative volume at the symbol's last tick before the session opened: take_preopen(), sent with the
  first bars' BarsClosed) in bar_checks.detail. Information only: the bars themselves are unchanged.
- Every other bar: open/high/low/close from LTPs; volume = cumulative volume at its last tick minus the cumulative
  volume at the symbol's last tick before the bar.
- A bar is invalid when the feed was down, or ticks were lost (signal_q overflow), at any moment of its window; when
  a tick arrived after the bar closed; when the first bar's day open is missing or changed; or when the volume has
  no baseline or runs backwards. The window is the bar itself for the first bar and [baseline tick, bar end) for
  the others, because their volume delta spans that whole stretch. Until the first on_link(connected=True) the feed
  counts as down, so a bar the bot did not watch from its start is never valid.

Closing
- signal_q has one producer (kite-ws) and is FIFO, so items arrive in receipt order: the first item stamped at or
  after a bar's end closes every bar of that period, exactly.
- close_due(now) closes the current period on a timer, for quiet tapes. The signals thread drains signal_q first
  and calls close_due(now) before the strategies' on_timer(now), so the 09:15 bars are complete and present at the
  first timer at or after 09:20:00.
- A tick that still arrives for a closed bar re-emits that bar with valid=False. A gap learned late does the same.
  These corrections reuse (symbol, start, source), which the store upserts; a strategy must ignore invalid bars.
"""

# Ported from agentic_trader/kite_ticker.py:1507-1515 @b4694f3 — changes: the cumulative-to-delta volume conversion
#   is per bar (last cumulative in the bar minus the symbol's last cumulative before it), not per tick; a missing or
#   backwards baseline invalidates the bar instead of reading as 0

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Iterable

from titan2.clock import IST, SessionCalendar, to_ist
from titan2.model import Bar, UnderlyingTick
# The bar-check tolerances decide G1's bar-mismatch input, so they live in settings.SEMANTIC_CONSTANTS.
from titan2.settings import BAR_CHECK_PRICE_TOL, BAR_CHECK_SAMPLE, BAR_CHECK_VOLUME_TOL

log = logging.getLogger(__name__)

BAR_INTERVAL_S = 300
LIVE = "LIVE"
HIST = "HIST"

# Why a LIVE bar is invalid (Bars.why_invalid)
FEED_GAP = "FEED_GAP"                  # feed down or ticks lost inside the bar's window
LATE_TICK = "LATE_TICK"                # a tick for the bar arrived after it closed
NO_DAY_OPEN = "NO_DAY_OPEN"            # first bar: no tick carried a positive ohlc.open
DAY_OPEN_CHANGED = "DAY_OPEN_CHANGED"  # first bar: ohlc.open changed between two positive values
NO_VOLUME_BASE = "NO_VOLUME_BASE"      # not the first bar, and no earlier tick to take the volume delta from
VOLUME_BACKWARDS = "VOLUME_BACKWARDS"  # the cumulative volume decreased

# Bar check (15:36): each live first bar against Kite's historical candle; the tolerances (BAR_CHECK_*) come from
# titan2.settings. G1 counts the mismatches (§10.7).
_TOL_EPS = 1e-9                        # float slack: a deviation of exactly the tolerance passes

_EPOCH = datetime(1970, 1, 1, tzinfo=IST)


def _positive(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


@dataclass(slots=True)
class _Acc:
    """One bar in progress."""

    symbol: str
    start: datetime
    end: datetime
    first: bool                        # the session's first bar
    open: float
    high: float
    low: float
    close: float
    cum_first: int
    cum_last: int
    base_cum: int | None               # cumulative volume at the symbol's last tick before this bar
    base_wall: datetime | None
    day_open: float | None = None
    day_open_changed: bool = False
    backwards: bool = False

    def add(self, t: UnderlyingTick) -> None:
        self.high = max(self.high, t.ltp)
        self.low = min(self.low, t.ltp)
        self.close = t.ltp
        if t.cum_volume < self.cum_last:
            self.backwards = True
        self.cum_last = t.cum_volume
        self.note_day_open(t.day_open)

    def note_day_open(self, v: float | None) -> None:
        if not _positive(v):
            return
        if self.day_open is not None and abs(v - self.day_open) > 1e-9:
            self.day_open_changed = True
        self.day_open = float(v)


class Bars:
    """Today's LIVE 5-minute bars per underlying, and the previous session's HIST bars (the MarketView source)."""

    def __init__(self, session_open: datetime, session_close: datetime | None = None,
                 interval_s: int = BAR_INTERVAL_S) -> None:
        if isinstance(interval_s, bool) or not isinstance(interval_s, int) or interval_s <= 0:
            raise ValueError(f"interval_s must be a positive int, got {interval_s!r}")
        self.session_open = to_ist(session_open)
        self.session_close = None if session_close is None else to_ist(session_close)
        if self.session_close is not None and self.session_close <= self.session_open:
            raise ValueError(f"session close {self.session_close} is not after open {self.session_open}")
        self._step = timedelta(seconds=interval_s)
        self._cur: tuple[datetime, datetime] | None = None     # the period accepting ticks
        self._done_until = self.session_open                   # every bar before this instant is closed
        self._acc: dict[str, _Acc] = {}
        self._base: dict[str, tuple[int, datetime]] = {}       # symbol -> (cum_volume, recv_wall) of its newest tick
        self._closed: dict[str, list[Bar]] = {}                # today's closed bars, oldest first
        self._span: dict[tuple[str, datetime], tuple[datetime, datetime]] = {}  # closed bar -> validity window
        self._why: dict[tuple[str, datetime], str] = {}
        self._down_since: datetime | None = _EPOCH             # feed down until the first connect
        self._gaps: list[tuple[datetime, datetime]] = []       # closed intervals: feed down or ticks lost
        self._prev: dict[str, tuple[Bar, ...]] = {}
        self._preopen: dict[str, int] = {}                     # first bars closed since the last take_preopen()
        self.late_ticks = 0

    @classmethod
    def for_session(cls, calendar: SessionCalendar, day: date) -> Bars | None:
        """Bars on the calendar's hours for day, or None when day is not a trading session."""
        bounds = calendar.session_bounds(day)
        return None if bounds is None else cls(bounds[0], bounds[1])

    # -- input from the signals thread ----------------------------------------------------------------------
    def on_tick(self, t: UnderlyingTick) -> list[Bar]:
        """Add one tick. Returns the bars it closed (and a correction if it came late)."""
        cum = t.cum_volume
        if not _positive(t.ltp) or isinstance(cum, bool) or not isinstance(cum, int) or cum < 0:
            log.error("bars: malformed tick ignored: %r", t)
            return []
        w = to_ist(t.recv_wall)
        out = self._advance(w)
        prev = self._base.get(t.symbol)
        if prev is None or w >= prev[1]:
            self._base[t.symbol] = (t.cum_volume, w)
        period = self._period_of(w)
        if period is None:
            return out
        if w < self._done_until:
            self.late_ticks += 1
            log.warning("bars: late tick %s @ %s for a closed bar; marking it invalid", t.symbol, w.isoformat())
            return out + self._invalidate_bar(t.symbol, period[0], LATE_TICK)
        if self._cur is None:
            self._cur = period
            self._done_until = max(self._done_until, period[0])
        acc = self._acc.get(t.symbol)
        if acc is None:
            acc = _Acc(t.symbol, period[0], period[1], period[0] == self.session_open, t.ltp, t.ltp, t.ltp, t.ltp,
                       t.cum_volume, t.cum_volume, None if prev is None else prev[0],
                       None if prev is None else prev[1])
            acc.note_day_open(t.day_open)
            self._acc[t.symbol] = acc
        else:
            acc.add(t)
        return out

    def on_link(self, connected: bool, wall: datetime) -> list[Bar]:
        """The feed connected or disconnected at wall (a FeedLink from signal_q). Returns closed bars/corrections."""
        w = to_ist(wall)
        out = self._advance(w)
        if connected:
            if self._down_since is not None:
                self._gaps.append((self._down_since, w))
                self._down_since = None
        elif self._down_since is None:
            self._down_since = w
            out += self._invalidate_overlapping(w, None)
        return out

    def on_gap(self, first_wall: datetime, last_wall: datetime) -> list[Bar]:
        """Ticks received in [first_wall, last_wall] were lost (signal_q overflow). Returns corrections."""
        a, b = sorted((to_ist(first_wall), to_ist(last_wall)))
        self._gaps.append((a, b))
        return self._invalidate_overlapping(a, b)

    def close_due(self, now_wall: datetime) -> list[Bar]:
        """Timer: close the current period once now_wall reaches its end. Drain signal_q before calling this."""
        return self._advance(to_ist(now_wall))

    # -- queries (MarketView) -------------------------------------------------------------------------------
    def bars(self, symbol: str, n: int) -> tuple[Bar, ...]:
        """The last n closed LIVE bars of today, oldest first (invalid ones included: check Bar.valid)."""
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            return ()
        return tuple(self._closed.get(symbol, ())[-n:])

    def first_bar(self, symbol: str) -> Bar | None:
        """The closed first bar of the session (09:15) for symbol, or None."""
        for bar in self._closed.get(symbol, ()):
            if bar.start == self.session_open:
                return bar
        return None

    def why_invalid(self, symbol: str, start: datetime) -> str | None:
        return self._why.get((symbol, to_ist(start)))

    def take_preopen(self) -> tuple[tuple[str, int], ...]:
        """(symbol, pre-open volume) of the first bars closed since the last call, sorted: the cumulative volume at the
        symbol's last tick before the session opened (the pre-open auction's), which the first bar's volume includes.
        Only symbols that had such a tick; the host sends them with the first bars' BarsClosed (bar check detail)."""
        out, self._preopen = tuple(sorted(self._preopen.items())), {}
        return out

    def set_prev_session(self, symbol: str, bars: Iterable[Bar]) -> None:
        """The previous session's HIST bars for symbol (from FetchHistorical at prep), any order."""
        rows = list(bars)
        for b in rows:
            if not isinstance(b, Bar) or b.source != HIST or b.symbol != symbol:
                raise ValueError(f"prev-session bars for {symbol} must be HIST bars of that symbol, got {b!r}")
        self._prev[symbol] = tuple(sorted(rows, key=lambda b: b.start))

    def prev_session_bars(self, symbol: str, n: int) -> tuple[Bar, ...]:
        """The previous session's last n HIST bars, oldest first (fewer if fewer exist)."""
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            return ()
        return self._prev.get(symbol, ())[-n:]

    # -- internals ----------------------------------------------------------------------------------------------
    def _period_of(self, w: datetime) -> tuple[datetime, datetime] | None:
        if w < self.session_open or (self.session_close is not None and w >= self.session_close):
            return None
        start = self.session_open + ((w - self.session_open) // self._step) * self._step
        end = start + self._step
        if self.session_close is not None and end > self.session_close:
            end = self.session_close
        return start, end

    def _advance(self, w: datetime) -> list[Bar]:
        """Move the frontier to w: close the current period if w is at or after its end."""
        if self._cur is None or w < self._cur[1]:
            return []
        out = [self._finish(self._acc[s]) for s in sorted(self._acc)]
        self._acc.clear()
        self._done_until = max(self._done_until, self._cur[1])
        self._cur = None
        return out

    def _finish(self, acc: _Acc) -> Bar:
        why = None
        if acc.first:
            o = acc.day_open if acc.day_open is not None else acc.open
            volume = acc.cum_last
            w0 = acc.start
            if acc.base_cum is not None:              # the pre-open auction's volume, which `volume` includes
                self._preopen[acc.symbol] = acc.base_cum
            if acc.day_open is None:
                why = NO_DAY_OPEN
            elif acc.day_open_changed:
                why = DAY_OPEN_CHANGED
        else:
            o = acc.open
            w0 = acc.start if acc.base_wall is None else min(acc.start, acc.base_wall)
            if acc.base_cum is None:
                why, volume = NO_VOLUME_BASE, max(0, acc.cum_last - acc.cum_first)
            elif acc.cum_last < acc.base_cum:
                why, volume = VOLUME_BACKWARDS, 0
            else:
                volume = acc.cum_last - acc.base_cum
        if acc.backwards and why is None:
            why = VOLUME_BACKWARDS
        if self._gap_overlaps(w0, acc.end):
            why = FEED_GAP
        bar = Bar(acc.symbol, acc.start, o, max(acc.high, o), min(acc.low, o), acc.close, volume, why is None, LIVE)
        self._closed.setdefault(acc.symbol, []).append(bar)
        self._span[(acc.symbol, acc.start)] = (w0, acc.end)
        if why is not None:
            self._why[(acc.symbol, acc.start)] = why
        return bar

    def _gap_overlaps(self, w0: datetime, w1: datetime) -> bool:
        if self._down_since is not None and self._down_since < w1:
            return True
        return any(a < w1 and b >= w0 for a, b in self._gaps)

    def _mark_invalid(self, symbol: str, i: int, why: str) -> Bar:
        bars = self._closed[symbol]
        bar = replace(bars[i], valid=False)
        bars[i] = bar
        self._why[(symbol, bar.start)] = why
        return bar

    def _invalidate_bar(self, symbol: str, start: datetime, why: str) -> list[Bar]:
        bars = self._closed.get(symbol, [])
        for i in range(len(bars) - 1, -1, -1):
            if bars[i].start == start:
                return [self._mark_invalid(symbol, i, why)] if bars[i].valid else []
            if bars[i].start < start:
                break
        return []                       # the symbol had no bar in that period: the late tick is only counted

    def _invalidate_overlapping(self, a: datetime, b: datetime | None) -> list[Bar]:
        """Re-emit as invalid every valid closed bar whose window overlaps [a, b] (b None: open-ended)."""
        out = []
        for symbol in sorted(self._closed):
            bars = self._closed[symbol]
            for i in range(len(bars) - 1, -1, -1):
                w0, w1 = self._span[(symbol, bars[i].start)]
                if w1 <= a:
                    break               # earlier bars end earlier still
                if bars[i].valid and (b is None or b >= w0):
                    out.append(self._mark_invalid(symbol, i, FEED_GAP))
        return out


def prev_session_window(calendar: SessionCalendar, day: date) -> tuple[datetime, datetime] | None:
    """(open, close) of the trading session before day, for the PREV_BARS FetchHistorical; None if unknown."""
    prev = calendar.prev_trading_day(day)
    return None if prev is None else calendar.session_bounds(prev)


def find_bar(bars: Iterable[Bar], start: datetime) -> Bar | None:
    """The bar starting at start (e.g. the 09:15 HIST candle in a HistoricalResult), or None."""
    start = to_ist(start)
    for b in bars:
        if b.start == start:
            return b
    return None


# ---------------------------------------------------------------------------------------------------------------
# Bar check (15:36)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BarCheck:
    symbol: str
    start: datetime | None
    ok: bool
    reasons: tuple[str, ...]           # empty when ok
    open_dev: float | None             # |live/hist - 1|
    close_dev: float | None
    volume_dev: float | None
    # information only (2026-10-14 night): the two volumes as they are, so the gap's sign is kept, and the pre-open
    #   volume (Bars.take_preopen) the live first bar includes. The check itself still judges volume_dev alone
    live_volume: int | None = None
    hist_volume: int | None = None
    preopen_volume: int | None = None

    def detail(self) -> str:
        """bar_checks.detail: JSON with the reasons and deviations (non-finite values as null), plus the signed
        volumes: live_volume, kite_volume, volume_gap (live - Kite), volume_rel (live/Kite - 1, signed),
        preopen_volume and volume_rel_ex_preopen ((live - pre-open)/Kite - 1: near 0 if Kite's candle leaves the
        pre-open auction out)."""
        def num(x: float | None) -> float | None:
            return round(x, 6) if x is not None and math.isfinite(x) else None
        live, kite, pre = self.live_volume, self.hist_volume, self.preopen_volume
        both = live is not None and kite is not None
        return json.dumps({"reasons": list(self.reasons), "open_dev": num(self.open_dev),
                           "close_dev": num(self.close_dev), "volume_dev": num(self.volume_dev),
                           "live_volume": live, "kite_volume": kite, "preopen_volume": pre,
                           "volume_gap": live - kite if both else None,
                           "volume_rel": num(live / kite - 1.0) if both and kite > 0 else None,
                           "volume_rel_ex_preopen": (num((live - pre) / kite - 1.0)
                                                     if both and kite > 0 and pre is not None else None)},
                          sort_keys=True)


def _rel_dev(live: float, hist: float) -> float | None:
    if not (_positive(live) and _positive(hist)):
        return None
    return abs(live / hist - 1.0)


def check_bar(live: Bar | None, hist: Bar | None, preopen_volume: int | None = None) -> BarCheck:
    """Compare a live first bar with Kite's historical candle: open and close within 0.1 %, volume within 5 %.
    preopen_volume (the symbol's cumulative volume before the open, when known) is only recorded in the detail."""
    ref = live if live is not None else hist
    symbol = ref.symbol if ref is not None else ""
    start = ref.start if ref is not None else None
    vols = {"live_volume": live.volume if live is not None else None,
            "hist_volume": hist.volume if hist is not None else None,
            "preopen_volume": preopen_volume if isinstance(preopen_volume, int) and not isinstance(preopen_volume, bool)
            else None}
    reasons = []
    if live is None:
        reasons.append("LIVE_MISSING")
    elif not live.valid:
        reasons.append("LIVE_INVALID")
    if hist is None:
        reasons.append("HIST_MISSING")
    elif not hist.valid:
        reasons.append("HIST_INVALID")
    if live is None or hist is None:
        return BarCheck(symbol, start, False, tuple(reasons), None, None, None, **vols)
    if live.symbol != hist.symbol or live.start != hist.start:
        reasons.append("NOT_THE_SAME_BAR")
    open_dev, close_dev = _rel_dev(live.open, hist.open), _rel_dev(live.close, hist.close)
    if hist.volume == 0:
        volume_dev = 0.0 if live.volume == 0 else math.inf
    else:
        volume_dev = abs(live.volume / hist.volume - 1.0) if hist.volume > 0 and live.volume >= 0 else None
    for name, dev, tol in (("OPEN", open_dev, BAR_CHECK_PRICE_TOL), ("CLOSE", close_dev, BAR_CHECK_PRICE_TOL),
                           ("VOLUME", volume_dev, BAR_CHECK_VOLUME_TOL)):
        if dev is None or dev > tol + _TOL_EPS:
            reasons.append(name)
    return BarCheck(symbol, start, not reasons, tuple(reasons), open_dev, close_dev, volume_dev, **vols)


def bar_check_symbols(fired: Iterable[str], universe: Iterable[str], session: date,
                      n: int = BAR_CHECK_SAMPLE) -> tuple[str, ...]:
    """Every symbol that fired (sorted), then a seeded sample of n other universe symbols (sorted)."""
    fired_set = sorted(set(fired))
    pool = sorted(set(universe) - set(fired_set))
    seed = int.from_bytes(hashlib.sha256(f"barcheck|{session.isoformat()}".encode()).digest()[:8], "big")
    sample = random.Random(seed).sample(pool, min(max(n, 0), len(pool)))
    return tuple(fired_set) + tuple(sorted(sample))
