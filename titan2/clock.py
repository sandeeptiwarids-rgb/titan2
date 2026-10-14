"""Clocks, IST helpers and the NSE session calendar (spec §11 Sessions/DTE, DETAIL §11.4 and §11.7).

Wall time is an IST-aware datetime. Ages and timeouts use mono() (time.monotonic), which is never persisted.

RealClock's wall time is the system clock, plus CORRECTION once it holds a measured offset to internet time: the
laptop measures it at start and every 5 minutes (titan2.laptop), because a laptop without admin rights cannot fix
a drifting Windows clock (2026-10-13: 1.43 s behind, Windows Time stopped). Corrected time never moves backwards.

Calendar rule: a date is a trading session when it is a weekday that is not a holiday, unless special_sessions
lists it, in which case that entry's "trade" flag decides (Budget weekends and Muhurat sessions are trade: false).
Dates outside the listed years are never trading days (fails closed). Session hours come from the data file's
regular_hours table, never from a hardcoded close (NSE moved the F&O close from 15:30 to 15:40 on 2026-08-03).
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:  # Windows without the tzdata package: India has used a fixed +05:30, no DST, since 1945
    IST = timezone(timedelta(hours=5, minutes=30), "IST")

SESSIONS_FILE = Path(__file__).resolve().parent / "data" / "nse_sessions.json"

# Options have no pre-open, so option quotes stamped in the first seconds after the open are stale carry-overs:
# no strategy enters before 09:20 and the paper exchange never fills against a quote stamped before open + 30 s.
OPENING_QUOTE_GRACE_S = 30.0

# Used only when a calendar dict has no "regular_hours" (e.g. small test calendars): today's NSE F&O hours.
DEFAULT_REGULAR_HOURS: tuple[tuple[date, dtime, dtime], ...] = ((date.min, dtime(9, 15), dtime(15, 40)),)


# ---------------------------------------------------------------------------------------------------------------
# IST helpers
# ---------------------------------------------------------------------------------------------------------------
def to_ist(dt: datetime) -> datetime:
    """The same instant in IST. A naive datetime is refused: every wall time in titan2 is aware."""
    if not isinstance(dt, datetime):
        raise TypeError(f"expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime {dt!r}: wall times must be timezone-aware")
    return dt.astimezone(IST)


def parse_hms(value: str | dtime) -> dtime:
    """'HH:MM', 'HH:MM:SS' or 'HH:MM:SS.fff' -> a naive time of day (IST implied)."""
    t = value if isinstance(value, dtime) else dtime.fromisoformat(value)
    if t.tzinfo is not None:
        raise ValueError(f"time of day must not carry an offset: {value!r}")
    return t


def ist_datetime(d: date, t: dtime | str) -> datetime:
    """IST-aware datetime for a session date and a time of day."""
    return datetime.combine(_as_date(d), parse_hms(t), tzinfo=IST)


def parse_wall(text: str, assume_ist: bool = False) -> datetime:
    """ISO-8601 text -> IST-aware datetime. Accepts a trailing 'Z' (Python 3.10's fromisoformat does not).

    Naive text is refused unless assume_ist (e.g. Kite's 'YYYY-MM-DD HH:MM:SS' timestamps, which are IST).
    """
    s = text.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        if not assume_ist:
            raise ValueError(f"naive timestamp {text!r}")
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def iso(dt: datetime, timespec: str = "milliseconds") -> str:
    """Persisted form: ISO-8601 in IST with offset, e.g. 2026-10-12T09:20:01.412+05:30."""
    return to_ist(dt).isoformat(timespec=timespec)


def session_of(dt: datetime) -> date:
    """The IST calendar date (session date) of an aware datetime."""
    return to_ist(dt).date()


def _as_date(x: date) -> date:
    if isinstance(x, datetime):
        return session_of(x)
    if isinstance(x, date):
        return x
    raise TypeError(f"expected date, got {type(x).__name__}")


# ---------------------------------------------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------------------------------------------
class Clock(ABC):
    @abstractmethod
    def mono(self) -> float:
        """Monotonic seconds for ages and timeouts. Never persisted."""

    @abstractmethod
    def now_wall(self) -> datetime:
        """IST-aware wall time."""

    def today(self) -> date:
        return self.now_wall().date()


class WallCorrection:
    """The process-wide correction of the system clock to internet time (CORRECTION; set by the laptop profile).

    Unset: RealClock reads the system clock, exactly as without it. Set (set(offset_s), offset_s = the seconds to add
    to the system clock to get internet time, as laptop.measure_clock reads it): RealClock reads the system clock
    plus offset_s, and never goes backwards: a new offset that would set the clock back holds it still until the
    system clock catches up (as long as the change; milliseconds between two readings). The system clock keeps the
    time between readings (sleep included). A step of the system clock (Windows Time wakes up and syncs: on this
    laptop +1.1 to +2.5 s around midday on most days) is absorbed at once: the system clock minus the monotonic
    clock (which nothing sets) moving by more than STEP_S is a step, so offset_s moves by minus that and titan2's
    time goes on unbroken; step_pending then asks the laptop for a fresh reading. Thread-safe."""

    STEP_S = 0.25                                  # both clocks tick in 15.6 ms on Windows: far above their jitter

    def __init__(self, system: Any = time.time, mono: Any = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._system, self._mono = system, mono
        self._offset: float | None = None
        self._skew = 0.0                           # system - monotonic when the offset was last set or adjusted
        self._last = -math.inf
        self.error_s: float | None = None          # how far the reading behind the offset can be off
        self.source = ""
        self.set_at: float | None = None           # system time of the last set()
        self.steps = 0                             # steps of the system clock absorbed
        self.last_step_s: float | None = None
        self.step_pending = False                  # a step was absorbed since the last reading: measure again soon

    def set(self, offset_s: float, error_s: float = 0.0, source: str = "") -> None:
        for name, x in (("offset_s", offset_s), ("error_s", error_s)):
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
                raise ValueError(f"WallCorrection: {name} must be a finite number of seconds, got {x!r}")
        if error_s < 0:
            raise ValueError(f"WallCorrection: error_s must be >= 0, got {error_s!r}")
        with self._lock:
            now = self._system()
            if self._offset is None:
                self._last = max(self._last, now)  # never behind what the plain system clock already handed out
            self._offset, self.error_s, self.source, self.set_at = float(offset_s), float(error_s), source, now
            self._skew, self.step_pending = now - self._mono(), False

    def clear(self) -> None:
        with self._lock:
            self._offset, self.error_s, self.source, self.set_at = None, None, "", None
            self._last, self.step_pending = -math.inf, False

    @property
    def offset_s(self) -> float | None:
        return self._offset

    def epoch(self) -> float | None:
        """Corrected POSIX time now, or None while unset (callers then read the system clock themselves)."""
        with self._lock:
            if self._offset is None:
                return None
            now = self._system()
            skew = now - self._mono()
            step = skew - self._skew
            if abs(step) > self.STEP_S:            # the system clock was set: titan2's time goes on unbroken
                self._offset -= step
                self._skew, self.steps, self.last_step_s, self.step_pending = skew, self.steps + 1, step, True
            t = now + self._offset
            if t < self._last:
                return self._last
            self._last = t
            return t


CORRECTION = WallCorrection()


class RealClock(Clock):
    def mono(self) -> float:
        return time.monotonic()

    def now_wall(self) -> datetime:
        t = CORRECTION.epoch()
        return datetime.now(IST) if t is None else datetime.fromtimestamp(t, IST)


CLOCK_OFFSET_ENV = "TITAN2_CLOCK_OFFSET_S"     # a corrected laptop session hands its offset to the dashboard


def apply_env_offset(env: Mapping[str, str]) -> bool:
    """In a child process of a corrected laptop session (the dashboard): take the session's offset from
    TITAN2_CLOCK_OFFSET_S. False when it is absent or not a finite number (the system clock then stays)."""
    raw = str(env.get(CLOCK_OFFSET_ENV, "") or "").strip()
    try:
        x = float(raw) if raw else math.nan
    except ValueError:
        return False
    if not math.isfinite(x):
        return False
    CORRECTION.set(x, source="the laptop session")
    return True


_log_factory_installed = False


def install_log_correction() -> None:
    """Stamp log records with corrected time too, so the log file and titan2.db agree (idempotent)."""
    global _log_factory_installed
    if _log_factory_installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base(*args, **kwargs)
        t = CORRECTION.epoch()
        if t is not None:
            record.created = t
            record.msecs = (t - int(t)) * 1000.0
        return record

    logging.setLogRecordFactory(factory)
    _log_factory_installed = True


class ShiftedClock(Clock):
    """Another clock moved by whole days on the wall only (`titan2 laptop --date`: simulation and tests). mono is the
    base clock's, so ages and timeouts are untouched."""

    def __init__(self, base: Clock, days: int) -> None:
        if isinstance(days, bool) or not isinstance(days, int):
            raise TypeError(f"days must be a whole number, got {days!r}")
        self.base = base
        self.days = days

    def mono(self) -> float:
        return self.base.mono()

    def now_wall(self) -> datetime:
        return self.base.now_wall() + timedelta(days=self.days)


class FakeClock(Clock):
    """Deterministic test clock. advance() moves wall and mono together; jump_wall() moves wall only (I15).

    Both are kept in whole microseconds so thousands of 50 ms steps never drift apart. mono starts at 1000.0
    rather than 0 so code that treats 0 as "unset" is caught. A naive start/target is taken as IST.
    """

    def __init__(self, start: datetime | str = "2026-10-12T09:00:00+05:30", mono_start: float = 1000.0) -> None:
        if isinstance(start, str):
            wall = parse_wall(start, assume_ist=True)
        elif isinstance(start, datetime):
            wall = start.replace(tzinfo=IST) if start.tzinfo is None else to_ist(start)
        else:
            raise TypeError(f"start must be a datetime or ISO text, got {type(start).__name__}")
        self._wall = wall
        self._mono_us = round(mono_start * 1_000_000)
        self._lock = threading.Lock()

    def mono(self) -> float:
        with self._lock:
            return self._mono_us / 1_000_000

    def now_wall(self) -> datetime:
        with self._lock:
            return self._wall

    def advance(self, seconds: float) -> None:
        if not seconds >= 0:
            raise ValueError(f"monotonic time cannot go backwards: advance({seconds!r})")
        us = round(seconds * 1_000_000)
        with self._lock:
            self._mono_us += us
            self._wall += timedelta(microseconds=us)

    def advance_to(self, target: datetime | str) -> None:
        """Advance to a wall time: a datetime, or 'HH:MM[:SS[.fff]]' on the current date."""
        now = self.now_wall()
        if isinstance(target, str):
            when = ist_datetime(now.date(), target)
        else:
            when = target.replace(tzinfo=IST) if target.tzinfo is None else to_ist(target)
        delta = (when - now).total_seconds()
        if delta < 0:
            raise ValueError(f"advance_to({target!r}) is before now ({now.isoformat()})")
        self.advance(delta)

    def jump_wall(self, seconds: float) -> None:
        """Step the wall clock without moving mono (an NTP step); may be negative."""
        with self._lock:
            self._wall += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------------------------------------------
# Session calendar
# ---------------------------------------------------------------------------------------------------------------
class CalendarError(ValueError):
    """The sessions file is missing or malformed. Callers fail closed (SessionCalendar.empty(): no trading day)."""


@dataclass(frozen=True, slots=True)
class SpecialSession:
    day: date
    open: dtime
    close: dtime
    trade: bool                      # False: the exchange is open but titan2 does not trade (Budget weekend, Muhurat)


def _cal_date(value: Any, where: str) -> date:
    if not isinstance(value, str):
        raise CalendarError(f"{where}: date must be 'YYYY-MM-DD' text, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CalendarError(f"{where}: bad date {value!r}") from None


def _cal_time(value: Any, where: str) -> dtime:
    if not isinstance(value, str):
        raise CalendarError(f"{where}: time must be 'HH:MM' text, got {value!r}")
    try:
        return parse_hms(value)
    except ValueError:
        raise CalendarError(f"{where}: bad time {value!r}") from None


def _cal_hours(entry: Mapping[str, Any], where: str) -> tuple[dtime, dtime]:
    opens, closes = _cal_time(entry.get("open"), f"{where}.open"), _cal_time(entry.get("close"), f"{where}.close")
    if not opens < closes:
        raise CalendarError(f"{where}: open {opens} is not before close {closes}")
    return opens, closes


class SessionCalendar:
    """Trading sessions from data/nse_sessions.json. Immutable, so any thread may read it."""

    def __init__(self, holidays: frozenset[date], special: Mapping[date, SpecialSession],
                 regular_hours: tuple[tuple[date, dtime, dtime], ...], coverage: tuple[date, date] | None) -> None:
        if not regular_hours:
            raise CalendarError("regular_hours is empty")
        self._holidays = frozenset(holidays)
        self._special = dict(special)
        self._hours = tuple(sorted(regular_hours))
        self._coverage = coverage
        days: list[date] = []
        if coverage is not None:
            d, last = coverage
            while d <= last:
                if self._trades_on(d):
                    days.append(d)
                d += timedelta(days=1)
        self._days = days                      # sorted trading days inside coverage
        self._day_set = frozenset(days)

    # -- construction ---------------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> SessionCalendar:
        p = Path(path) if path is not None else SESSIONS_FILE
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CalendarError(f"cannot read sessions file {p}: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def empty(cls) -> SessionCalendar:
        """No coverage: every date is a non-trading day. The fail-closed fallback when the file cannot load."""
        return cls(frozenset(), {}, DEFAULT_REGULAR_HOURS, None)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionCalendar:
        """{"years": {"YYYY": {"holidays": [...], "special_sessions": [{date, open, close, trade}]}},
            "regular_hours": [{"from", "open", "close"}]}  -- unknown keys (source, notes) are ignored."""
        if not isinstance(data, Mapping) or not isinstance(data.get("years"), Mapping) or not data["years"]:
            raise CalendarError("sessions data needs a non-empty 'years' object")
        holidays: set[date] = set()
        special: dict[date, SpecialSession] = {}
        years: list[int] = []
        for key, entry in data["years"].items():
            if not (isinstance(key, str) and len(key) == 4 and key.isascii() and key.isdigit()):
                raise CalendarError(f"bad year key {key!r}")
            year = int(key)
            years.append(year)
            if not isinstance(entry, Mapping) or not isinstance(entry.get("holidays"), list):
                raise CalendarError(f"{key}: needs a 'holidays' list")
            year_holidays = set()
            for i, value in enumerate(entry["holidays"]):
                d = _cal_date(value, f"{key}.holidays[{i}]")
                if d.year != year:
                    raise CalendarError(f"{key}.holidays[{i}]: {d} is not in {year}")
                year_holidays.add(d)
            names = entry.get("holiday_names", {})
            if not isinstance(names, Mapping):
                raise CalendarError(f"{key}.holiday_names must be an object")
            for value in names:
                if _cal_date(value, f"{key}.holiday_names") not in year_holidays:
                    raise CalendarError(f"{key}.holiday_names: {value} is named but not listed in holidays")
            holidays |= year_holidays
            sessions = entry.get("special_sessions", [])
            if not isinstance(sessions, list):
                raise CalendarError(f"{key}.special_sessions must be a list")
            for i, s in enumerate(sessions):
                where = f"{key}.special_sessions[{i}]"
                if not isinstance(s, Mapping):
                    raise CalendarError(f"{where} must be an object")
                d = _cal_date(s.get("date"), f"{where}.date")
                if d.year != year or d in special:
                    raise CalendarError(f"{where}: {d} is outside {year} or listed twice")
                trade = s.get("trade", False)
                if not isinstance(trade, bool):
                    raise CalendarError(f"{where}.trade must be true or false")
                special[d] = SpecialSession(d, *_cal_hours(s, where), trade)
        years.sort()
        if years != list(range(years[0], years[-1] + 1)):
            raise CalendarError(f"years must be contiguous, got {years}")
        coverage = (date(years[0], 1, 1), date(years[-1], 12, 31))

        hours: tuple[tuple[date, dtime, dtime], ...] = DEFAULT_REGULAR_HOURS
        if "regular_hours" in data:
            rows = data["regular_hours"]
            if not isinstance(rows, list) or not rows:
                raise CalendarError("regular_hours must be a non-empty list")
            parsed = []
            for i, row in enumerate(rows):
                where = f"regular_hours[{i}]"
                if not isinstance(row, Mapping):
                    raise CalendarError(f"{where} must be an object")
                parsed.append((_cal_date(row.get("from"), f"{where}.from"), *_cal_hours(row, where)))
            starts = [r[0] for r in parsed]
            if starts != sorted(set(starts)):
                raise CalendarError("regular_hours 'from' dates must be strictly increasing")
            if starts[0] > coverage[0]:
                raise CalendarError(f"regular_hours start {starts[0]} after coverage start {coverage[0]}")
            hours = tuple(parsed)
        return cls(frozenset(holidays), special, hours, coverage)

    # -- queries --------------------------------------------------------------------------------------------
    def _trades_on(self, d: date) -> bool:
        s = self._special.get(d)
        if s is not None:
            return s.trade
        return d.weekday() < 5 and d not in self._holidays

    @property
    def coverage_start(self) -> date | None:
        return self._coverage[0] if self._coverage else None

    @property
    def coverage_end(self) -> date | None:
        return self._coverage[1] if self._coverage else None

    def covers(self, d: date) -> bool:
        d = _as_date(d)
        return self._coverage is not None and self._coverage[0] <= d <= self._coverage[1]

    def covers_ahead(self, today: date, days: int = 30) -> bool:
        """True when today and the next `days` days are all covered (the self-check WARNs otherwise)."""
        today = _as_date(today)
        return self.covers(today) and self.covers(today + timedelta(days=days))

    def is_trading_day(self, d: date) -> bool:
        return _as_date(d) in self._day_set

    def special_session(self, d: date) -> SpecialSession | None:
        return self._special.get(_as_date(d))

    def session_hours(self, d: date) -> tuple[dtime, dtime] | None:
        """(open, close) for a trading session, else None."""
        d = _as_date(d)
        if d not in self._day_set:
            return None
        s = self._special.get(d)
        if s is not None:
            return s.open, s.close
        i = bisect.bisect_right([h[0] for h in self._hours], d) - 1
        _, opens, closes = self._hours[max(i, 0)]
        return opens, closes

    def session_bounds(self, d: date) -> tuple[datetime, datetime] | None:
        """IST-aware (open, close) datetimes for a trading session, else None."""
        hours = self.session_hours(d)
        if hours is None:
            return None
        d = _as_date(d)
        return ist_datetime(d, hours[0]), ist_datetime(d, hours[1])

    def quotes_valid_from(self, d: date) -> datetime | None:
        """Earliest receipt time at which an option quote of session d may be trusted (open + 30 s)."""
        bounds = self.session_bounds(d)
        return None if bounds is None else bounds[0] + timedelta(seconds=OPENING_QUOTE_GRACE_S)

    def td(self, a: date, b: date) -> int:
        """Number of trading sessions d with a < d <= b (0 when b <= a). Days beyond coverage count as none."""
        a, b = _as_date(a), _as_date(b)
        if b <= a:
            return 0
        return bisect.bisect_right(self._days, b) - bisect.bisect_right(self._days, a)

    def next_trading_day(self, d: date) -> date | None:
        """First trading session after d, or None beyond coverage."""
        i = bisect.bisect_right(self._days, _as_date(d))
        return self._days[i] if i < len(self._days) else None

    def prev_trading_day(self, d: date) -> date | None:
        """Last trading session before d, or None before coverage."""
        i = bisect.bisect_left(self._days, _as_date(d))
        return self._days[i - 1] if i > 0 else None

    def trading_days(self, first: date, last: date) -> list[date]:
        """Trading sessions in [first, last], both ends inclusive."""
        lo = bisect.bisect_left(self._days, _as_date(first))
        hi = bisect.bisect_right(self._days, _as_date(last))
        return self._days[lo:hi]

    def is_last_session_of_week(self, d: date) -> bool:
        """True for the week's final trading session (weekly prune). Also true when the next one is unknown."""
        d = _as_date(d)
        if d not in self._day_set:
            return False
        nxt = self.next_trading_day(d)
        return nxt is None or nxt.isocalendar()[:2] != d.isocalendar()[:2]

    def __repr__(self) -> str:
        cov = f"{self._coverage[0]}..{self._coverage[1]}" if self._coverage else "none"
        return f"SessionCalendar(coverage={cov}, sessions={len(self._days)}, holidays={len(self._holidays)})"


def td(cal: SessionCalendar, a: date, b: date) -> int:
    """Trading sessions in (a, b] per cal (DETAIL §11.4)."""
    return cal.td(a, b)
