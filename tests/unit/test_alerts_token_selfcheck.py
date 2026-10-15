"""U18: titan2/alerts.py (Telegram, the healthchecks dead-man, delivery rules), titan2/token.py (the session file, the
headless login and the manual exchange; never restarts anything) and titan2/selfcheck.py (the DETAIL §13.7 matrix).

Offline and deterministic: every network, Kite, systemd and host probe is a fake; no real secret is ever read.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import types
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest

from titan2 import alerts, charges, selfcheck, store as store_mod, token
from titan2.clock import IST, FakeClock
from titan2.core.events import Alert
from titan2.model import Account, HaltClears, Margins

GB, MB = 1024 ** 3, 1024 ** 2

# Fake credentials: distinctive strings so a leak into any log line or message is easy to spot.
API_KEY, API_SECRET, USER = "kiteapikey01", "ApiSecretValue-77f1", "AB1234"
PASSWORD, TOTP_SECRET, TOTP_CODE = "Password-Hunter2", "TOTPSECRETBASE32XYZ", "492039"
REQUEST_TOKEN, ACCESS_TOKEN = "RqTok0123456789abcdef", "AcTok9876543210fedcbaXYZ"
SECRETS = (API_SECRET, PASSWORD, TOTP_SECRET, TOTP_CODE, REQUEST_TOKEN, ACCESS_TOKEN)
NOW = datetime(2026, 10, 12, 8, 30, 5, tzinfo=IST)            # a Monday


# ---------------------------------------------------------------------------------------------------------------
# alerts.py
# ---------------------------------------------------------------------------------------------------------------
class FakeTelegram:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def send(self, text: str) -> None:
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail:
            raise alerts.AlertError("telegram: HTTP 502")
        self.sent.append(text)


class FakeHC:
    def __init__(self, fail: bool = False) -> None:
        self.pings: list[tuple[str, str]] = []
        self.fail = fail

    def ping(self, signal: str = "", body: str = "") -> None:
        if self.fail:
            raise alerts.AlertError(f"healthchecks {signal or 'ping'}: HTTP 503")
        self.pings.append((signal, body))


def drain(alerter: alerts.Alerter) -> None:
    for _ in range(100):
        if not alerter.queued():
            return
        alerter.step()
    raise AssertionError("alert queue did not drain")


def body(msg: str) -> str:
    return msg.splitlines()[1]


def test_alert_dedup_nonblocking():
    clock = FakeClock("2026-10-12T10:00:00+05:30")
    tg = FakeTelegram()
    a = alerts.Alerter(tg, None, clock=clock, sleep=lambda s: None)

    # INFO: one message per key per 5 min; duplicates are not lost, they are counted into the next message
    assert a.submit("INFO", "TRADE_CLOSED", "PAPER:od RELIANCE net +120", "closed:17") is True
    assert a.submit("INFO", "TRADE_CLOSED", "PAPER:od RELIANCE net +121", "closed:17") is False
    drain(a)
    assert len(tg.sent) == 1 and body(tg.sent[0]) == "PAPER:od RELIANCE net +120"
    clock.advance(299)
    assert a.submit("INFO", "TRADE_CLOSED", "again", "closed:17") is False
    clock.advance(2)
    assert a.submit("INFO", "TRADE_CLOSED", "after the window", "closed:17") is True
    drain(a)
    assert body(tg.sent[-1]) == "after the window" and "(+2 more like this" in tg.sent[-1]

    # WARN: at most once per key per 30 min; the key defaults to the code
    assert a.submit("WARN", "FEED_RECONNECT", "ws reconnected")
    clock.advance(1200)
    assert not a.submit("WARN", "FEED_RECONNECT", "ws reconnected again")
    clock.advance(601)
    assert a.submit("WARN", "FEED_RECONNECT", "ws reconnected a third time")
    # a CRITICAL with the same key is never held back by the WARN window, and it is sent before queued WARNs
    assert a.submit("CRITICAL", "FEED_RECONNECT", "ws down 60 s", "FEED_RECONNECT")
    drain(a)
    assert [m.split()[1] for m in tg.sent[-3:]] == ["CRITICAL", "WARN", "WARN"]
    assert a.ack("FEED_RECONNECT") and a.unacked_critical() == 0

    # CRITICAL: repeats every 5 min with the latest text until acknowledged (ACK_ALERT through dispatch)
    tg.sent.clear()
    b = alerts.Alerter(tg, None, clock=clock, sleep=lambda s: None)
    assert b.dispatch(Alert("CRITICAL", "MISMATCH", "LIVE: broker 0 vs bot 65", "MISMATCH:LIVE"))
    assert not b.submit("CRITICAL", "MISMATCH", "LIVE: broker 0 vs bot 65 (2nd recon)", "MISMATCH:LIVE")
    b.step()
    assert len(tg.sent) == 1 and tg.sent[0].endswith("ack: titan2 ack-alert MISMATCH:LIVE")
    assert b.unacked_critical() == 1 and b.active_keys() == ["MISMATCH:LIVE"]
    clock.advance(299)
    b.step()
    assert len(tg.sent) == 1
    clock.advance(1)
    b.step()
    assert len(tg.sent) == 2 and "(repeat 1)" in tg.sent[1]
    assert body(tg.sent[1]) == "LIVE: broker 0 vs bot 65 (2nd recon)" and "(+1 more like this" in tg.sent[1]
    assert b.dispatch(Alert("INFO", alerts.ACK_CODE, "ack via CLI", "MISMATCH:LIVE")) is True
    assert b.unacked_critical() == 0 and b.counters()["acks"] == 1
    assert b.ack("MISMATCH:LIVE") is False                                # nothing left to acknowledge
    clock.advance(200)
    assert not b.submit("CRITICAL", "MISMATCH", "re-raised inside the window", "MISMATCH:LIVE")
    clock.advance(900)
    b.step()
    assert len(tg.sent) == 2                                              # no repeat after the ack
    assert b.submit("CRITICAL", "MISMATCH", "a new mismatch", "MISMATCH:LIVE")   # a new incident alerts again
    b.step()
    assert len(tg.sent) == 3 and b.unacked_critical() == 1
    # an unknown severity is never dropped quietly: it becomes CRITICAL
    assert b.submit("SEVERE", "ODD", "bad severity")
    b.step()
    assert tg.sent[-1].startswith("[titan2] CRITICAL ODD") and "unknown severity 'SEVERE'" in tg.sent[-1]

    # Non-blocking: Telegram hangs; the core keeps submitting; the queue is bounded and drops the oldest
    slow = FakeTelegram()
    slow.gate = threading.Event()
    c = alerts.Alerter(slow, None, queue_max=1000)
    c.start()
    try:
        assert c.submit("WARN", "FIRST", "first", "k-first")
        assert slow.entered.wait(5)                     # the alerts thread is now stuck inside the HTTP call
        worst = 0.0
        for i in range(1500):
            t0 = time.perf_counter()
            c.submit("WARN", "STORM", f"alert {i}", f"k{i}")
            worst = max(worst, time.perf_counter() - t0)
        assert worst < 0.05, f"submit blocked for {worst:.3f} s"
        assert c.queued() == 1000
        assert c.counters()["dropped"] == 500
        # a full queue still takes a CRITICAL: the oldest WARN makes room, never a CRITICAL
        assert c.submit("CRITICAL", "TOKEN", "token dead with positions open", "TOKEN")
        assert c.queued() == 1000 and c.counters()["dropped"] == 501
    finally:
        slow.gate.set()
        c.stop(timeout_s=10)
    bodies = [body(m) for m in slow.sent]
    assert bodies[:3] == ["first", "token dead with positions open", "alert 501"] and len(bodies) == 1001
    assert "alert 500" not in bodies and bodies[-1] == "alert 1499"
    assert c.counters()["sent"] == 1001


def test_alert_pacing():
    clock = FakeClock("2026-10-12T10:00:00+05:30")
    tg, waits = FakeTelegram(), []

    def sleep(seconds):
        waits.append(round(seconds, 3))
        clock.advance(seconds)

    a = alerts.Alerter(tg, None, clock=clock, sleep=sleep)
    for i in range(3):
        a.submit("WARN", "X", f"m{i}", f"k{i}")
    drain(a)
    assert waits == [1.0, 1.0] and len(tg.sent) == 3        # one Telegram message a second
    clock.advance(5)
    a.submit("WARN", "X", "later", "k-later")
    drain(a)
    assert waits == [1.0, 1.0]                              # no wait once the pace allows it


def test_healthcheck_gated():
    clock = FakeClock("2026-10-12T08:30:00+05:30")
    hb = alerts.Heartbeat()
    hc = FakeHC()
    a = alerts.Alerter(None, hc, hb, clock=clock, sleep=lambda s: None)

    def tick(seconds: float, progress: bool = True) -> list[str]:
        clock.advance(seconds)
        if progress:
            hb.core_progress(clock.mono())
        return a.step()

    # READY: pinged once the core has made progress, even before the 08:45 window
    a.ping_ready()
    assert a.step() == [] and hc.pings == []                 # no core progress yet: no ping, READY stays pending
    assert tick(1) == ["ping"] and hc.pings == [("", "")]
    assert tick(61) == []                                    # 08:31: outside the window
    clock.advance_to("08:44:59")
    hb.core_progress(clock.mono())
    assert a.step() == []
    assert tick(1) == ["ping"]                               # 08:45:00: window opens
    assert tick(30) == []                                    # every 60 s, not more
    assert tick(30) == ["ping"]
    # a stuck core withholds the ping: the dead-man fires from outside the host
    assert tick(60, progress=False) == []
    clock.advance(9.9)
    assert a.step() == []                                    # still stale
    hb.core_progress(clock.mono() - 9.9)                     # progress 9.9 s ago: under 10 s is fresh
    assert a.step() == ["ping"]
    clock.advance(60)
    hb.core_progress(clock.mono() - 10.0)                    # exactly 10 s old: stale
    assert a.step() == []
    # a failed ping is retried after 10 s, not 60
    hc.fail = True
    assert tick(1) == ["ping"] and a.counters()["ping_failed"] == 1
    hc.fail = False
    assert tick(5) == []
    assert tick(5) == ["ping"]
    # no fixed end of the day: the pings go on while the core runs (its EOD exit ends them; a later
    # BROKER_SQUAREOFF_AT moves that exit to 15:53), and stop with the heartbeat
    clock.advance_to("15:44:30")
    hb.core_progress(clock.mono())
    assert a.step() == ["ping"]
    assert tick(30) == []                                    # 15:45:00
    assert tick(30) == ["ping"]                              # 15:45:30: still running, still pinging
    assert tick(120, progress=False) == []                   # the core has stopped
    # /fail on a CRITICAL exit is never gated, not even on a stale heartbeat
    clock.advance(600)
    a.fail("exit 78: integrity_check failed")
    assert a.step() == ["fail"]
    assert hc.pings[-1] == ("fail", "exit 78: integrity_check failed")
    assert a.counters()["pings"] == len(hc.pings)
    # no healthchecks configured: nothing to ping, nothing breaks
    quiet = alerts.Alerter(None, None, hb, clock=clock)
    quiet.ping_ready()
    quiet.fail("x")
    assert quiet.step() == []


def _cron_ticks(day: date, schedule: tuple[str, ...] = alerts.DEADMAN_SCHEDULE) -> list[datetime]:
    """Every tick of schedule (alerts.DEADMAN_SCHEDULE by default) on day (fields '*', 'n' or 'a-b'; weekdays only)."""
    def field(spec: str, lo: int, hi: int) -> range:
        if spec == "*":
            return range(lo, hi + 1)
        a, _, b = spec.partition("-")
        return range(int(a), int(b or a) + 1)

    out = []
    for line in schedule:
        minute, hour, dom, month, dow = line.split()
        assert (dom, month, dow) == ("*", "*", "1-5"), line
        if day.weekday() < 5:
            out += [datetime.combine(day, dtime(h, m), IST) for h in field(hour, 0, 23) for m in field(minute, 0, 59)]
    return sorted(out)


def deadman_down(pings: list[datetime], day: date, schedule: tuple[str, ...] = alerts.DEADMAN_SCHEDULE,
                 paused_at: datetime | None = None) -> list[str]:
    """When a healthchecks cron check on schedule (DEADMAN_SCHEDULE by default) would go DOWN on day, given the pings
    it received: after a ping at p the next ping is due by the first tick after p plus the grace (the day starts as
    if pinged at 00:00). paused_at: the owner pauses the check then (its Pause button); a paused check never goes
    DOWN, and the next ping resumes it (healthchecks.io's default), as if it were an ordinary ping."""
    ticks = _cron_ticks(day, schedule)
    grace = timedelta(minutes=alerts.DEADMAN_GRACE_MIN)
    down = []
    last = datetime.combine(day, dtime(0, 0), IST)
    for p in sorted(pings) + [datetime.combine(day + timedelta(days=1), dtime(0, 0), IST)]:
        due = next((t for t in ticks if t > last), None)
        paused = paused_at is not None and last < paused_at < p and due is not None and paused_at <= due + grace
        if due is not None and p > due + grace and not paused:
            down.append(f"after {last:%H:%M:%S} the {due:%H:%M} tick needs a ping by {due + grace:%H:%M} "
                        f"(next ping {p:%H:%M:%S})")
        last = p
    return down


def test_deadman_schedule_matches_the_bots_day():
    """The documented dead-man check (alerts.DEADMAN_SCHEDULE) never goes DOWN on a healthy trading day, whatever
    BROKER_SQUAREOFF_AT is allowed to be: pings run from READY until the EOD exit at BROKER_SQUAREOFF_AT + 14 min
    (15:34 at the earliest), with one last ping as the exit begins. The old schedule (`* 9-15`, pings stopping at
    15:45, exit at 15:40) went DOWN at 15:44 every day."""
    from titan2 import settings as settings_mod
    from titan2.core.engine import EOD_AFTER_SQUAREOFF_S

    done_s = dict(EOD_AFTER_SQUAREOFF_S)["DONE"]
    lim = settings_mod.LIMITS["BROKER_SQUAREOFF_AT"]
    earliest = datetime.combine(date(2026, 10, 12), lim.lo) + timedelta(seconds=done_s)
    assert earliest.time() >= (datetime.combine(date(2026, 10, 12), alerts.DEADMAN_LAST_TICK)
                               + timedelta(minutes=1)).time()
    assert alerts.PING_FROM <= _cron_ticks(date(2026, 10, 12))[0].time()
    day = date(2026, 10, 12)                                      # a Monday session
    for sq in (lim.lo, settings_mod.DEFAULTS.BROKER_SQUAREOFF_AT, lim.hi):
        clock = FakeClock(datetime.combine(day, dtime(8, 45, 0), IST))
        hb, hc = alerts.Heartbeat(), FakeHC()
        a = alerts.Alerter(None, hc, hb, clock=clock, sleep=lambda s: None)
        stamps: list[datetime] = []
        original = hc.ping

        def ping(signal: str = "", body: str = "") -> None:
            original(signal, body)
            stamps.append(clock.now_wall())

        hc.ping = ping
        exit_at = datetime.combine(day, sq, IST) + timedelta(seconds=done_s)
        clock.advance(20)                                         # recovery, then READY
        hb.core_progress(clock.mono())
        a.ping_ready()
        while clock.now_wall() < exit_at:
            hb.core_progress(clock.mono())
            a.step()
            clock.advance(1.0)
        a.ping_now()                                              # the graceful exit
        for _ in range(5):
            a.step()
            clock.advance(1.0)
        assert stamps and stamps[-1] >= exit_at
        assert deadman_down(stamps, day) == [], (sq, deadman_down(stamps, day))


def _laptop_pings(day: date, start: dtime, to_ready_min: float, stop: dtime) -> list[datetime]:
    """When the bot's alerter pings the dead-man on a laptop day started by hand at `start`: from READY, to_ready_min
    after it (the login, the self-check and the recovery come first), until a stop at `stop` (the EOD exit, or a
    Ctrl+C), with one last ping as the stop begins."""
    clock = FakeClock(datetime.combine(day, start, IST) + timedelta(minutes=to_ready_min))
    hb, hc = alerts.Heartbeat(), FakeHC()
    a = alerts.Alerter(None, hc, hb, clock=clock, sleep=lambda s: None)
    stamps: list[datetime] = []
    original = hc.ping

    def ping(signal: str = "", body: str = "") -> None:
        original(signal, body)
        stamps.append(clock.now_wall())

    hc.ping = ping
    hb.core_progress(clock.mono())
    a.ping_ready()
    end = datetime.combine(day, stop, IST)
    while clock.now_wall() < end:
        hb.core_progress(clock.mono())
        a.step()
        clock.advance(5.0)
    a.ping_now()
    for _ in range(3):
        a.step()
        clock.advance(1.0)
    return stamps


def test_the_laptops_deadman_schedule_fits_a_start_by_hand():
    """Review round 5: docs/LAPTOP.md recommended the server's schedule, whose first tick is 08:45 (the server's bot
    starts by itself at 08:45). The laptop is started by hand between 08:40 and 09:10 and pings first at READY, a few
    minutes after the start, so a 09:05 start went DOWN at 08:48 and rang the phone, then UP again at READY. The
    laptop's own schedule, alerts.LAPTOP_DEADMAN_SCHEDULE, begins at 09:15: a start by 09:10 with up to 5 minutes to
    READY never goes DOWN up to the EOD exit at 15:40. A stop before its last tick (15:33) still goes DOWN, which is
    why the docs say to pause the check when stopping early. Review round 6: they said to pause it before the stop,
    but titan2 pings while it stops (through the Ctrl+C flatten, and once more as it exits) and a ping resumes a
    paused check, which then rang 3 to 4 minutes after the exit. docs/LAPTOP.md section 4 now says to pause it after
    titan2 has stopped, within a time read here from it and played on the schedule, at the worst second of a minute:
    the window's `titan2 stopped cleanly` comes within about 20 s of the exit's ping (the alerts' flush, then the
    supervisor, the condor job and the dashboard stop), or, when that ping is skipped (the threads took over 10 s to
    stop, so the core heartbeat was stale), within about 95 s of the minute's ping before it."""
    day = date(2026, 10, 12)                                      # a Monday session
    laptop_schedule = alerts.LAPTOP_DEADMAN_SCHEDULE
    assert laptop_schedule[-1] == alerts.DEADMAN_SCHEDULE[-1]       # it ends as the server's
    assert _cron_ticks(day, laptop_schedule)[0] == datetime.combine(day, dtime(9, 15), IST)
    for start in (dtime(8, 40), dtime(9, 0), dtime(9, 10)):
        pings = _laptop_pings(day, start, 5.0, dtime(15, 40))
        assert deadman_down(pings, day, laptop_schedule) == [], (start, deadman_down(pings, day, laptop_schedule))
    late = _laptop_pings(day, dtime(9, 5), 1.0, dtime(15, 40))
    assert deadman_down(late, day)[0].startswith("after 00:00:00 the 08:45 tick needs a ping by 08:48")   # the old
    early = _laptop_pings(day, dtime(8, 40), 2.0, dtime(14, 46))
    assert deadman_down(early, day, laptop_schedule)                # a Ctrl+C at 14:46 rings unless it is paused
    within = _documented_pause_after_a_stop()
    ctrl_c = datetime.combine(day, dtime(14, 45), IST)
    for start in (dtime(8, 40), dtime(9, 0, 58)):                 # its pings at :00, or at :58 of each minute
        for stop in (dtime(14, 46), dtime(14, 46, 30), dtime(14, 46, 57)):     # the flatten and the clean exit
            pings = _laptop_pings(day, start, 2.0, stop)
            assert [p for p in pings if p > ctrl_c] and deadman_down(pings, day, laptop_schedule), (start, stop)
            assert deadman_down(pings, day, laptop_schedule, paused_at=ctrl_c), (start, stop)    # the old advice
            for kept, line_after_s in ((pings, 20.0), (pings[:-1], 95.0)):
                paused = kept[-1] + timedelta(seconds=line_after_s) + within
                assert deadman_down(kept, day, laptop_schedule, paused_at=paused) == [], (start, stop, paused)
            assert deadman_down(pings, day, laptop_schedule, paused_at=pings[-1] + timedelta(minutes=5))   # late


def _documented_pause_after_a_stop() -> timedelta:
    """How soon docs/LAPTOP.md section 4 says to pause the dead-man check after an early stop: "once the window says
    `titan2 stopped cleanly`, within a minute" (or "within N minutes")."""
    text = " ".join((Path(__file__).resolve().parents[2] / "docs" / "LAPTOP.md").read_text(encoding="utf-8").split())
    m = re.search(r"\*\*The dead-man check\.\*\* [^*]*? after titan2 has stopped: once the window says `titan2 stopped "
                  r"cleanly`, within (a|\d+) minutes?\b", text)
    assert m, "docs/LAPTOP.md section 4 says nothing on when to pause the dead-man check after an early stop"
    return timedelta(minutes=1 if m.group(1) == "a" else int(m.group(1)))


def test_weekday_holiday_keeps_the_deadman_fed(tmp_path, monkeypatch):
    """A weekday NSE holiday (2026-10-20): healthchecks cannot know NSE holidays, so the bot stays up as the
    dead-man's pinger until the schedule's last tick (no broker call, no order), then exits 0, and the check never
    goes DOWN. SIGTERM ends it at once. A weekend or a missing HEALTHCHECK_URL changes nothing (exit at once)."""
    from titan2 import runtime
    from titan2.clock import SessionCalendar

    cal = SessionCalendar.load()
    holiday = date(2026, 10, 20)
    assert holiday.weekday() < 5 and not cal.is_trading_day(holiday)
    clock = FakeClock(datetime.combine(holiday, dtime(8, 45, 5), IST))
    notes: list[str] = []
    proc = runtime.BotProcess(env={"TITAN2_VAR": str(tmp_path / "var")}, clock=clock, calendar=cal,
                              notifier=runtime.Notifier(send=lambda b: notes.append(b.decode())),
                              cwd=tmp_path, check_kite=lambda **_k: None, install_signals=False)
    hb, hc = alerts.Heartbeat(), FakeHC()
    stamps: list[datetime] = []
    original = hc.ping

    def ping(signal: str = "", body: str = "") -> None:
        original(signal, body)
        stamps.append(clock.now_wall())

    hc.ping = ping
    proc.alerter = alerts.Alerter(None, hc, hb, clock=clock, sleep=lambda s: None)

    def sleep(seconds: float) -> None:                            # the alerts thread, inline
        clock.advance(seconds)
        proc.alerter.step()

    proc.sleep = sleep
    proc.keep_deadman(hb, holiday)
    assert clock.now_wall() >= datetime.combine(holiday, alerts.DEADMAN_LAST_TICK, IST) + timedelta(minutes=1)
    assert deadman_down(stamps, holiday) == [] and len(stamps) > 300
    assert notes.count("WATCHDOG=1") > 1000                       # systemd's watchdog stays fed
    assert proc.alerter.counters()["accepted"] == 1               # the INFO "no session today" note

    before = len(stamps)
    proc.stop.request("SIGTERM")                                  # SIGTERM: returns at once
    proc.clock = clock2 = FakeClock(datetime.combine(holiday, dtime(10, 0), IST))
    proc.keep_deadman(hb, holiday)
    assert clock2.now_wall() == datetime.combine(holiday, dtime(10, 0), IST) and len(stamps) == before
    proc.stop = runtime.StopRequest()                             # a weekend: the check expects nothing
    sat = date(2026, 10, 24)
    proc.clock = clock3 = FakeClock(datetime.combine(sat, dtime(10, 0), IST))
    proc.keep_deadman(hb, sat)
    assert clock3.now_wall() == datetime.combine(sat, dtime(10, 0), IST)

    # the whole bot on that holiday, the alerts thread for real: it pings and exits 0 (and never hangs on a clock
    # that only moves when told to)
    posts: list[str] = []
    monkeypatch.setattr(alerts, "_default_post",
                        lambda url, **kw: posts.append(url) or types.SimpleNamespace(status_code=200))
    from titan2 import settings as settings_mod
    settings_mod._reset_load_once()
    try:
        fake = FakeClock(datetime.combine(holiday, dtime(8, 45), IST))
        app = tmp_path / "app"
        (app / "titan2").mkdir(parents=True)
        sd_sent: list[str] = []
        sd = runtime.Notifier(send=lambda b: sd_sent.append(b.decode()))
        code = runtime.BotProcess(env={"TITAN2_VAR": str(tmp_path / "var2"),
                                       "HEALTHCHECK_URL": "https://hc-ping.com/00000000-0000-0000-0000-000000000000"},
                                  clock=fake, calendar=cal, cwd=app, check_kite=lambda **_k: None, notifier=sd,
                                  install_signals=False, sleep=fake.advance).run()
    finally:
        settings_mod._reset_load_once()
    assert code == 0 and posts and fake.now_wall().time() >= alerts.DEADMAN_LAST_TICK
    sent = [m.split("\n", 1)[0] for m in sd_sent]
    # systemd: READY first, the watchdog fed throughout, STOPPING only once the keeping is over (after STOPPING=1
    # systemd would kill the process at TimeoutStopSec)
    assert sent[0] == "READY=1" and sent[-1] == "STOPPING=1" and sent.count("STOPPING=1") == 1
    assert sent.count("WATCHDOG=1") > 1000


def test_critical_now_fails_the_deadman_only_for_the_bot():
    """titan2-alert@ sends "unit %i failed" for every titan2 unit. Only the bot's own failure may ping /fail on the
    bot's dead-man check; a failed self-check, backup, token or dashboard unit is Telegram only (else a healthy bot's
    check goes DOWN, e.g. after an 08:40 self-check FAIL)."""
    tg, hc = FakeTelegram(), FakeHC()
    assert alerts.critical_now("unit titan2-selfcheck.service failed", telegram=tg, healthcheck=hc,
                               sleep=lambda s: None) == 0
    assert hc.pings == [] and "titan2-selfcheck.service" in tg.sent[-1]
    for text in ("unit titan2-bot.service failed", "unit titan2-bot.timer failed", "titan2-bot will not start: x"):
        assert alerts.critical_now(text, telegram=tg, healthcheck=hc, sleep=lambda s: None) == 0
        assert hc.pings[-1] == ("fail", text)
    assert alerts.critical_now("unit titan2-backup.service failed", telegram=FakeTelegram(fail=True), healthcheck=hc,
                               sleep=lambda s: None) == 2                     # Telegram was its only channel
    assert alerts.critical_now("unit titan2-backup.service failed", telegram=tg, healthcheck=hc, deadman=True,
                               sleep=lambda s: None) == 0 and len(hc.pings) == 4
    assert not alerts.is_bot_failure("unit titan2-dash.service failed") and alerts.is_bot_failure("crash")


class _Resp:
    def __init__(self, status: int = 200, payload: object = None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_transports_build_requests_and_redact_secrets():
    calls = []

    def post(url, data=None, timeout=None):
        calls.append((url, data, timeout))
        return _Resp(200, {"ok": True})

    tg = alerts.Telegram("123456:SECRET-bot-token", "-1001", post=post)
    tg.send("hello")
    url, data, timeout = calls[0]
    assert url == "https://api.telegram.org/bot123456:SECRET-bot-token/sendMessage" and timeout == alerts.HTTP_TIMEOUT_S
    assert data["chat_id"] == "-1001" and data["text"] == "hello" and "parse_mode" not in data
    assert "SECRET" not in repr(tg)

    def broken(url, data=None, timeout=None):
        raise ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: {url}")

    with pytest.raises(alerts.AlertError) as err:
        alerts.Telegram("123456:SECRET-bot-token", "-1001", post=broken).send("x")
    assert "SECRET-bot-token" not in str(err.value) and err.value.__cause__ is None
    with pytest.raises(alerts.AlertError, match="HTTP 401"):
        alerts.Telegram("1:tok", "1", post=lambda url, **k: _Resp(401, {"ok": False})).send("x")
    with pytest.raises(alerts.AlertError):
        alerts.Telegram("1:tok", "1", post=lambda url, **k: _Resp(200, None)).send("x")    # not JSON

    pings = []
    hc = alerts.Healthcheck("https://hc-ping.com/0f1e2d3c-SECRET-uuid/", post=lambda url, data=None, timeout=None:
                            pings.append((url, data)) or _Resp(200))
    hc.ping()
    hc.ping("fail", "exit 78")
    hc.ping("log", "self-check")
    assert pings == [("https://hc-ping.com/0f1e2d3c-SECRET-uuid", b""),
                     ("https://hc-ping.com/0f1e2d3c-SECRET-uuid/fail", b"exit 78"),
                     ("https://hc-ping.com/0f1e2d3c-SECRET-uuid/log", b"self-check")]
    with pytest.raises(ValueError):
        hc.ping("restart")
    with pytest.raises(alerts.AlertError) as err:
        alerts.Healthcheck("https://hc-ping.com/0f1e2d3c-SECRET-uuid", post=broken).ping()
    assert "SECRET-uuid" not in str(err.value)

    def path_only(url, data=None, timeout=None):            # requests shows only the path of the failing URL
        raise ConnectionError(f"Max retries exceeded with url: /{url.split('/', 3)[3]} (Caused by timeout)")

    with pytest.raises(alerts.AlertError) as err:
        alerts.Healthcheck("https://hc-ping.com/PINGKEY-secret77/titan2-bot", post=path_only).ping("fail", "x")
    assert "PINGKEY" not in str(err.value) and "titan2-bot" not in str(err.value)

    assert alerts.Telegram.from_env({}) is None and alerts.Healthcheck.from_env({}) is None
    with pytest.raises(ValueError):
        alerts.Healthcheck.from_env({"HEALTHCHECK_URL": "http://insecure.example/x"})
    with pytest.raises(ValueError):
        alerts.Telegram.from_env({"TELEGRAM_BOT_TOKEN": "tok"})                         # chat id missing
    bot = alerts.Alerter.from_env({"TELEGRAM_BOT_TOKEN": "1:tok", "TELEGRAM_CHAT_ID": "9",
                                   "HEALTHCHECK_URL": "http://not-https/x"})
    assert bot.telegram is not None and bot.healthcheck is None                        # bad channel off, bot starts


def test_alerter_thread_flushes_on_stop_and_logs_without_telegram(caplog):
    tg, hc = FakeTelegram(), FakeHC()
    a = alerts.Alerter(tg, hc)
    a.start()
    with pytest.raises(RuntimeError):
        a.start()
    a.submit("CRITICAL", "DB_CORRUPT", "integrity_check failed; exiting 78")
    a.fail("exit 78")
    a.stop(timeout_s=5)
    assert any("DB_CORRUPT" in m for m in tg.sent) and ("fail", "exit 78") in hc.pings

    caplog.set_level(logging.INFO, logger="titan2.alerts")
    logonly = alerts.Alerter(None, None, clock=FakeClock())
    logonly.submit("WARN", "NO_BID", "no bid on leg 3")
    assert logonly.step() == ["log_only"]
    assert "no bid on leg 3" in caplog.text and "Telegram is not configured" in caplog.text

    failing = alerts.Alerter(FakeTelegram(fail=True), None, clock=FakeClock(), sleep=lambda s: None)
    failing.submit("WARN", "X", "kept in the log")
    failing.step()
    assert failing.counters()["send_failed"] == 1 and "kept in the log" in caplog.text


def test_send_now_and_critical_now():
    tg, hc, waits = FakeTelegram(), FakeHC(), []
    assert alerts.send_now("WARN", "TOKEN_LOGIN_NEEDED", "log in", telegram=tg, sleep=waits.append)
    assert tg.sent[0].startswith("[titan2] WARN TOKEN_LOGIN_NEEDED") and "ack:" not in tg.sent[0]
    assert alerts.send_now("INFO", "X", "y", env={}) is False                       # not configured: logged only
    bad = FakeTelegram(fail=True)
    assert alerts.send_now("CRITICAL", "X", "y", telegram=bad, sleep=waits.append) is False
    assert waits == list(alerts.SEND_BACKOFF_S)                                     # 3 tries, backing off
    assert alerts.critical_now("unit titan2-bot.service failed", telegram=tg, healthcheck=hc, sleep=waits.append) == 0
    assert hc.pings[-1] == ("fail", "unit titan2-bot.service failed")
    assert alerts.critical_now("x", telegram=bad, healthcheck=hc, sleep=waits.append) == 0     # one channel is enough
    assert alerts.critical_now("x", env={}, sleep=waits.append) == 2                           # nothing delivered


# ---------------------------------------------------------------------------------------------------------------
# token.py
# ---------------------------------------------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status: int = 200, payload: object = None, headers: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeKiteWeb:
    """requests.Session stand-in for Kite's login pages and the Kite Connect redirect chain."""

    def __init__(self, *, login_status: int = 200, twofa_failures: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.login_status = login_status
        self.twofa_failures = twofa_failures

    def post(self, url, data=None, timeout=None, allow_redirects=True):
        self.calls.append(("POST", url))
        if url == token.KITE_LOGIN_URL:
            assert data == {"user_id": USER, "password": PASSWORD}
            if self.login_status != 200:
                return FakeResp(self.login_status, {"status": "error", "message": "Invalid username or password."})
            return FakeResp(200, {"status": "success", "data": {"request_id": "rq-77", "twofa_type": "totp"}})
        if url == token.KITE_TWOFA_URL:
            assert data == {"user_id": USER, "request_id": "rq-77", "twofa_value": TOTP_CODE, "twofa_type": "totp"}
            if self.twofa_failures:
                self.twofa_failures -= 1
                return FakeResp(400, {"status": "error", "message": "Invalid TOTP."})
            return FakeResp(200, {"status": "success", "data": {}})
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, timeout=None, allow_redirects=True):
        self.calls.append(("GET", url))
        assert allow_redirects is False
        if url == f"{token.KITE_CONNECT_LOGIN}?api_key={API_KEY}&v=3":
            return FakeResp(302, headers={"Location": f"https://kite.zerodha.com/connect/login?api_key={API_KEY}"
                                                      "&sess_id=S1"})
        if url == f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&sess_id=S1":
            return FakeResp(302, headers={"Location": "/connect/finish?sess_id=S1"})      # a relative redirect
        if url == "https://kite.zerodha.com/connect/finish?sess_id=S1":
            return FakeResp(302, headers={"Location": "https://127.0.0.1/cb?action=login&status=success"
                                                      f"&request_token={REQUEST_TOKEN}"})
        raise AssertionError(f"unexpected GET {url}")                 # the app's redirect URL is never fetched


class FakeKiteWorld:
    """KiteConnect stand-in: generate_session issues ACCESS_TOKEN; profile() works only with an issued token."""

    def __init__(self, user: str = USER) -> None:
        self.user = user
        self.valid: set[str] = set()
        self.sessions = 0

    def factory(self, api_key, access_token=None):
        assert api_key == API_KEY
        world = self

        class Client:
            def generate_session(self, request_token, api_secret):
                assert request_token == REQUEST_TOKEN and api_secret == API_SECRET
                world.sessions += 1
                world.valid.add(ACCESS_TOKEN)
                return {"access_token": ACCESS_TOKEN, "user_id": world.user, "login_time": None}

            def profile(self):
                if access_token not in world.valid:
                    raise RuntimeError("TokenException: Incorrect `api_key` or `access_token`.")
                return {"user_id": world.user, "user_name": "Test User"}

        return Client()


def token_env(**over: str) -> dict[str, str]:
    env = {"ZERODHA_API_KEY": API_KEY, "ZERODHA_API_SECRET": API_SECRET, "ZERODHA_USER_ID": USER,
           "ZERODHA_PASSWORD": PASSWORD, "ZERODHA_TOTP_SECRET": TOTP_SECRET, "TOKEN_MODE": "auto"}
    env.update(over)
    return env


def run_token(path: Path, *, world: FakeKiteWorld | None = None, web: FakeKiteWeb | None = None,
              notes: list | None = None, sleeps: list | None = None, now: datetime = NOW, **kwargs) -> int:
    notes = [] if notes is None else notes
    sleeps = [] if sleeps is None else sleeps
    return token.run(env=kwargs.pop("env", token_env()), path=path, now=lambda: now, http=web or FakeKiteWeb(),
                     totp=lambda secret: TOTP_CODE if secret == TOTP_SECRET else "000000",
                     kite_factory=(world or FakeKiteWorld()).factory, sleep=sleeps.append,
                     notify=lambda sev, code, text: notes.append((sev, code, text)) or True, **kwargs)


def test_session_file_0600(tmp_path, monkeypatch):
    path = token.session_path(tmp_path)
    sess = token.new_session(ACCESS_TOKEN, USER, NOW)
    opened = []
    real_open = os.open

    def spy(p, flags, mode=0o777, *args, **kwargs):
        opened.append((Path(p), flags, mode))
        return real_open(p, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    old_umask = os.umask(0)                                  # even a permissive umask cannot widen the mode
    try:
        assert token.write_session(sess, path) == path
    finally:
        os.umask(old_umask)
    created = [o for o in opened if o[0].name.endswith(".tmp")]
    assert len(created) == 1
    tmp, flags, mode = created[0]
    assert mode == 0o600 and tmp.parent == path.parent
    assert flags & os.O_CREAT and flags & os.O_EXCL and flags & os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        assert flags & os.O_NOFOLLOW
    assert sorted(p.name for p in path.parent.iterdir()) == ["kite_session.json"]   # no temp file left behind
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == set(token.SESSION_KEYS)
    assert data == {"access_token": ACCESS_TOKEN, "user_id": USER, "issued_at": "2026-10-12T08:30:05.000+05:30",
                    "valid_until": "2026-10-13T06:00:00.000+05:30"}
    assert token.read_session(path) == sess

    # rewriting replaces the file in one step and keeps it 0600
    newer = token.new_session("AcTokNewer000111222", USER, NOW + timedelta(hours=1))
    token.write_session(newer, path)
    assert token.read_session(path) == newer
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    # a failed write leaves the previous session intact and no temp file
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        token.write_session(sess, path)
    monkeypatch.undo()
    assert token.read_session(path) == newer
    assert sorted(p.name for p in path.parent.iterdir()) == ["kite_session.json"]


def _docstring_ids(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def test_token_never_restarts(tmp_path, monkeypatch):
    # 1. The module has no means to start, stop, restart or signal anything.
    tree = ast.parse(Path(token.__file__).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not modules & {"subprocess", "signal", "multiprocessing", "pty", "asyncio", "shlex", "sh"}
    dangerous = {"system", "popen", "kill", "killpg", "fork", "forkpty", "startfile", "posix_spawn",
                 "posix_spawnp"} | {n for n in dir(os) if n.startswith(("exec", "spawn"))}
    assert not {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} & dangerous
    docs = _docstring_ids(tree)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in docs]
    assert not [s for s in strings if "systemctl" in s or "restart" in s.lower() or "service" in s.lower()]

    # 2. A full refresh, and a failed one, never run a process and never touch the env file.
    spawned = []

    def forbidden(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("the token unit tried to run a process")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("system", "kill"):
        monkeypatch.setattr(os, name, forbidden)
    env_file = tmp_path / "etc" / "titan2.env"
    env_file.parent.mkdir()
    env_file.write_text("".join(f"{k}={v}\n" for k, v in token_env().items()), encoding="utf-8")
    before = env_file.read_bytes()
    env = token.load_env(env_file, environ={})
    notes: list = []
    path = token.session_path(tmp_path / "var")
    world = FakeKiteWorld()
    assert run_token(path, world=world, env=env, notes=notes) == 0
    assert notes == [] and world.sessions == 1 and token.read_session(path).access_token == ACCESS_TOKEN
    assert run_token(token.session_path(tmp_path / "var2"), web=FakeKiteWeb(twofa_failures=9), env=env,
                     notes=notes) == 2
    assert notes[-1][:2] == ("CRITICAL", "TOKEN_REFRESH_FAILED")
    assert not spawned
    assert env_file.read_bytes() == before
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert files == ["etc/titan2.env", "var/session/kite_session.json"]


def test_token_auto_flow_redirects_and_never_logs_secrets(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    web, world, notes = FakeKiteWeb(), FakeKiteWorld(), []
    path = token.session_path(tmp_path)
    assert run_token(path, web=web, world=world, notes=notes) == 0
    assert [c[1].split("?")[0] for c in web.calls] == [
        token.KITE_LOGIN_URL, token.KITE_TWOFA_URL, token.KITE_CONNECT_LOGIN,
        "https://kite.zerodha.com/connect/login", "https://kite.zerodha.com/connect/finish"]
    sess = token.read_session(path)
    assert sess.user_id == USER and sess.is_fresh(NOW) and sess.valid_until == datetime(2026, 10, 13, 6, tzinfo=IST)

    # a still-valid session is reused: no login at all
    web2 = FakeKiteWeb()
    assert run_token(path, web=web2, world=world) == 0 and web2.calls == [] and world.sessions == 1

    # failures, to put error texts in the log too
    assert run_token(token.session_path(tmp_path / "f1"), web=FakeKiteWeb(twofa_failures=9), notes=notes) == 2
    assert run_token(token.session_path(tmp_path / "f2"), request_token="!!bad!!", notes=notes) == 2
    texts = caplog.text + "".join(n[2] for n in notes) + repr(sess) + repr(token.Credentials.from_env(token_env()))
    for secret in SECRETS:
        assert secret not in texts, f"secret {secret!r} leaked"
    assert sess.fingerprint in caplog.text


def test_token_retry_policy(tmp_path):
    # a TOTP rejected at the edge of its window is retried: 3 attempts, 20 s apart
    sleeps, notes = [], []
    web = FakeKiteWeb(twofa_failures=2)
    assert run_token(token.session_path(tmp_path / "a"), web=web, sleeps=sleeps, notes=notes) == 0
    assert sleeps == [token.LOGIN_RETRY_S] * 2 and notes == []
    # every attempt fails -> CRITICAL, exit 2, the old session (if any) is left alone
    sleeps, notes = [], []
    assert run_token(token.session_path(tmp_path / "b"), web=FakeKiteWeb(twofa_failures=9), sleeps=sleeps,
                     notes=notes) == 2
    assert sleeps == [token.LOGIN_RETRY_S] * 2 and notes[0][:2] == ("CRITICAL", "TOKEN_REFRESH_FAILED")
    # a rejected password is never retried (repeated bad logins can lock the account)
    web, sleeps = FakeKiteWeb(login_status=400), []
    assert run_token(token.session_path(tmp_path / "c"), web=web, sleeps=sleeps) == 2
    assert web.calls == [("POST", token.KITE_LOGIN_URL)] and sleeps == []
    # a session for another account is refused and never saved
    path = token.session_path(tmp_path / "d")
    notes = []
    assert run_token(path, world=FakeKiteWorld(user="ZZ9999"), notes=notes) == 2
    assert not path.exists() and "ZZ9999" in notes[0][2]
    # missing credentials are named (never valued)
    notes = []
    env = token_env()
    del env["ZERODHA_TOTP_SECRET"]
    assert run_token(token.session_path(tmp_path / "e"), env=env, notes=notes) == 2
    assert "ZERODHA_TOTP_SECRET" in notes[0][2]
    assert run_token(token.session_path(tmp_path / "f"), env=token_env(TOKEN_MODE="sometimes")) == 2


def test_token_manual_mode_and_request_token(tmp_path):
    path = token.session_path(tmp_path)
    notes, web, world = [], FakeKiteWeb(), FakeKiteWorld()
    env = token_env(TOKEN_MODE="manual")
    del env["ZERODHA_PASSWORD"], env["ZERODHA_TOTP_SECRET"]           # manual mode needs neither
    assert run_token(path, env=env, web=web, world=world, notes=notes) == 0
    assert web.calls == [] and not path.exists()
    sev, code, text = notes[0]
    assert (sev, code) == ("WARN", "TOKEN_LOGIN_NEEDED")
    assert f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3" in text
    assert "titan2 token --request-token" in text
    # the operator pastes the request_token: exchanged on the server, saved, verified
    assert run_token(path, env=env, web=web, world=world, request_token=f"  {REQUEST_TOKEN}\n") == 0
    assert token.read_session(path).access_token == ACCESS_TOKEN and web.calls == []
    # Telegram down in manual mode: the unit fails so OnFailure alerts
    assert token.run(env=env, path=token.session_path(tmp_path / "x"), now=lambda: NOW,
                     kite_factory=world.factory, notify=lambda *a: False) == 2


def test_session_validity_and_reading(tmp_path):
    assert token.next_day_start(datetime(2026, 10, 12, 5, 59, tzinfo=IST)) == datetime(2026, 10, 12, 6, tzinfo=IST)
    assert token.next_day_start(datetime(2026, 10, 12, 6, 0, tzinfo=IST)) == datetime(2026, 10, 13, 6, tzinfo=IST)
    s = token.new_session(ACCESS_TOKEN, USER, datetime(2026, 10, 12, 8, 30, tzinfo=IST))
    assert s.is_fresh(datetime(2026, 10, 12, 15, 0, tzinfo=IST))
    assert not s.is_fresh(datetime(2026, 10, 13, 8, 30, tzinfo=IST))           # yesterday's
    early = token.new_session(ACCESS_TOKEN, USER, datetime(2026, 10, 12, 5, 50, tzinfo=IST))
    assert not early.is_fresh(datetime(2026, 10, 12, 8, 30, tzinfo=IST))       # expired at 06:00
    with pytest.raises(token.TokenError):
        token.new_session("", USER, NOW)

    p = tmp_path / "kite_session.json"
    with pytest.raises(token.SessionError, match="no session file"):
        token.read_session(p)
    bad_docs = ["{not json", "[]", json.dumps({"access_token": ACCESS_TOKEN}),
                json.dumps({**s.to_json(), "issued_at": "2026-10-12 08:30:00"}),           # naive time
                json.dumps({**s.to_json(), "valid_until": s.to_json()["issued_at"]}),
                json.dumps({**s.to_json(), "access_token": "has spaces " + ACCESS_TOKEN})]
    for doc in bad_docs:
        p.write_text(doc, encoding="utf-8")
        with pytest.raises(token.SessionError) as err:
            token.read_session(p)
        assert ACCESS_TOKEN not in str(err.value)


def test_token_recheck_and_env_loading(tmp_path):
    path = token.session_path(tmp_path)
    world = FakeKiteWorld()
    ok, detail = token.token_recheck(path, API_KEY, None, kite_factory=world.factory)
    assert not ok and "no session file" in detail
    sess = token.new_session(ACCESS_TOKEN, USER, NOW)
    token.write_session(sess, path)
    ok, detail = token.token_recheck(path, API_KEY, sess.fingerprint, kite_factory=world.factory)
    assert not ok and "profile() failed" in detail and ACCESS_TOKEN not in detail          # token not working
    world.valid.add(ACCESS_TOKEN)
    prep = token.make_token_prep(path, API_KEY, sess.fingerprint, kite_factory=world.factory)
    assert prep() == (True, f"SAME fp={sess.fingerprint}")
    assert token.token_recheck(path, API_KEY, "0123456789ab", kite_factory=world.factory) == \
        (True, f"NEW fp={sess.fingerprint}")

    env_file = tmp_path / "titan2.env"
    env_file.write_text("# comment\n\nZERODHA_API_KEY = abc\nexport TOKEN_MODE=manual\nQUOTED=\"a b\"\n"
                        "SINGLE='x'\nnot a line\nBAD-KEY=1\nEMPTY=\n", encoding="utf-8")
    env = token.load_env(env_file, environ={"TOKEN_MODE": "auto"})
    assert env == {"ZERODHA_API_KEY": "abc", "TOKEN_MODE": "auto", "QUOTED": "a b", "SINGLE": "x", "EMPTY": ""}
    assert token.load_env(tmp_path / "missing.env", environ={"A": "1"}) == {"A": "1"}


# ---------------------------------------------------------------------------------------------------------------
# selfcheck.py
# ---------------------------------------------------------------------------------------------------------------
# DETAIL §13.7, copied independently of the code: row -> (morning, prestart, deploy, cutover; effect of a FAIL).
EXPECTED_MATRIX = {
    "token":        ("MP-C", "HALT TOKEN GLOBAL AUTO"),
    "ntp":          ("MP-C", "HALT CLOCK_UNSYNCED GLOBAL AUTO"),
    "calendar":     ("MPDC", "WARN"),
    "instruments":  ("MP-C", "HALT NO_INSTRUMENTS GLOBAL AUTO"),
    "ban_list":     ("MP--", "WARN"),
    "prev_bars":    ("MP--", "WARN"),
    "settings":     ("MPDC", "HALT SETTINGS_INVALID GLOBAL AUTO"),
    "db":           ("MPDC", "EXIT78"),
    "resources":    ("MPDC", "WARN"),
    "v1_disabled":  ("MPDC", "HALT V1_ACTIVE GLOBAL AUTO"),
    "code_clean":   ("MPDC", "EXIT78"),                  # + morning: a shadowing module is found at 08:40
    "env_files":    ("MPDC", "CRITICAL"),
    "broker_flat":  ("M--C", "HALT UNKNOWN_POSITION ACCOUNT:LIVE ACK"),        # prestart: recon covers it
    "egress_ip":    ("MP-C", "HALT LIVE_DISABLED_TODAY ACCOUNT:LIVE NEXT_SESSION"),
    "live_ready":   ("MP--", "HALT LIVE_DISABLED_TODAY ACCOUNT:LIVE NEXT_SESSION"),
    "rates":        ("M-D-", "WARN"),
    "prev_session": ("MP--", "CRITICAL"),
    "notify":       ("M--C", "WARN"),
}
PHASE_LETTER = {"morning": "M", "prestart": "P", "deploy": "D", "cutover": "C"}
CRITICAL_ROWS = {"token", "instruments", "settings", "db", "v1_disabled", "code_clean", "env_files", "broker_flat",
                 "egress_ip", "live_ready", "prev_session"}
GOOD_NOW = datetime(2026, 10, 12, 8, 40, tzinfo=IST)          # Monday; previous session Friday 2026-10-09
BAD_NOW = datetime(2026, 12, 14, 8, 40, tzinfo=IST)           # calendar coverage ends 2026-12-31: < 30 days ahead
PREV = date(2026, 10, 9)
UIDS = {0: "root", 1001: "titan", 1002: "titandash"}


def _st(mode: int, uid: int, gid: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)


class FakeSystem:
    """The host: systemd, cron, NTP, files, users, disk, memory and the egress IP, all scripted."""

    def __init__(self, good: bool, var: Path) -> None:
        self.good = good
        self.calls: list[tuple] = []
        sess = token.session_path(var)
        bot_env = "ZERODHA_API_KEY=k\nALLOW_LIVE=0\n" + ("" if good else "ZERODHA_ACCESS_TOKEN=abc\n")
        self.files: dict[Path, tuple] = {
            selfcheck.ENV_FILE: (_st(0o100640 if good else 0o100644, 0, 1001), bot_env),
            selfcheck.DASH_ENV_FILE: (_st(0o100640, 0, 1002), PermissionError),
            sess: (_st(0o100600, 1001, 1001), None),
            sess.parent: (_st(0o040700, 1001, 1001), None),
            Path("/etc/crontab"): (None, "SHELL=/bin/sh\n17 * * * * root cd / && run-parts /etc/cron.hourly\n"),
            Path("/etc/cron.d/e2scrub_all"): (None, "# e2scrub\n30 3 * * 0 root /usr/lib/e2scrub_all_cron\n"),
        }

    def run(self, cmd, timeout, cwd=None):
        cmd = list(cmd)
        self.calls.append(tuple(cmd))
        good = self.good
        if cmd[0] == "timedatectl":
            return (0, "yes\n") if good else (0, "no\n")
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return (1, "masked\n") if good else (0, "enabled\n")
        if cmd[:2] == ["systemctl", "is-active"]:
            return (3, "inactive\n") if good else (0, "active\n")
        if cmd[0] == "crontab":
            return (1, "no crontab for titan\n") if good else \
                (0, "30 8 * * 1-5 cd /home/ubuntu/titan/agentic_trader && python kite_token_manager.py\n")
        if cmd[1:3] == ["-c", selfcheck.FRESH_CHECK]:
            assert timeout >= 60
            return (0, "TITAN2_FRESH_OK\n") if good else (1, "Traceback ...\nImportError: cannot import name x\n")
        raise AssertionError(f"unexpected command {cmd}")

    def read_text(self, path):
        entry = self.files.get(Path(path))
        if entry is None or entry[1] is None:
            raise FileNotFoundError(str(path))
        if entry[1] is PermissionError:
            raise PermissionError(str(path))
        return entry[1]

    def list_dir(self, path):
        if Path(path) == Path("/etc/cron.d"):
            return [Path("/etc/cron.d/e2scrub_all")]
        raise PermissionError(str(path))                     # /var/spool/cron/crontabs: other users' crontabs

    def stat(self, path):
        entry = self.files.get(Path(path))
        if entry is None or entry[0] is None:
            raise FileNotFoundError(str(path))
        return entry[0]

    def user_name(self, uid):
        return UIDS[uid]

    def group_name(self, gid):
        return UIDS[gid]

    def current_user(self):
        return "titan"

    def user_groups(self, user):
        assert user == "titandash"
        return {"titandash"} if self.good else {"titandash", "titan"}

    def disk_free(self, path):
        return 50 * GB if self.good else 1 * GB

    def mem_available(self):
        return 2 * GB if self.good else 100 * MB

    def egress_ip(self):
        return "203.0.113.7" if self.good else "198.51.100.9"


class FakeKiteData:
    def __init__(self, good: bool) -> None:
        self.good = good

    def profile(self):
        return {"user_id": USER, "user_name": "Test User"}

    def positions(self, account):
        assert account == Account.LIVE
        return [] if self.good else [types.SimpleNamespace(exchange="NFO", net_qty=65, product="NRML",
                                                           tradingsymbol="NIFTY26OCT25000CE")]

    def orders_today(self, account):
        return [] if self.good else [types.SimpleNamespace(status="WORKING", tradingsymbol="NIFTY26OCT25000PE",
                                                           side="BUY", qty=65, raw_status="AMO REQ RECEIVED")]

    def margins(self, account):
        return Margins(Account.LIVE, 100000.0 if self.good else 10000.0, {})


class FakeInstruments:
    def __repr__(self) -> str:
        return "InstrumentStore(session=2026-10-12, contracts=41250, stocks=180, indices=2)"


def _raise(message: str):
    def provider():
        raise RuntimeError(message)
    return provider


def make_db(var: Path, *, eod_done: bool, live_state: str | None, user_version: int | None = None) -> Path:
    path = var / "db" / store_mod.DB_FILENAME
    st = store_mod.Store(path)
    try:
        with store_mod.core_thread(), st.tx():
            close = datetime.combine(PREV, dtime(15, 40), tzinfo=IST)
            st.append_event("BOOT", wall=close - timedelta(hours=7), mono=1.0)
            if eod_done:
                st.append_event(selfcheck.EOD_DONE_KIND, wall=close, mono=2.0, payload={"phase": "DONE"})
            if live_state is not None:
                st.put_strategy_mode("opening_drive", version="1.abc", mode="LIVE", changed_wall=close,
                                     changed_by="test")
                st.register_trial("opening_drive", "1.abc", close, {"v": 1})
                if live_state != "COLLECTING":
                    st.update_trial("opening_drive", "1.abc", state=live_state, state_ts=close)
    finally:
        st.close()
    if user_version is not None:
        con = sqlite3.connect(path)
        con.execute(f"PRAGMA user_version={int(user_version)}")
        con.commit()
        con.close()
    return path


def context_kwargs(tmp_path: Path, good: bool) -> dict:
    var = tmp_path / ("good" if good else "bad")
    app = var / "app"
    (app / "titan2").mkdir(parents=True)
    if not good:
        (app / "json.py").write_text("HARD_RULES = {}\n", encoding="utf-8")         # shadows the stdlib's json
        cfg = var / "config" / "settings.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"MAX_LOTS": 5}', encoding="utf-8")                        # looser than LIMITS
    make_db(var, eod_done=good, live_state="CERTIFIED" if good else "COLLECTING", user_version=None if good else 99)
    now = GOOD_NOW if good else BAD_NOW
    issued = now.replace(hour=8, minute=30) - (timedelta(0) if good else timedelta(days=1))
    return dict(
        env={"ZERODHA_USER_ID": USER, "ZERODHA_API_KEY": API_KEY, "REGISTERED_EGRESS_IP": "203.0.113.7",
             "ALLOW_LIVE": "0"},
        now=now, var_dir=var, app_dir=app, system=FakeSystem(good, var), python="python3",
        db_copy=None if good else var / "missing-copy.db",
        session=token.new_session(ACCESS_TOKEN, USER, issued), kite=FakeKiteData(good),
        instruments=FakeInstruments() if good else _raise("InstrumentsError: NIFTY lists CE/PE for 1 expiry"),
        ban_list=types.SimpleNamespace(ok=True, source="CACHE", detail="2 banned (cached)") if good else
        types.SimpleNamespace(ok=False, source="MISSING", detail="fetch failed: HTTP 403"),
        bars=(190, 200) if good else (10, 200),
        telegram=FakeTelegram(fail=not good), healthcheck=FakeHC(fail=not good))


def _phases(spec: str) -> set[str]:
    return {phase for phase, letter in PHASE_LETTER.items() if letter in spec}


def test_selfcheck_matrix(tmp_path, monkeypatch):
    # The table itself: rows, order, phases and effects are DETAIL §13.7.
    assert [c.name for c in selfcheck.CHECKS] == list(EXPECTED_MATRIX)
    for check in selfcheck.CHECKS:
        phases, effect = EXPECTED_MATRIX[check.name]
        assert set(check.phases) == _phases(phases), check.name
        e = check.effect
        if effect.startswith("HALT"):
            _, kind, scope, clears = effect.split()
            assert (e.status, e.halt, e.exit_code) == (selfcheck.FAIL, (kind, scope, HaltClears(clears)), None)
        elif effect == "EXIT78":
            assert (e.status, e.halt, e.exit_code) == (selfcheck.FAIL, None, 78)
        elif effect == "CRITICAL":
            assert (e.status, e.halt, e.exit_code, e.alert) == (selfcheck.FAIL, None, None, "CRITICAL")
        else:
            assert (e.status, e.halt, e.exit_code, e.alert) == (selfcheck.WARN, None, None, "WARN"), check.name
        assert e.alert == ("CRITICAL" if check.name in CRITICAL_ROWS else "WARN"), check.name

    for phase in selfcheck.PHASES:
        expected_rows = [n for n, (p, _) in EXPECTED_MATRIX.items() if PHASE_LETTER[phase] in p]

        # Everything healthy: every row passes (or does not apply), nothing is raised.
        good_kw = context_kwargs(tmp_path / phase, good=True)
        good = selfcheck.run_phase(phase, selfcheck.Context(phase, **good_kw))
        assert [r.name for r in good.results] == expected_rows
        assert {r.name: r.status for r in good.results if r.status != selfcheck.PASS} == {}, good.summary(True)
        assert good.ok and good.exit_code == 0 and not good.fatal
        assert good.halts() == [] and good.alert_commands() == [] and good.demotions() == []
        assert good.severity() == "INFO"
        json.dumps(good.to_json())
        if phase in ("morning", "cutover"):
            assert len(good_kw["telegram"].sent) == 1 and good_kw["healthcheck"].pings[0][0] == "log"
        if phase in ("morning", "prestart", "deploy", "cutover"):
            fresh = [c for c in good_kw["system"].calls if c[1:2] == ("-c",)]
            assert fresh and fresh[0][0] == "python3"

        # Everything broken: each row fails with exactly its matrix effect.
        with monkeypatch.context() as m:
            m.setattr(charges, "RATES_VERIFIED_ON", date(2026, 1, 1))    # the bad run's rates are then 257 days old
            bad = selfcheck.run_phase(phase, selfcheck.Context(phase, **context_kwargs(tmp_path / phase, good=False)))
        assert [r.name for r in bad.results] == expected_rows
        for r in bad.results:
            status = selfcheck.WARN if EXPECTED_MATRIX[r.name][1] == "WARN" else selfcheck.FAIL
            assert r.status == status, (phase, r.name, r.detail)
            assert r.effect is not None and r.effect.note
        assert not bad.ok and bad.exit_code == 2 and bad.fatal
        assert bad.severity() == "CRITICAL"
        assert {(a.code, a.severity) for a in bad.alert_commands()} == {
            (f"SELFCHECK_{n.upper()}", "CRITICAL" if n in CRITICAL_ROWS else "WARN") for n in expected_rows}
        assert all(a.dedup_key == f"selfcheck:{a.code[10:].lower()}" for a in bad.alert_commands())
        halts = {(h.kind, h.scope, h.clears) for h in bad.halts()}
        want = set()
        for n in expected_rows:
            effect = EXPECTED_MATRIX[n][1]
            if effect.startswith("HALT"):
                _, kind, scope, clears = effect.split()
                want.add((kind, scope, HaltClears(clears)))
        assert halts == want
        assert len(bad.halts()) == len(want)                     # one halt per (kind, scope)
        if phase in ("morning", "prestart"):
            assert bad.demotions() == ["opening_drive"]
            live = next(h for h in bad.halts() if h.kind == "LIVE_DISABLED_TODAY")
            assert "egress_ip" in live.reason and "live_ready" in live.reason and len(live.reason) <= 300
        json.dumps(bad.to_json())

    # The prestart halts are the matrix's entry halts; the exit-78 rows are reported as fatal, not as halts.
    prestart = selfcheck.run_phase("prestart", selfcheck.Context("prestart", **context_kwargs(tmp_path / "p2", False)))
    assert sorted(h.kind for h in prestart.halts()) == ["CLOCK_UNSYNCED", "LIVE_DISABLED_TODAY", "NO_INSTRUMENTS",
                                                        "SETTINGS_INVALID", "TOKEN", "V1_ACTIVE"]
    assert {r.name for r in prestart.results if r.effect and r.effect.exit_code == 78} == {"db", "code_clean"}

    # Integration with the engine's halt catalog (core/risk.HALT_RULES): every prestart halt is one the core can
    # raise as is (same kind, scope and clear rule), and the Report reaches the core in a SelfcheckResult event.
    from titan2.core import risk
    from titan2.core.events import RunSelfcheck, SelfcheckResult

    for h in prestart.halts():
        assert risk.make_halt(h.kind, h.scope, h.reason).clears == h.clears, h
    assert RunSelfcheck().phase == selfcheck.PRESTART
    done = SelfcheckResult(RunSelfcheck().phase, prestart.ok, prestart)
    assert done.report.halts() == prestart.halts() and not done.ok


def test_selfcheck_row_details(tmp_path, monkeypatch):
    kw = context_kwargs(tmp_path, good=True)

    def run(phase: str = "morning", **over) -> selfcheck.Report:
        return selfcheck.run_phase(phase, selfcheck.Context(phase, **{**kw, **over}))

    def row(report: selfcheck.Report, name: str) -> selfcheck.CheckResult:
        return report.result(name)

    # egress IP: unset is fine while LIVE is off, never when ALLOW_LIVE=1 (a static IP is a LIVE prerequisite)
    env = dict(kw["env"], REGISTERED_EGRESS_IP="")
    assert row(run(env=env), "egress_ip").status == selfcheck.SKIP
    assert row(run(env=dict(env, ALLOW_LIVE="1")), "egress_ip").status == selfcheck.FAIL
    # live readiness: certified but short of margin -> LIVE off today, no demotion
    short = types.SimpleNamespace(**{n: getattr(FakeKiteData(True), n) for n in ("profile", "positions",
                                                                                 "orders_today")})
    short.margins = lambda account: Margins(Account.LIVE, 59999.0, {})
    r = run(kite=short)
    assert row(r, "live_ready").status == selfcheck.FAIL and r.demotions() == []
    assert [h.kind for h in r.halts()] == ["LIVE_DISABLED_TODAY"]
    # a resource that cannot be produced fails every row that needs it (fail closed), with the reason
    r = run(kite=_raise("ReadFailed: FATAL:TokenException(403)"))
    for name in ("token", "broker_flat", "live_ready"):
        assert row(r, name).status == selfcheck.FAIL and "TokenException" in row(r, name).detail
    # a v1 unit that no longer exists is as good as masked; no systemctl at all cannot be verified
    sysm = FakeSystem(True, kw["var_dir"])
    sysm.run = lambda cmd, timeout, cwd=None: (1, "Failed to get unit file state for titan-bot.service: "
                                                  "No such file or directory\n") if cmd[1] == "is-enabled" else \
        (3, "inactive\n") if cmd[1] == "is-active" else (1, "no crontab for titan\n")
    assert row(run(system=sysm), "v1_disabled").status == selfcheck.PASS
    sysm.run = lambda cmd, timeout, cwd=None: (127, f"{cmd[0]}: not found")
    assert row(run(system=sysm), "v1_disabled").status == selfcheck.FAIL
    # a readable dash.env with broker keys fails; a session file readable by others fails
    sysm = FakeSystem(True, kw["var_dir"])
    sysm.files[selfcheck.DASH_ENV_FILE] = (_st(0o100640, 0, 1002), "DASH_USER=a\nZERODHA_API_SECRET=x\n")
    assert "ZERODHA_API_SECRET" in row(run(system=sysm), "env_files").detail
    sysm = FakeSystem(True, kw["var_dir"])
    sess = token.session_path(kw["var_dir"])
    sysm.files[sess] = (_st(0o100644, 1001, 1001), None)
    assert row(run(system=sysm), "env_files").status == selfcheck.FAIL
    # secrets never reach a detail
    leaky = dict(kw["env"], ZERODHA_PASSWORD=PASSWORD)
    r = run(env=leaky, instruments=_raise(f"login failed for {PASSWORD}"))
    assert PASSWORD not in row(r, "instruments").detail and "***" in row(r, "instruments").detail
    # previous session: an open structure from an earlier day is "not flat"
    db = kw["var_dir"] / "db" / store_mod.DB_FILENAME
    con = sqlite3.connect(db)
    con.execute("INSERT INTO intents(intent_id, session, strategy, strategy_version, underlying, direction, structure, "
                "strike_pref, expiry_rule, trigger_px, recheck_lo, recheck_hi, created_ts, ttl_s, reason) VALUES "
                "('i1', '2026-10-09', 'od', '1.abc', 'NSE:SAIL', 'UP', 'LONG_CE', 'ATM', 'NEAREST_ELIGIBLE', 100, "
                "99, 101, '2026-10-09T09:20:00.000+05:30', 10, '{}')")
    con.execute("INSERT INTO structures(structure_id, session, book, account, policy, strategy, strategy_version, "
                "intent_id, type, underlying, state, reserved_ts) VALUES (1, '2026-10-09', 'PAPER:od', 'PAPER', "
                "'FULL', 'od', '1.abc', 'i1', 'LONG_CE', 'NSE:SAIL', 'EXITING', '2026-10-09T09:20:00.000+05:30')")
    con.commit()
    con.close()
    r = row(run(), "prev_session")
    assert r.status == selfcheck.FAIL and "1 open structure(s) from 2026-10-09" in r.detail
    # no database yet (first start): the DB rows do not apply
    fresh = kw["var_dir"] / "fresh"
    r = run(var_dir=fresh)
    assert {n: row(r, n).status for n in ("db", "live_ready", "prev_session")} == dict.fromkeys(
        ("db", "live_ready", "prev_session"), selfcheck.SKIP)


def test_selfcheck_deploy_migrates_a_copy_only(tmp_path):
    kw = context_kwargs(tmp_path, good=True)
    live_db = kw["var_dir"] / "db" / store_mod.DB_FILENAME
    copy = tmp_path / "backups" / "deploy-abc" / "titan2.db"
    store_mod.backup_db(live_db, copy)
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    r = selfcheck.run_phase("deploy", selfcheck.Context("deploy", **dict(kw, db_copy=copy))).result("db")
    assert r.status == selfcheck.PASS and f"a copy of {copy}" in r.detail
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest           # the operator's copy is untouched
    assert sorted(p.name for p in copy.parent.iterdir()) == ["titan2.db"]
    v1 = tmp_path / "titan_state.db"                                          # never treated as a titan2 DB
    con = sqlite3.connect(v1)
    con.execute("CREATE TABLE active_trades(id INTEGER)")
    con.commit()
    con.close()
    r = selfcheck.run_phase("deploy", selfcheck.Context("deploy", **dict(kw, db_copy=v1))).result("db")
    assert r.status == selfcheck.FAIL and "not a titan2 database" in r.detail


def test_cwd_problems_and_fresh_interpreter(tmp_path):
    app = tmp_path / "app"
    (app / "titan2").mkdir(parents=True)
    (app / "data").mkdir()                                     # a plain directory cannot shadow a regular package
    (app / "notes.txt").write_text("x")
    # modules and packages the bot never imports cannot shadow anything: an operator's research script, v1 leftovers
    (app / "_analyze_today_remote.py").write_text("print('research')\n")
    (app / "zerodha_tools.pyc").write_bytes(b"")
    (app / "ml_models").mkdir()
    (app / "ml_models" / "__init__.py").write_text("")
    assert selfcheck.cwd_problems(app) == []
    selfcheck.assert_clean_cwd(app)
    # a name the bot imports (stdlib, an installed distribution, titan2, a site hook) would replace the real module
    (app / "json.py").write_text("")
    (app / "token.pyc").write_bytes(b"")
    (app / "titan2.py").write_text("")
    (app / "sitecustomize.py").write_text("")
    (app / "kiteconnect").mkdir()
    (app / "kiteconnect" / "__init__.py").write_text("")
    assert selfcheck.cwd_problems(app) == ["module file json.py", "package kiteconnect/",
                                           "module file sitecustomize.py", "module file titan2.py",
                                           "module file token.pyc"]
    with pytest.raises(selfcheck.SelfcheckError, match="json.py"):
        selfcheck.assert_clean_cwd(app)
    # only installed code counts as "elsewhere": the directory itself (sys.path[0] for the bot) never does
    assert all(not Path(e or ".").resolve() == app.resolve() for e in selfcheck.installed_import_dirs(skip=app))
    # a lookup that fails counts as shadowing (fail closed)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(selfcheck.importlib.machinery.PathFinder, "find_spec",
                  lambda *a, **k: (_ for _ in ()).throw(OSError("broken path entry")))
        assert "module file _analyze_today_remote.py" in selfcheck.cwd_problems(app)
        assert "package ml_models/" in selfcheck.cwd_problems(app)

    # FRESH_CHECK run for real in a stand-in package: imports every module, skips the CLI and the dashboard app
    pkg = tmp_path / "fresh" / "titan2"
    (pkg / "broker").mkdir(parents=True)
    (pkg / "dash").mkdir()
    for f in (pkg / "__init__.py", pkg / "broker" / "__init__.py", pkg / "dash" / "__init__.py"):
        f.write_text("")
    (pkg / "__main__.py").write_text("raise SystemExit('the CLI must not run')\n")
    (pkg / "dash" / "app.py").write_text("raise SystemExit('needs dash.env')\n")
    kite = pkg / "broker" / "kite.py"
    kite.write_text("def assert_kite_unpatched():\n    pass\n")
    host = selfcheck.System()
    cwd = pkg.parent
    rc, out = host.run([sys.executable, "-c", selfcheck.FRESH_CHECK], 120, cwd=cwd)
    assert rc == 0 and selfcheck.FRESH_OK in out, out
    (pkg / "broken.py").write_text("import module_that_does_not_exist_u18\n")
    rc, out = host.run([sys.executable, "-c", selfcheck.FRESH_CHECK], 120, cwd=cwd)
    assert rc != 0 and selfcheck.FRESH_OK not in out and "module_that_does_not_exist_u18" in out
    (pkg / "broken.py").unlink()
    kite.write_text("def assert_kite_unpatched():\n    raise RuntimeError('KiteConnect.place_order is patched')\n")
    rc, out = host.run([sys.executable, "-c", selfcheck.FRESH_CHECK], 120, cwd=cwd)
    assert rc != 0 and "patched" in out
    assert host.run(["titan2-no-such-command-u18"], 5)[0] == 127


def test_selfcheck_cli(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(selfcheck.alerts, "send_now",
                        lambda severity, code, text, **kw: sent.append((severity, code, text)) or True)
    out: list[str] = []
    kw = context_kwargs(tmp_path, good=True)
    assert selfcheck.cli("morning", out=out.append, **kw) == 0
    assert out[0].startswith("titan2 self-check morning 2026-10-12 08:40: PASS") and "PASS token:" in out[0]
    reports = list((kw["var_dir"] / "reports").glob("selfcheck-*-morning.json"))
    assert len(reports) == 1 and json.loads(reports[0].read_text(encoding="utf-8"))["ok"] is True
    assert sent == [("INFO", "SELFCHECK_MORNING", out[0].splitlines()[0])]

    bad = context_kwargs(tmp_path / "b", good=False)
    assert selfcheck.cli("cutover", out=out.append, **bad) == 2
    assert out[-1].startswith("titan2 self-check cutover 2026-12-14 08:40: FAIL") and len(sent) == 1
    assert selfcheck.cli("nightly", out=out.append) == 2
    with pytest.raises(ValueError):
        selfcheck.run_phase("nightly")
    with pytest.raises(ValueError):
        selfcheck.run_phase("morning", selfcheck.Context("cutover", **kw))
