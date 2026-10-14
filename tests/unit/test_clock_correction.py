"""titan2.clock.CORRECTION: titan2 keeps internet time on a laptop whose Windows clock drifts and cannot be fixed
(no admin rights; 2026-10-13: 1.43 s behind), and whose Windows Time service wakes up some days and steps the clock
by 1-2.5 s. The laptop measures the offset at start and every 5 minutes; steps are absorbed at once."""

from __future__ import annotations

import logging
import math
import threading
import time
import types
from datetime import datetime

import pytest

from titan2 import clock as clock_mod, laptop, selfcheck
from titan2.clock import IST, RealClock, WallCorrection
from titan2.laptop import ClockOffset

T0 = datetime(2026, 10, 13, 9, 58, tzinfo=IST).timestamp()


def exactly(x: float):
    """Epoch seconds are ~1.8e9: pytest.approx's default relative tolerance would allow half an hour."""
    return pytest.approx(x, abs=1e-6)


class Clocks:
    """A fake system clock (time.time) and monotonic clock: advance() is time passing, step() is Windows setting
    its clock."""

    def __init__(self) -> None:
        self.t, self.m = T0, 5000.0

    def system(self) -> float:
        return self.t

    def mono(self) -> float:
        return self.m

    def advance(self, dt: float) -> None:
        self.t += dt
        self.m += dt

    def step(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def corr(monkeypatch):
    """A fresh process-wide correction on fake clocks; CORRECTION and the log record factory restored after."""
    clocks = Clocks()
    c = WallCorrection(system=clocks.system, mono=clocks.mono)
    monkeypatch.setattr(clock_mod, "CORRECTION", c)
    monkeypatch.setattr(clock_mod, "_log_factory_installed", False)
    factory = logging.getLogRecordFactory()
    yield c, clocks
    logging.setLogRecordFactory(factory)


def test_unset_reads_the_system_clock(corr):
    c, _ = corr
    assert c.epoch() is None and c.offset_s is None
    before = datetime.now(IST)
    assert abs((RealClock().now_wall() - before).total_seconds()) < 1     # datetime.now(IST), as without it


def test_set_adds_the_offset(corr):
    c, clocks = corr
    c.set(1.433, 0.043, "NTP time.windows.com")
    assert c.epoch() == exactly(T0 + 1.433)
    now = RealClock().now_wall()
    assert now.tzinfo is IST and now.timestamp() == exactly(T0 + 1.433)
    clocks.advance(60.0)                                                   # the system clock keeps the time
    assert RealClock().now_wall().timestamp() == exactly(T0 + 61.433)
    assert (c.error_s, c.source, c.set_at, c.steps) == (0.043, "NTP time.windows.com", T0, 0)


def test_it_never_moves_backwards(corr):
    c, clocks = corr
    c.set(1.433)
    first = c.epoch()
    c.set(1.403)                                                           # a re-measure 30 ms lower
    assert c.epoch() == first                                              # held still ...
    clocks.advance(0.02)
    assert c.epoch() == first
    clocks.advance(0.02)                                                   # ... until the system clock catches up
    assert c.epoch() == exactly(T0 + 0.04 + 1.403) and c.epoch() > first


def test_a_windows_clock_step_is_absorbed_at_once(corr):
    """The review's case: Windows Time steps the clock +1.15 s mid-session. titan2's time must go on unbroken (not
    jump 1.15 s ahead until the next reading) and ask for a fresh reading."""
    c, clocks = corr
    c.set(1.433)
    clocks.advance(100.0)
    before = c.epoch()
    clocks.step(1.15)                                                      # Windows syncs: +1.15 s at once
    clocks.advance(0.2)
    assert c.epoch() == exactly(before + 0.2)                              # unbroken: internet time
    assert c.offset_s == exactly(1.433 - 1.15) and c.steps == 1 and c.last_step_s == exactly(1.15)
    assert c.step_pending
    clocks.step(-2.0)                                                      # a step back is absorbed the same way
    clocks.advance(1.0)
    assert c.epoch() == exactly(before + 1.2) and c.steps == 2
    c.set(-0.567 + 0.0005)                                                 # the fresh reading clears the request
    assert not c.step_pending


def test_jitter_is_not_a_step(corr):
    c, clocks = corr
    c.set(1.433)
    for dt in (0.0156, -0.0156, 0.031, -0.031, 0.1, -0.1, 0.24):
        clocks.step(dt)                                                    # 15.6 ms ticks, both ways
        clocks.step(-dt)
        clocks.step(dt)
        c.epoch()
        clocks.step(-dt)
    assert c.steps == 0 and not c.step_pending and c.offset_s == 1.433


def test_a_first_offset_behind_holds_at_what_was_already_handed_out(corr):
    c, clocks = corr
    c.set(-0.5)                                                            # Windows ahead of internet time
    assert c.epoch() == T0                                                 # not before the uncorrected reads
    clocks.advance(0.6)
    assert c.epoch() == exactly(T0 + 0.1)


def test_bad_values_are_refused_and_clear_resets(corr):
    c, _ = corr
    for bad in (math.nan, math.inf, True, "1.4", None):
        with pytest.raises(ValueError):
            c.set(bad)
    with pytest.raises(ValueError):
        c.set(1.0, -0.1)
    c.set(1.0)
    c.clear()
    assert c.epoch() is None and c.offset_s is None and c.error_s is None and not c.step_pending


def test_threads_always_see_time_go_forward(corr):
    c, clocks = corr
    c.set(1.4)
    seen: list[list[float]] = [[] for _ in range(4)]

    def reader(i: int) -> None:
        for _ in range(2000):
            seen[i].append(c.epoch())

    def writer() -> None:
        for k in range(200):
            c.set(1.4 + (0.01 if k % 2 else -0.01))
            clocks.advance(0.001)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)] + [threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in seen:
        assert all(b >= a for a, b in zip(s, s[1:]))


def test_env_offset_for_the_dashboard(corr):
    c, _ = corr
    assert clock_mod.apply_env_offset({}) is False and c.offset_s is None
    assert clock_mod.apply_env_offset({clock_mod.CLOCK_OFFSET_ENV: "abc"}) is False
    assert clock_mod.apply_env_offset({clock_mod.CLOCK_OFFSET_ENV: "inf"}) is False and c.offset_s is None
    assert clock_mod.apply_env_offset({clock_mod.CLOCK_OFFSET_ENV: "1.4335"}) is True
    assert c.offset_s == 1.4335 and c.source == "the laptop session"


def test_log_records_carry_corrected_time(corr):
    c, _ = corr
    clock_mod.install_log_correction()
    clock_mod.install_log_correction()                                     # idempotent
    plain = logging.getLogRecordFactory()("t", logging.INFO, __file__, 1, "m", (), None)
    assert abs(plain.created - time.time()) < 5                            # unset: the system clock
    c.set(1.433)
    rec = logging.getLogRecordFactory()("t", logging.INFO, __file__, 1, "m", (), None)
    assert rec.created == exactly(T0 + 1.433) and rec.msecs == pytest.approx((rec.created % 1) * 1000)


# ---------------------------------------------------------------------------------------------------------------
# The laptop: measuring at start, every 5 minutes, and sooner when needed
# ---------------------------------------------------------------------------------------------------------------
def reading(offset: float, error: float = 0.043, source: str = "NTP time.windows.com"):
    return lambda: (ClockOffset(offset, error, source), source)


def test_correct_clock_first_then_refreshes(corr):
    c, _ = corr
    out: list[str] = []
    assert laptop.correct_clock(reading(1.433), out=out.append) == pytest.approx(1.433)
    assert c.offset_s == pytest.approx(1.433)
    assert out == ["clock: Windows is 1.433 s behind internet time (+-0.043 s, NTP time.windows.com); titan2 keeps "
                   "internet time"]
    assert laptop.correct_clock(reading(1.437), out=out.append) == pytest.approx(1.437)
    assert c.offset_s == pytest.approx(1.437) and len(out) == 1            # refreshes are logged, not printed
    assert laptop.correct_clock(lambda: (None, "no time source answered"), out=out.append) is None
    assert c.offset_s == pytest.approx(1.437)                              # no reading: the last one stays
    assert out[-1] == "clock: no internet time (no time source answered): the last correction stays"
    coarse = reading(1.6, 0.6, "the Date header of https://api.kite.trade")
    assert laptop.correct_clock(coarse) is None and c.offset_s == pytest.approx(1.437)     # agrees: kept
    assert laptop.correct_clock(reading(1.44, 0.2, "NTP pool.ntp.org")) == pytest.approx(1.44)   # still exact enough


def test_a_coarse_reading_that_proves_the_correction_stale_is_applied(corr):
    """The review's case: only the HTTP Date answers and it contradicts the offset in force beyond both errors."""
    c, _ = corr
    c.set(1.433, 0.043)
    assert laptop.correct_clock(reading(0.1, 0.55, "the Date header")) == pytest.approx(0.1)
    assert c.offset_s == pytest.approx(0.1) and c.error_s == 0.55


def test_correct_clock_without_any_reading_leaves_the_system_clock(corr):
    c, _ = corr
    out: list[str] = []
    assert laptop.correct_clock(lambda: (None, "no time source answered"), out=out.append) is None
    assert c.offset_s is None and laptop.correction_missing()
    assert out[-1] == ("clock: no internet time (no time source answered): titan2 uses the Windows clock as it is "
                       "and tries again every 20 s")
    assert laptop.correct_clock(reading(3600.0), out=out.append) is None and c.offset_s is None   # never trusted
    assert "beyond the 60 s titan2 corrects; titan2 uses the Windows clock as it is" in out[-1]
    assert laptop.correct_clock(reading(-0.9, 0.55, "the Date header")) == pytest.approx(-0.9)    # first: any reading
    assert not laptop.correction_missing()


class _Ctl:
    presses = 0


def _supervisor(tmp_path, keeper, mono):
    return laptop.Supervisor(ctl=_Ctl(), session=laptop.Session(), var=tmp_path, clock=types.SimpleNamespace(),
                             out=lambda s: None, cli_mod=types.SimpleNamespace(CLI_DB="c.db"),
                             power=lambda: {"on_ac": True}, mono=lambda: mono[0], clock_keeper=keeper)


def _run(sup, mono, calls, t):
    mono[0] = t
    sup.step()
    if sup._clock_thread is not None:
        sup._clock_thread.join(5)
    return list(calls)


def test_supervisor_refreshes_every_5_minutes_on_its_own_thread(tmp_path, corr):
    c, _ = corr
    c.set(1.433)                                                           # the start measured
    calls: list[float] = []
    mono = [1000.0]
    sup = _supervisor(tmp_path, lambda: calls.append(mono[0]), mono)
    assert _run(sup, mono, calls, 1000.0) == []                            # the first refresh waits its turn
    assert _run(sup, mono, calls, 1000.0 + laptop.CLOCK_REFRESH_S - 1) == []
    assert _run(sup, mono, calls, 1000.0 + laptop.CLOCK_REFRESH_S) == [1000.0 + laptop.CLOCK_REFRESH_S]
    assert len(_run(sup, mono, calls, 1000.0 + laptop.CLOCK_REFRESH_S + 30)) == 1


def test_supervisor_retries_every_20_s_without_a_correction(tmp_path, corr):
    """The review's case: the start's reading failed. Waiting 5 minutes would leave the one-time self-checks to
    fail the clock and keep entries off all day."""
    c, _ = corr
    calls: list[float] = []
    mono = [0.0]
    sup = _supervisor(tmp_path, lambda: calls.append(mono[0]), mono)
    assert _run(sup, mono, calls, 0.0) == [0.0]                            # at once
    assert _run(sup, mono, calls, 10.0) == [0.0]
    assert _run(sup, mono, calls, 20.0) == [0.0, 20.0]                     # every 20 s while there is none
    c.set(1.433)                                                           # a reading landed
    assert _run(sup, mono, calls, 40.0) == [0.0, 20.0]                     # back to every 5 minutes
    assert _run(sup, mono, calls, 320.0) == [0.0, 20.0, 320.0]


def test_supervisor_measures_soon_after_a_clock_step(tmp_path, corr):
    c, clocks = corr
    c.set(1.433)
    calls: list[float] = []
    mono = [1000.0]
    sup = _supervisor(tmp_path, lambda: calls.append(mono[0]), mono)
    _run(sup, mono, calls, 1000.0)
    clocks.step(1.15)
    c.epoch()                                                              # the next stamp absorbs the step
    assert c.step_pending
    assert _run(sup, mono, calls, 1030.0) == [1030.0]                      # well before the 5 minutes


# ---------------------------------------------------------------------------------------------------------------
# The self-check judges the clock titan2 uses
# ---------------------------------------------------------------------------------------------------------------
def _check(monkeypatch, offset, error=0.043):
    monkeypatch.setattr(selfcheck, "_clock_reading", lambda ctx: (offset, error, "NTP time.windows.com"))
    return selfcheck._check_clock(None)


def test_clock_row_judges_the_corrected_clock(corr, monkeypatch):
    c, _ = corr
    ok, detail = _check(monkeypatch, 1.433)                                # uncorrected: as before
    assert not ok and detail.startswith("this clock is 1.433 s behind internet time (+-0.043 s")
    assert "titan2 corrects its own clock as soon as an internet time source answers" in detail
    c.set(1.42, 0.043, "NTP time.windows.com")
    ok, detail = _check(monkeypatch, 1.433)
    assert ok and detail == ("titan2's clock is 0.013 s behind internet time (+-0.043 s, NTP time.windows.com); it "
                             "corrects the Windows clock, 1.433 s behind internet time")
    ok, detail = _check(monkeypatch, 2.9)                                  # Windows stepped since the reading
    assert not ok and "1.480 s behind internet time" in detail and "off even so, so entries stay off" in detail


def test_the_dashboard_gets_the_sessions_offset(corr, tmp_path):
    c, _ = corr
    seen: dict = {}

    def popen(cmd, **kw):
        seen.clear()
        seen.update(kw["env"])
        return types.SimpleNamespace(poll=lambda: None)

    laptop.start_dash({"PATH": "x"}, tmp_path, tmp_path, datetime.now(IST).date(), out=lambda s: None, popen=popen)
    assert clock_mod.CLOCK_OFFSET_ENV not in seen
    c.set(1.433)
    laptop.start_dash({"PATH": "x"}, tmp_path, tmp_path, datetime.now(IST).date(), out=lambda s: None, popen=popen)
    assert float(seen[clock_mod.CLOCK_OFFSET_ENV]) == 1.433


# ---------------------------------------------------------------------------------------------------------------
# 2026-10-14 08:04: one slow NTP answer (+-3.6 s) was applied and titan2's clock ran 3.5 s ahead
# ---------------------------------------------------------------------------------------------------------------
def test_measure_clock_keeps_the_most_exact_of_three_samples():
    answers = iter([ClockOffset(3.514, 3.619, "NTP time.windows.com"), ClockOffset(0.002, 0.021, "NTP time.windows.com"),
                    ClockOffset(0.010, 0.040, "NTP time.windows.com")])
    asked: list[str] = []

    def ntp(host, timeout_s):
        asked.append(host)
        return next(answers)

    reading, source = laptop.measure_clock(hosts=("time.windows.com", "pool.ntp.org"), ntp=ntp)
    assert (float(reading), reading.error_s, source) == (pytest.approx(0.002), 0.021, "NTP time.windows.com")
    assert asked == ["time.windows.com"] * 3                    # the first host answered: the second is not asked

    def failing_then_ok(host, timeout_s):
        if host == "time.windows.com":
            raise OSError("timed out")
        return ClockOffset(0.5, 0.03, f"NTP {host}")

    reading, source = laptop.measure_clock(hosts=("time.windows.com", "pool.ntp.org"), ntp=failing_then_ok)
    assert source == "NTP pool.ntp.org" and float(reading) == 0.5


def test_a_reading_too_uncertain_is_never_applied(corr):
    c, _ = corr
    out: list[str] = []
    assert laptop.correct_clock(reading(3.514, 3.619), out=out.append) is None and c.offset_s is None
    assert out[-1].startswith("clock: a reading too uncertain to use (+3.514 s +-3.619 s, NTP time.windows.com); "
                              "titan2 uses the Windows clock as it is and tries again every 20 s")
    assert laptop.correct_clock(reading(0.001, 0.023), out=out.append) == pytest.approx(0.001)    # the next one, exact
    assert laptop.correct_clock(reading(2.0, 1.2)) is None and c.offset_s == pytest.approx(0.001)  # kept, not replaced
    c.clear()
    assert laptop.correct_clock(reading(0.6, 0.7, "the Date header")) == pytest.approx(0.6)        # HTTP fallback fits
