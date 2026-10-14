"""U01: titan2.clock (clocks, IST helpers, SessionCalendar and td)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone

import pytest

from titan2 import clock as ck
from titan2.clock import CalendarError, FakeClock, SessionCalendar

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def nse() -> SessionCalendar:
    return SessionCalendar.load()


def small_calendar() -> SessionCalendar:
    return SessionCalendar.from_dict({"years": {"2026": {
        "holidays": ["2026-10-20"],
        "special_sessions": [
            {"date": "2026-10-17", "open": "09:15", "close": "13:00", "trade": True},    # Saturday, traded
            {"date": "2026-10-18", "open": "18:00", "close": "19:00", "trade": False},   # Sunday, not traded
            {"date": "2026-10-22", "open": "09:15", "close": "12:00"},                   # Thursday, trade defaults false
        ],
    }}})


def test_calendar_td_holidays(nse):
    cal = small_calendar()
    d = date
    # (a, b]: weekends, holidays and trade:false sessions do not count; a trade:true Saturday does
    assert cal.td(d(2026, 10, 16), d(2026, 10, 20)) == 2           # Sat 17 (special) + Mon 19; Tue 20 holiday
    assert cal.td(d(2026, 10, 16), d(2026, 10, 21)) == 3
    assert cal.td(d(2026, 10, 21), d(2026, 10, 23)) == 1           # Thu 22 is a trade:false session
    assert cal.td(d(2026, 10, 19), d(2026, 10, 19)) == 0
    assert cal.td(d(2026, 10, 23), d(2026, 10, 19)) == 0           # reversed interval is empty
    assert cal.td(d(2026, 10, 19), d(2026, 10, 20)) == 0           # b itself a holiday
    assert cal.td(d(2026, 10, 18), d(2026, 10, 19)) == 1           # a itself a non-session
    assert cal.td(datetime(2026, 10, 16, 15, 0, tzinfo=IST), d(2026, 10, 19)) == 2
    assert ck.td(cal, d(2026, 10, 16), d(2026, 10, 20)) == 2
    assert cal.td(d(2026, 12, 30), d(2027, 1, 5)) == 1             # only Dec 31; beyond coverage counts as none
    assert cal.is_trading_day(d(2026, 10, 17)) and not cal.is_trading_day(d(2026, 10, 18))
    assert not cal.is_trading_day(d(2026, 10, 20)) and not cal.is_trading_day(d(2026, 10, 22))
    assert cal.session_hours(d(2026, 10, 17)) == (time(9, 15), time(13, 0))
    with pytest.raises(TypeError):
        cal.td("2026-10-16", d(2026, 10, 20))

    # the shipped NSE calendar
    assert nse.td(d(2026, 10, 16), d(2026, 10, 20)) == 1           # Tue 20 Dussehra
    assert nse.td(d(2026, 1, 14), d(2026, 1, 16)) == 1             # Thu 15 Maharashtra civic polls (FAOP/72262)
    assert nse.td(d(2026, 1, 30), d(2026, 2, 2)) == 1              # Sun 1 Feb Budget session is not traded
    assert nse.td(d(2026, 11, 6), d(2026, 11, 13)) == 4            # Tue 10 Diwali-Balipratipada
    assert nse.td(d(2025, 12, 24), d(2026, 1, 2)) == 6             # across the year; Thu 25 Dec Christmas
    # index rule (MIN_TD 2): a Tuesday weekly is still eligible on Friday, rolls on Monday
    assert nse.td(d(2026, 10, 23), d(2026, 10, 27)) == 2
    assert nse.td(d(2026, 10, 26), d(2026, 10, 27)) == 1
    # a Tuesday holiday moves the weekly to Monday: it then rolls on the Friday before
    assert nse.td(d(2026, 11, 6), d(2026, 11, 9)) == 1
    # stock rule (MIN_TD 5) for the 27 Oct monthly: eligible on Mon 19, rolls by Wed 21
    assert nse.td(d(2026, 10, 19), d(2026, 10, 27)) == 5
    assert nse.td(d(2026, 10, 21), d(2026, 10, 27)) == 4


def test_shipped_calendar_matches_nse_circulars(nse):
    raw = json.loads(ck.SESSIONS_FILE.read_text(encoding="utf-8"))
    hol26 = [date.fromisoformat(x) for x in raw["years"]["2026"]["holidays"]]
    hol25 = [date.fromisoformat(x) for x in raw["years"]["2025"]["holidays"]]
    assert len(hol26) == 16 and len(set(hol26)) == 16 and date(2026, 1, 15) in hol26     # FAOP/71777 + 72262
    assert len(hol25) == 14 and len(set(hol25)) == 14                                    # FAOP/65588
    for h in hol25 + hol26:
        assert h.weekday() < 5, h
        assert not nse.is_trading_day(h)
    assert set(raw["years"]["2026"]["holiday_names"]) == {x.isoformat() for x in hol26}
    assert (nse.coverage_start, nse.coverage_end) == (date(2025, 1, 1), date(2026, 12, 31))
    assert len(nse.trading_days(date(2026, 1, 1), date(2026, 12, 31))) == 261 - 16
    assert len(nse.trading_days(date(2025, 1, 1), date(2025, 12, 31))) == 261 - 14
    budget = nse.special_session(date(2026, 2, 1))
    assert budget is not None and not budget.trade and (budget.open, budget.close) == (time(9, 15), time(15, 30))
    assert not nse.is_trading_day(date(2026, 2, 1)) and nse.session_hours(date(2026, 2, 1)) is None
    muhurat = nse.special_session(date(2025, 10, 21))
    assert muhurat is not None and not muhurat.trade and muhurat.open == time(13, 45)
    assert nse.is_trading_day(date(2026, 10, 12)) and not nse.is_trading_day(date(2026, 10, 10))


def test_session_hours_follow_regular_hours_table(nse):
    # the F&O close moved from 15:30 to 15:40 on Mon 2026-08-03; nothing hardcodes the close
    assert nse.session_hours(date(2026, 7, 31)) == (time(9, 15), time(15, 30))
    assert nse.session_hours(date(2026, 8, 3)) == (time(9, 15), time(15, 40))
    assert nse.session_hours(date(2026, 10, 12)) == (time(9, 15), time(15, 40))
    assert nse.session_hours(date(2026, 10, 20)) is None
    opens, closes = nse.session_bounds(date(2026, 10, 12))
    assert opens == datetime(2026, 10, 12, 9, 15, tzinfo=IST) and closes == datetime(2026, 10, 12, 15, 40, tzinfo=IST)
    assert nse.quotes_valid_from(date(2026, 10, 12)) == datetime(2026, 10, 12, 9, 15, 30, tzinfo=IST)
    assert nse.quotes_valid_from(date(2026, 10, 11)) is None
    # calendars without a regular_hours table use today's NSE hours, which the shipped table also ends with
    assert ck.DEFAULT_REGULAR_HOURS[-1][1:] == nse.session_hours(date(2026, 12, 31))
    assert small_calendar().session_hours(date(2026, 10, 19)) == (time(9, 15), time(15, 40))


def test_coverage_and_fail_closed(nse, tmp_path):
    assert nse.covers(date(2026, 12, 31)) and not nse.covers(date(2027, 1, 1))
    assert not nse.is_trading_day(date(2027, 1, 4))                # a Monday beyond coverage: no trading
    assert nse.covers_ahead(date(2026, 10, 12), 30)
    assert not nse.covers_ahead(date(2026, 12, 15), 30)            # self-check WARN territory
    assert nse.next_trading_day(date(2026, 12, 31)) is None
    assert nse.prev_trading_day(date(2025, 1, 1)) is None
    empty = SessionCalendar.empty()
    assert not empty.is_trading_day(date(2026, 10, 12)) and empty.td(date(2026, 1, 1), date(2026, 12, 31)) == 0
    assert empty.session_hours(date(2026, 10, 12)) is None and empty.coverage_end is None
    assert not empty.covers(date(2026, 10, 12)) and not empty.covers_ahead(date(2026, 10, 12))
    with pytest.raises(CalendarError):
        SessionCalendar.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(CalendarError):
        SessionCalendar.load(bad)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"years": {"2026": {"holidays": []}}}), encoding="utf-8")
    assert SessionCalendar.load(good).td(date(2026, 10, 9), date(2026, 10, 12)) == 1


@pytest.mark.parametrize("data", [
    {},
    {"years": {}},
    {"years": []},
    {"years": {"26": {"holidays": []}}},
    {"years": {"2026": {}}},
    {"years": {"2026": {"holidays": "2026-01-26"}}},
    {"years": {"2026": {"holidays": ["2025-12-25"]}}},
    {"years": {"2026": {"holidays": ["2026-13-01"]}}},
    {"years": {"2026": {"holidays": [20260126]}}},
    {"years": {"2026": {"holidays": [], "holiday_names": {"2026-01-26": "Republic Day"}}}},
    {"years": {"2026": {"holidays": [], "special_sessions": {"date": "2026-02-01"}}}},
    {"years": {"2026": {"holidays": [], "special_sessions": [
        {"date": "2026-02-01", "open": "15:30", "close": "09:15", "trade": False}]}}},
    {"years": {"2026": {"holidays": [], "special_sessions": [
        {"date": "2026-02-01", "open": "09:15", "close": "15:30", "trade": "no"}]}}},
    {"years": {"2026": {"holidays": [], "special_sessions": [
        {"date": "2026-02-01", "open": "09:15", "close": "15:30"},
        {"date": "2026-02-01", "open": "09:15", "close": "15:30"}]}}},
    {"years": {"2026": {"holidays": [], "special_sessions": [{"date": "2026-02-01", "open": "25:00", "close": "26:00"}]}}},
    {"years": {"2025": {"holidays": []}, "2027": {"holidays": []}}},
    {"years": {"2026": {"holidays": []}}, "regular_hours": []},
    {"years": {"2026": {"holidays": []}}, "regular_hours": [{"from": "2026-06-01", "open": "09:15", "close": "15:40"}]},
    {"years": {"2026": {"holidays": []}}, "regular_hours": [
        {"from": "2026-01-01", "open": "09:15", "close": "15:30"},
        {"from": "2026-01-01", "open": "09:15", "close": "15:40"}]},
    {"years": {"2026": {"holidays": []}}, "regular_hours": [{"from": "2026-01-01", "open": "09:15"}]},
])
def test_calendar_rejects_malformed(data):
    with pytest.raises(CalendarError):
        SessionCalendar.from_dict(data)


def test_neighbours_and_week_end(nse):
    assert nse.prev_trading_day(date(2026, 10, 21)) == date(2026, 10, 19)
    assert nse.next_trading_day(date(2026, 10, 19)) == date(2026, 10, 21)
    assert nse.next_trading_day(date(2026, 10, 17)) == date(2026, 10, 19)         # from a Saturday
    assert nse.prev_trading_day(date(2026, 1, 1)) == date(2025, 12, 31)
    assert nse.trading_days(date(2026, 10, 16), date(2026, 10, 23)) == [
        date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]
    assert nse.is_last_session_of_week(date(2026, 10, 16))
    assert not nse.is_last_session_of_week(date(2026, 10, 15))
    assert nse.is_last_session_of_week(date(2026, 4, 2))            # Thursday before Good Friday
    assert nse.is_last_session_of_week(date(2026, 12, 31))          # next session unknown: prune anyway
    assert not nse.is_last_session_of_week(date(2026, 10, 17))      # not a session


def test_fake_clock():
    c = FakeClock()
    assert c.now_wall() == datetime(2026, 10, 12, 9, 0, tzinfo=IST) and c.mono() == 1000.0
    assert c.today() == date(2026, 10, 12)
    c.advance(0.25)
    assert c.mono() == 1000.25 and c.now_wall() == datetime(2026, 10, 12, 9, 0, 0, 250000, tzinfo=IST)
    for bad in (-0.05, math.nan):
        with pytest.raises(ValueError):
            c.advance(bad)
    c.advance_to("09:21:00")
    assert c.now_wall() == datetime(2026, 10, 12, 9, 21, tzinfo=IST) and c.mono() == pytest.approx(2260.0)
    with pytest.raises(ValueError):
        c.advance_to("09:00")
    c.jump_wall(5)                                                  # NTP step: wall moves, mono does not (I15)
    assert c.now_wall() == datetime(2026, 10, 12, 9, 21, 5, tzinfo=IST) and c.mono() == pytest.approx(2260.0)
    c.jump_wall(-3)
    assert c.now_wall() == datetime(2026, 10, 12, 9, 21, 2, tzinfo=IST)
    c.advance_to(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))   # 09:30 IST
    assert c.now_wall() == datetime(2026, 10, 12, 9, 30, tzinfo=IST)
    assert FakeClock(datetime(2026, 10, 12, 9, 15)).now_wall() == datetime(2026, 10, 12, 9, 15, tzinfo=IST)
    assert FakeClock("2026-10-12T03:45:00Z", mono_start=5).now_wall() == datetime(2026, 10, 12, 9, 15, tzinfo=IST)
    assert FakeClock("2026-10-12 09:15:00").mono() == 1000.0
    assert isinstance(c, ck.Clock)
    # 50 ms runner steps keep wall and mono in exact lockstep (no float drift that could look like a clock jump)
    s = FakeClock()
    for _ in range(36_000):                                         # 30 minutes
        s.advance(0.05)
    assert s.mono() == 2800.0 and s.now_wall() == datetime(2026, 10, 12, 9, 30, tzinfo=IST)


def test_real_clock_and_ist_helpers():
    rc = ck.RealClock()
    a, b = rc.mono(), rc.mono()
    assert b >= a
    now = rc.now_wall()
    assert now.utcoffset() == timedelta(hours=5, minutes=30) and rc.today() == now.date()

    assert ck.IST.utcoffset(datetime(2026, 10, 12)) == timedelta(hours=5, minutes=30)
    with pytest.raises(ValueError):
        ck.to_ist(datetime(2026, 10, 12, 9, 15))                    # naive wall time is refused
    with pytest.raises(TypeError):
        ck.to_ist(date(2026, 10, 12))
    utc = datetime(2026, 10, 12, 3, 50, 1, 412000, tzinfo=timezone.utc)
    assert ck.to_ist(utc) == datetime(2026, 10, 12, 9, 20, 1, 412000, tzinfo=IST)
    assert ck.iso(utc) == "2026-10-12T09:20:01.412+05:30"
    assert ck.parse_wall("2026-10-12T03:50:01.412Z") == utc
    assert ck.parse_wall(ck.iso(utc)) == utc
    assert ck.parse_wall("2026-10-12 09:20:01", assume_ist=True) == datetime(2026, 10, 12, 9, 20, 1, tzinfo=IST)
    with pytest.raises(ValueError):
        ck.parse_wall("2026-10-12 09:20:01")
    assert ck.parse_hms("09:15") == time(9, 15) and ck.parse_hms("09:15:30.500") == time(9, 15, 30, 500000)
    assert ck.parse_hms(time(9, 20)) == time(9, 20)
    with pytest.raises(ValueError):
        ck.parse_hms("09:15+05:30")
    assert ck.ist_datetime(date(2026, 10, 12), "09:15:30") == datetime(2026, 10, 12, 9, 15, 30, tzinfo=IST)
    assert ck.session_of(datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)) == date(2026, 10, 12)
