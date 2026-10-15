"""Reliability and visibility (2026-10-14 night; the self-audit's F0-F3): the quotes connection kept warm, a DNS blip
survived on the last good address, undelivered phone alerts kept and resent, and what the phone says about titan2
being alive (the start card, crash restarts, the give-up, the laptop's `notify` row).

Offline and deterministic: Kite, ntfy, healthchecks and the DNS are fakes; no network call is made and no real env
file, session or secret is read.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import urllib3.connection
import urllib3.util.connection as conn

from titan2 import alerts, laptop, net, runtime, selfcheck
from titan2.broker.kite import KiteBuckets, KiteData
from titan2.broker import lanes
from titan2.broker.lanes import ReadsLane
from titan2.clock import FakeClock, SessionCalendar, ist_datetime
from titan2.core.events import Alert, FetchQuotes, Priority, QuoteResult, SelfcheckResult
from titan2.model import Account, Quote

S = date(2026, 10, 14)                                   # a Wednesday session
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]})
OPT = "NFO:INDIGO26OCT4700PE"
TOPIC = "titan2-example-not-a-real-topic"                 # a fake ntfy topic: it must never be shown
HC_URL = "https://hc-ping.com/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


# ---------------------------------------------------------------------------------------------------------------
# F0: the quotes connection kept warm
# ---------------------------------------------------------------------------------------------------------------
class QuoteKite:
    """The quotes client: quote() and ltp() as the SDK takes them (a list), each recorded with the clock's time."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, float, list]] = []
        self.ltp_error: Exception | None = None
        self.called = threading.Event()

    def quote(self, symbols):
        self.calls.append(("quote", self.clock.mono(), list(symbols)))
        return {s: {"instrument_token": 1234, "last_price": 10.05,
                    "depth": {"buy": [{"price": 10.0, "quantity": 100}], "sell": [{"price": 10.1, "quantity": 100}]}}
                for s in symbols}

    def ltp(self, symbols):
        self.calls.append(("ltp", self.clock.mono(), list(symbols)))
        self.called.set()
        if self.ltp_error is not None:
            raise self.ltp_error
        return {s: {"instrument_token": 256265, "last_price": 25145.3} for s in symbols}


class Reads:
    """lane-reads' own KiteData stand-in (no order methods); the quote worker has its own client."""

    accounts = frozenset({Account.LIVE})


def _lane(clock: FakeClock, active, kite: QuoteKite | None = None):
    kite = kite or QuoteKite(clock)
    sink: list[Quote] = []
    posted: list = []
    qd = KiteData(kite, clock=clock, buckets=KiteBuckets.create(mono=clock.mono, sleep=clock.advance))
    lane = ReadsLane(Reads(), posted.append, sink.append, mono=clock.mono, quote_data=qd, active=active)
    return lane, kite, sink, posted


def test_quotes_lane_keeps_kites_connection_warm():
    """No quote() call for 60 s while active() (09:00-15:30 on a trading day): one ltp(NIFTY 50) on the quotes
    client, through the same 1-a-second quote bucket. Its answer reaches no sink and no decision; a quote call
    restarts the 60 s; outside active() nothing is sent."""
    clock = FakeClock(ist_datetime(S, "09:00:00"))
    on = [True]
    lane, kite, sink, posted = _lane(clock, lambda: on[0])
    assert lane.active is not None and lanes.KEEP_WARM_IDLE_S == 60.0 and lanes.KEEP_WARM_SYMBOLS == ("NSE:NIFTY 50",)
    assert lane.keep_warm() is True                          # nothing was asked yet: warm the connection now
    assert kite.calls == [("ltp", 1000.0, ["NSE:NIFTY 50"])]
    assert sink == [] and posted == []                       # thrown away: no Quote, no event
    assert lane.keep_warm() is False                         # just asked
    clock.advance(30)
    lane.submit(FetchQuotes("q:exit", (OPT,), int(Priority.EXIT)))
    t_quote = clock.mono()
    assert lane.step() == [QuoteResult("q:exit", True, (OPT,), (), None)]
    assert [c[0] for c in kite.calls] == ["ltp", "quote"] and len(sink) == 1     # a real quote does reach the sink
    clock.advance(59.0)
    assert lane.keep_warm() is False                         # 59 s since the quote call
    clock.advance(1.0)
    assert lane.keep_warm() is True and kite.calls[-1][0] == "ltp"
    assert kite.calls[-1][1] - t_quote >= lanes.KEEP_WARM_IDLE_S
    # the same quote bucket: a quote right after the keep-warm call waits for the next token (1 a second)
    lane.submit(FetchQuotes("q:2", (OPT,), int(Priority.EXIT)))
    warm_at = kite.calls[-1][1]
    lane.step()
    assert kite.calls[-1][0] == "quote" and kite.calls[-1][1] - warm_at >= 1.0 - 1e-6
    # outside 09:00-15:30 (active() False): nothing, however long the lane is idle
    on[0] = False
    clock.advance(600)
    n = len(kite.calls)
    assert lane.keep_warm() is False and len(kite.calls) == n
    # a lane built without active() never keeps warm (tests, tools)
    plain, plain_kite, _, _ = _lane(FakeClock(ist_datetime(S, "10:00")), None)
    assert plain.active is None and plain.keep_warm() is False and plain_kite.calls == []


def test_a_failed_keep_warm_call_is_only_logged(caplog):
    clock = FakeClock(ist_datetime(S, "11:00:00"))
    lane, kite, sink, posted = _lane(clock, lambda: True)
    kite.ltp_error = OSError("getaddrinfo failed")
    with caplog.at_level(logging.WARNING, logger="titan2.broker.lanes"):
        assert lane.keep_warm() is True
    assert "keep-warm" in caplog.text and "getaddrinfo failed" in caplog.text
    assert sink == [] and posted == [] and lane.keep_warm_failures == 1
    assert lane.keep_warm() is False                         # no retry storm: the next try is 60 s later
    clock.advance(lanes.KEEP_WARM_IDLE_S)
    kite.ltp_error = None
    assert lane.keep_warm() is True and lane.keep_warm_calls == 2

    def broken_active() -> bool:
        raise RuntimeError("calendar exploded")

    other, other_kite, _, _ = _lane(FakeClock(ist_datetime(S, "11:00")), broken_active)
    assert other.keep_warm() is False and other_kite.calls == []           # fails closed: no call, no crash


def test_the_quotes_worker_thread_makes_the_keep_warm_call():
    clock = FakeClock(ist_datetime(S, "09:30:00"))
    lane, kite, sink, posted = _lane(clock, lambda: True)
    lane.start()
    try:
        assert kite.called.wait(5), "lane-reads-quotes never made the keep-warm call"
    finally:
        lane.stop(join_s=5)
    assert [c[0] for c in kite.calls] == ["ltp"] and sink == [] and posted == []


def test_keep_warm_hours_are_0900_to_1530_on_a_trading_day():
    clock = FakeClock(ist_datetime(S, "08:59:30"))
    active = runtime.quotes_keep_warm(clock, CAL)
    assert active() is False
    clock.advance_to("09:00:03")
    assert active() is True
    clock.advance_to("15:29:56.900")
    assert active() is True
    clock.advance_to("15:30:30")
    assert active() is False                                 # after 15:30
    for day in (date(2026, 10, 20), date(2026, 10, 17)):    # a weekday holiday, a Saturday
        assert runtime.quotes_keep_warm(FakeClock(ist_datetime(day, "10:00:30")), CAL)() is False
    # never within 3 s of a whole minute, when a bar-close entry asks for quotes (opening_drive at 09:20:00)
    for t, quiet in (("09:19:56.900", False), ("09:19:57", True), ("09:20:00", True), ("09:20:02.900", True),
                     ("09:20:03", False), ("09:00:00", True)):
        assert runtime.quotes_keep_warm(FakeClock(ist_datetime(S, t)), CAL)() is not quiet, t


def test_the_keep_warm_rhythm_never_takes_the_0920_batchs_token():
    """A 60 s rhythm anchored at 09:00:00 would have made its call at 09:20:00.0x, taking the quote bucket's token
    just as opening_drive's batch asks for its quotes; with the quiet seconds it runs at :03 or later instead."""
    clock = FakeClock(ist_datetime(S, "09:00:00"))
    lane, kite, _, _ = _lane(clock, runtime.quotes_keep_warm(clock, CAL))
    while clock.now_wall() < ist_datetime(S, "09:20:02.750"):
        lane.keep_warm()
        clock.advance(0.25)                                  # the quote worker looks every 0.25 s
    times = [ist_datetime(S, "09:00:00") + timedelta(seconds=c[1] - 1000.0) for c in kite.calls]
    assert len(times) == 20 and all(c[0] == "ltp" for c in kite.calls)
    assert all(3.0 <= t.second + t.microsecond / 1e6 < 57.0 for t in times), times
    assert all(b - a >= timedelta(seconds=lanes.KEEP_WARM_IDLE_S) for a, b in zip(times, times[1:]))


# ---------------------------------------------------------------------------------------------------------------
# F2: a DNS blip survived on the last good IPv4 address
# ---------------------------------------------------------------------------------------------------------------
class FakeSock:
    family = socket.AF_INET

    def __init__(self, ip: str, port: int) -> None:
        self.ip, self.port = ip, port

    def getpeername(self):
        return (self.ip, self.port)

    def close(self) -> None:
        pass


@pytest.fixture
def dns(monkeypatch):
    """urllib3's create_connection replaced by a scripted one (restored after the test): api.kite.trade resolves to
    104.18.10.10 while dns["up"], else the lookup fails like 2026-10-14 14:22."""
    state = {"up": True, "calls": []}

    def create(address, timeout=None, source_address=None, socket_options=None):
        state["calls"].append(address)
        host, port = address
        if host in ("api.kite.trade", "ntfy.sh"):
            if not state["up"]:
                raise socket.gaierror(11001, "getaddrinfo failed")
            return FakeSock({"api.kite.trade": "104.18.10.10", "ntfy.sh": "159.203.148.75"}[host], port)
        return FakeSock(host, port)

    monkeypatch.setattr(conn, "allowed_gai_family", conn.allowed_gai_family)
    monkeypatch.setattr(conn, "create_connection", create)
    monkeypatch.setattr(net, "_LAST_GOOD", {})
    monkeypatch.setattr(net, "_FALLBACKS", {})
    return state


def test_a_failed_dns_lookup_connects_to_the_hosts_last_good_ipv4(dns, caplog):
    assert net.force_ipv4({}) is True
    wrapped = conn.create_connection
    assert net.force_ipv4({}) is True and conn.create_connection is wrapped        # installed once
    s = wrapped(("api.kite.trade", 443), 5)
    assert (s.ip, dns["calls"][-1]) == ("104.18.10.10", ("api.kite.trade", 443))
    assert net.last_good("api.kite.trade") == "104.18.10.10"
    dns["up"] = False
    with caplog.at_level(logging.INFO, logger="titan2.net"):
        s = wrapped(("api.kite.trade", 443), 5)
        assert s.ip == "104.18.10.10" and dns["calls"][-2:] == [("api.kite.trade", 443), ("104.18.10.10", 443)]
        # a host never reached in this process: the lookup's own error, as before
        with pytest.raises(socket.gaierror):
            wrapped(("ntfy.sh", 443), 5)
        dns["up"] = True
        wrapped(("api.kite.trade", 443), 5)
    assert "last good address 104.18.10.10" in caplog.text and "answers again" in caplog.text
    # an address given as such is never remembered (nothing to look up)
    wrapped(("10.0.0.5", 443), 5)
    assert net.last_good("10.0.0.5") is None


def test_tls_still_checks_the_real_hostname_on_the_last_good_address(dns, monkeypatch):
    """Only the TCP connect uses the remembered address: urllib3 sends the real hostname for SNI and checks the
    certificate against it, so a wrong address fails TLS instead of talking to the wrong server."""
    net.force_ipv4({})
    conn.create_connection(("api.kite.trade", 443), 5)         # learnt while DNS answered
    dns["up"] = False
    seen: dict = {}

    def wrap(**kw):
        seen.update(kw)
        raise RuntimeError("stop after the TLS arguments")

    monkeypatch.setattr(urllib3.connection, "_ssl_wrap_socket_and_match_hostname", wrap)
    c = urllib3.connection.HTTPSConnection("api.kite.trade", 443)
    with pytest.raises(RuntimeError, match="stop after the TLS arguments"):
        c.connect()
    assert seen["sock"].ip == "104.18.10.10"                  # the TCP connect went to the last good address
    assert seen["server_hostname"] == "api.kite.trade"        # TLS names (and verifies) the real host
    assert seen["cert_reqs"] in (None, "CERT_REQUIRED") or int(seen["cert_reqs"]) == 2
    assert seen["assert_hostname"] is None                     # urllib3's own hostname check stays on


def test_ipv6_opt_out_leaves_the_connect_alone(dns):
    before = conn.create_connection
    assert net.force_ipv4({"TITAN2_IPV6": "1"}) is False and conn.create_connection is before


# ---------------------------------------------------------------------------------------------------------------
# F3: undelivered alerts kept, retried every 30 s, resent after one "network back" line, kept across a restart
# ---------------------------------------------------------------------------------------------------------------
class Channel:
    """ntfy as the alerter sees it: send() raises a retryable AlertError while down, a refusal when told to."""

    def __init__(self) -> None:
        self.down = False
        self.refuse: str | None = None
        self.sent: list[str] = []
        self.tries = 0

    def send(self, text: str) -> None:
        self.tries += 1
        if self.refuse is not None and self.refuse in text:
            raise alerts.AlertError("ntfy: HTTP 400", retry=False)
        if self.down:
            raise alerts.AlertError("ntfy: ConnectionError: Failed to resolve 'ntfy.sh' (getaddrinfo failed)")
        self.sent.append(text)


def _alerter(clock: FakeClock, ch: Channel, **kw) -> alerts.Alerter:
    def sleep(seconds: float) -> None:
        clock.advance(seconds)

    return alerts.Alerter(ch, None, clock=clock, sleep=sleep, **kw)


def _run(a: alerts.Alerter, clock: FakeClock, seconds: float, step_s: float = 1.0) -> None:
    end = clock.mono() + seconds
    while clock.mono() < end:
        a.step()
        clock.advance(step_s)


def _heads(ch: Channel) -> list[str]:
    return [m.splitlines()[0] for m in ch.sent]


def test_alerts_failed_in_a_network_drop_are_resent_when_it_is_back():
    """2026-10-14 14:22: READ_LOST orders, READ_LOST positions and STALE_RECON were logged 'not delivered' and lost;
    the network was back 3 s after the last failure. Now they are kept, tried again every 30 s, and on the first
    success one 'network back' line goes first, then the delayed alerts with their original times."""
    clock = FakeClock(ist_datetime(S, "14:22:30"))
    ch = Channel()
    a = _alerter(clock, ch)
    ch.down = True
    assert a.submit("WARN", "READ_LOST", "LIVE: orders read unanswered 15 s", "read_lost:orders")
    a.step()                                                   # 3 tries (2 s and 5 s apart), then kept
    assert ch.sent == [] and a.held() == 1 and a.counters()["send_failed"] == 1
    a.submit("WARN", "READ_LOST", "LIVE: positions read unanswered 15 s", "read_lost:positions")
    a.submit("WARN", "STALE_RECON", "LIVE: 3 broker reads failed", "stale_recon:LIVE")
    tries = ch.tries
    a.step()                                                   # held behind the first, without a try of their own
    assert a.held() == 3 and a.queued() == 0 and ch.tries == tries
    _run(a, clock, 29)
    assert ch.tries == tries                                   # the next try is 30 s after the failure
    _run(a, clock, 2)
    assert ch.tries == tries + 1 and ch.sent == []             # one try of the "network back" line: still down
    ch.down = False
    _run(a, clock, 40)
    heads = _heads(ch)
    assert heads[0].startswith("[titan2] INFO ALERTS_DELAYED ")
    back = ch.sent[0].splitlines()[1]
    assert back.startswith("network back at 14:23:") and "; 3 alerts delayed" in back
    n = int(back.split(" after ")[1].split(" s;")[0])
    assert 60 <= n <= 80
    # then the delayed alerts, oldest first, each still stamped with its original time
    assert [h.split()[2] for h in heads[1:]] == ["READ_LOST", "READ_LOST", "STALE_RECON"]
    assert [h.split()[3] for h in heads[1:]] == ["14:22:30", "14:22:37", "14:22:37"]
    assert a.held() == 0 and a.counters()["resent"] == 3
    # after the backlog, alerts flow as before: no header for a message that goes through at once
    a.submit("INFO", "POSITION_CLOSED", "PAPER:x closed: net Rs 120", "closed:1")
    a.step()
    assert _heads(ch)[-1].startswith("[titan2] INFO POSITION_CLOSED") and len(ch.sent) == 5


def test_a_critical_goes_first_and_repeats_pause_in_an_outage():
    clock = FakeClock(ist_datetime(S, "11:00:00"))
    ch = Channel()
    a = _alerter(clock, ch)
    ch.down = True
    a.submit("WARN", "FEED_RECONNECT", "ws reconnected", "feed")
    a.submit("CRITICAL", "MISMATCH", "LIVE: broker 0 vs bot 65", "MISMATCH:LIVE")
    _run(a, clock, 20 * 60)                                    # 20 minutes down: 4 repeats would have been due
    ch.down = False
    _run(a, clock, 60)
    codes = [h.split()[2] for h in _heads(ch)]
    assert codes[0] == "ALERTS_DELAYED" and codes[1:3] == ["MISMATCH", "FEED_RECONNECT"]   # the CRITICAL first
    assert codes.count("MISMATCH") == 1                        # not a burst of 4 repeats after the outage
    _run(a, clock, alerts.CRITICAL_REPEAT_S + 5)
    assert [h.split()[2] for h in _heads(ch)].count("MISMATCH") == 2      # the repeats resume afterwards


def test_held_alerts_are_capped_at_200():
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    ch = Channel()
    a = _alerter(clock, ch)
    ch.down = True
    a.submit("CRITICAL", "TOKEN", "token dead", "TOKEN")
    a.ack("TOKEN")                                             # no repeats in this test
    a.step()
    for i in range(alerts.OUTBOX_MAX + 4):
        a.submit("WARN", "X", f"alert {i}", f"k{i}")
    a.step()
    assert a.held() == alerts.OUTBOX_MAX and a.counters()["lost"] == 5
    ch.down = False
    _run(a, clock, 300)
    back = ch.sent[0].splitlines()[1]
    assert "; 200 alerts delayed" in back and "5 not delivered" in back and "see the log" in back
    bodies = [m.splitlines()[1] for m in ch.sent[1:]]
    assert bodies[0] == "token dead" and bodies[1] == "alert 5" and bodies[-1] == "alert 203"   # never the CRITICAL
    assert len(bodies) == alerts.OUTBOX_MAX and a.held() == 0


def test_held_alerts_older_than_2_hours_are_dropped():
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    ch = Channel()
    a = _alerter(clock, ch)
    ch.down = True
    a.submit("CRITICAL", "TOKEN", "token dead", "TOKEN")
    a.ack("TOKEN")
    a.step()
    a.submit("WARN", "X", "an old one", "x")
    a.step()
    clock.advance(alerts.OUTBOX_MAX_AGE_S + 1)
    a.submit("WARN", "Y", "a new one", "y")
    a.step()
    assert a.held() == 1 and a.counters()["lost"] == 2         # 2 h old, the CRITICAL too
    ch.down = False
    _run(a, clock, 40)
    back = ch.sent[0].splitlines()[1]
    assert "; 1 alert delayed" in back and "2 not delivered" in back and "see the log" in back
    assert [m.splitlines()[1] for m in ch.sent[1:]] == ["a new one"]
    # everything aged out and nothing new: the "network back" line still says what was lost
    clock2 = FakeClock(ist_datetime(S, "10:00:00"))
    ch2 = Channel()
    b = _alerter(clock2, ch2)
    ch2.down = True
    b.submit("WARN", "X", "lost one", "x")
    b.step()
    clock2.advance(alerts.OUTBOX_MAX_AGE_S + 1)
    b.step()
    assert b.held() == 0 and b.counters()["lost"] == 1
    ch2.down = False
    _run(b, clock2, 40)
    assert len(ch2.sent) == 1
    body = ch2.sent[0].splitlines()[1]
    assert body.startswith("network back at ") and "no alert waiting" in body and "1 not delivered" in body
    b.submit("WARN", "Z", "after", "z")
    b.step()
    assert len(ch2.sent) == 2                                  # back to normal


def test_a_message_the_service_refuses_is_not_kept(caplog):
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    ch = Channel()
    ch.refuse = "too long"
    a = _alerter(clock, ch)
    a.submit("WARN", "X", "too long for ntfy", "x")
    with caplog.at_level(logging.ERROR, logger="titan2.alerts"):
        a.step()
    assert a.held() == 0 and ch.tries == 1                     # an HTTP 400 is no network drop: one try, logged
    assert "alert not delivered (ntfy: HTTP 400)" in caplog.text


def test_undelivered_alerts_at_the_stop_are_sent_at_the_next_start(tmp_path):
    box = alerts.outbox_path(tmp_path / "var")
    assert box == tmp_path / "var" / "run" / alerts.OUTBOX_FILE
    clock = FakeClock(ist_datetime(S, "15:39:00"))
    ch = Channel()
    ch.down = True
    a = _alerter(clock, ch, outbox_file=box)
    a.submit("INFO", "DAILY_REPORT", "2026-10-14: 3 trades, net Rs 1,487.88", "daily_report")
    a.step()
    a.submit("WARN", "STALE_RECON", "LIVE: 3 broker reads failed", "stale")
    a.stop(timeout_s=0.5)
    saved = json.loads(box.read_text(encoding="utf-8"))
    assert [m["text"].splitlines()[0].split()[2] for m in saved["alerts"]] == ["DAILY_REPORT", "STALE_RECON"]
    assert TOPIC not in box.read_text(encoding="utf-8")
    # the next morning: the alerter takes them (the file is gone) and sends them after one line saying so
    clock2 = FakeClock(ist_datetime(date(2026, 10, 15), "08:40:00"))
    ch2 = Channel()
    b = _alerter(clock2, ch2, outbox_file=box)
    assert b.restore_leftovers() == 2 and not box.exists() and b.held() == 2
    b.submit("INFO", "START_CARD", "titan2 is up", "start_card")
    _run(b, clock2, 10)
    codes = [h.split()[2] for h in _heads(ch2)]
    assert codes == ["ALERTS_DELAYED", "DAILY_REPORT", "STALE_RECON", "START_CARD"]
    line = ch2.sent[0].splitlines()[1]
    assert "2 alerts from the previous run" in line and "2026-10-14 15:39" in line
    assert ch2.sent[1].splitlines()[0].endswith("15:39:00")   # the original time
    assert b.restore_leftovers() == 0                          # nothing twice


def test_one_shot_alerts_can_be_kept_for_the_next_start(tmp_path):
    box = alerts.outbox_path(tmp_path)
    down = Channel()
    down.down = True
    assert alerts.send_now("CRITICAL", "BOT_GAVE_UP", "titan2 stopped", telegram=down, sleep=lambda s: None,
                           outbox=box) is False
    texts = [m["text"] for m in json.loads(box.read_text(encoding="utf-8"))["alerts"]]
    assert len(texts) == 1 and texts[0].startswith("[titan2] CRITICAL BOT_GAVE_UP")
    # delivered, or not configured at all: nothing is kept
    assert alerts.send_now("WARN", "X", "y", telegram=Channel(), sleep=lambda s: None, outbox=box) is True
    assert alerts.send_now("WARN", "X", "y", env={}, outbox=box) is False
    assert len(json.loads(box.read_text(encoding="utf-8"))["alerts"]) == 1


def test_alert_errors_say_whether_a_retry_can_help():
    def resp(status):
        return lambda url, **kw: SimpleNamespace(status_code=status, json=lambda: {"ok": status == 200})

    for status, retry in ((429, True), (500, True), (502, True), (400, False), (401, False), (413, False)):
        with pytest.raises(alerts.AlertError) as e:
            alerts.Ntfy(TOPIC, post=resp(status)).send("[titan2] WARN X\ny")
        assert e.value.retry is retry, status
        with pytest.raises(alerts.AlertError) as e:
            alerts.Telegram("1:tok", "1", post=resp(status)).send("x")
        assert e.value.retry is retry, status

    def broken(url, **kw):
        raise ConnectionError(f"HTTPSConnectionPool(host='ntfy.sh'): /{TOPIC} Failed to resolve")

    with pytest.raises(alerts.AlertError) as e:
        alerts.Ntfy(TOPIC, post=broken).send("[titan2] WARN X\ny")
    assert e.value.retry is True and TOPIC not in str(e.value)
    with pytest.raises(alerts.AlertError) as e:                 # all channels down: retry if any of them can
        alerts.Fanout([alerts.Ntfy(TOPIC, post=broken), alerts.Telegram("1:tok", "1", post=resp(401))]).send("x")
    assert e.value.retry is True


# ---------------------------------------------------------------------------------------------------------------
# Review round 5 (2026-10-15): the message in flight at a stop, a rate limit, a start that is still offline
# ---------------------------------------------------------------------------------------------------------------
def _codes(texts) -> list[str]:
    return [t.splitlines()[0].split()[2] for t in texts]


def _box(path: Path) -> list[str]:
    return _codes(m["text"] for m in json.loads(path.read_text(encoding="utf-8"))["alerts"])


def test_a_message_in_flight_at_the_stop_is_kept_for_the_next_start(tmp_path):
    """Review round 5 (probe probe_thread_stop): stop() waits timeout + 1 s for the alerts thread, then saved only
    what was kept or queued. A message the thread had already taken was in neither: when its tries outlasted the
    wait (a hanging connect), it was held in memory after the save and lost with the process. The most likely one is
    BOT_CRASHED, submitted just before the bot stops its alerter. The message in flight is now saved too; the thread
    makes no further try once stop() is called, and drops its late result (nothing is held twice)."""
    class Hanging:
        def __init__(self) -> None:
            self.calls = 0
            self.entered, self.release = threading.Event(), threading.Event()

        def send(self, text: str) -> None:
            self.calls += 1
            self.entered.set()
            self.release.wait(10)
            raise alerts.AlertError("ntfy: ConnectTimeout")

    box = alerts.outbox_path(tmp_path)
    ch = Hanging()
    a = alerts.Alerter(ch, None, outbox_file=box)
    a.start()
    try:
        a.submit("CRITICAL", "BOT_CRASHED", "titan2-bot crashed (RuntimeError('x'))", "bot:crash")
        assert ch.entered.wait(5)                              # the thread is inside its first try
        a.submit("WARN", "STALE_RECON", "LIVE: 3 broker reads failed", "stale")
        a.stop(0.2)                                            # it returns while that try still hangs
        assert _box(box) == ["BOT_CRASHED", "STALE_RECON"]
    finally:
        ch.release.set()
    a._thread.join(10)
    assert not a._thread.is_alive()
    assert ch.calls == 1 and a.held() == 0                     # no further try; its late failure is dropped
    assert _box(box) == ["BOT_CRASHED", "STALE_RECON"]         # each once


class FailsOnce:
    """A channel whose first try fails at once (a connection reset); the next goes through, or hangs (a connect
    that never answers) until released."""

    def __init__(self, then_hang: bool = False) -> None:
        self.calls = 0
        self.sent: list[str] = []
        self.then_hang = then_hang
        self.failed, self.entered, self.release = threading.Event(), threading.Event(), threading.Event()

    def send(self, text: str) -> None:
        self.calls += 1
        if self.calls == 1:
            self.failed.set()
            raise alerts.AlertError("ntfy: ConnectionError: Connection reset by peer")
        if self.then_hang:
            self.entered.set()
            self.release.wait(10)
            raise alerts.AlertError("ntfy: ConnectTimeout")
        self.sent.append(text)


def test_a_stop_lets_a_failed_message_try_again_until_its_deadline(tmp_path):
    """Review round 6 (probe probe_stop_in_backoff_transient): since round 5's fix for the message in flight, stop()
    also cancelled the quick retry of a message whose first try had just failed. A POSITION_CLOSED raised seconds
    before a clean stop, whose first try met a connection reset, was kept for the next start (on the laptop the next
    morning) though the stop's 5 s had room for the retry, which went out before round 5. The message keeps its
    tries until the stop's deadline, at once (stop() cuts the back-off short); only a try still hanging at the
    deadline is cut, and kept for the next start, once."""
    box = alerts.outbox_path(tmp_path / "a")
    ch = FailsOnce()
    a = alerts.Alerter(ch, None, outbox_file=box)
    a.start()
    a.submit("INFO", "POSITION_CLOSED", "PAPER:x closed (EOD_FLATTEN): net Rs 120", "closed:1")
    assert ch.failed.wait(5)                                   # its first try failed: the thread is in its back-off
    a.stop(5.0)
    assert ch.calls == 2 and _codes(ch.sent) == ["POSITION_CLOSED"]     # the retry went out during the stop
    assert not box.exists() and a.held() == 0 and not a._thread.is_alive()
    # a retry that still hangs at the deadline: cut, kept for the next start, and its late result dropped
    box = alerts.outbox_path(tmp_path / "b")
    ch = FailsOnce(then_hang=True)
    a = alerts.Alerter(ch, None, outbox_file=box)
    a.start()
    try:
        a.submit("INFO", "POSITION_CLOSED", "PAPER:x closed (EOD_FLATTEN): net Rs 120", "closed:1")
        assert ch.failed.wait(5)
        a.stop(1.0)                                            # it returns while the retry hangs
        assert ch.entered.is_set() and _box(box) == ["POSITION_CLOSED"]
    finally:
        ch.release.set()
    a._thread.join(10)
    assert not a._thread.is_alive()
    assert ch.calls == 2 and a.held() == 0 and ch.sent == []   # no third try after the deadline
    assert _box(box) == ["POSITION_CLOSED"]                    # once


class RateLimitedPost:
    """ntfy.sh as alerts.Ntfy's post(): no connection while down; else its per-client request limit, a burst of 60
    and then one request every 5 s, with HTTP 429 when none is left."""

    def __init__(self, clock: FakeClock, burst: int = 60, refill_s: float = 5.0) -> None:
        self.clock, self.burst, self.refill_s = clock, burst, refill_s
        self.tokens, self.last = float(burst), clock.mono()
        self.down = False
        self.sent: list[str] = []
        self.refused = 0

    def __call__(self, url, data=None, headers=None, timeout=None):
        now = self.clock.mono()
        self.tokens = min(float(self.burst), self.tokens + (now - self.last) / self.refill_s)
        self.last = now
        if self.down:
            raise ConnectionError("HTTPSConnectionPool(host='ntfy.sh'): Failed to resolve 'ntfy.sh'")
        if self.tokens < 1.0:
            self.refused += 1
            return SimpleNamespace(status_code=429)
        self.tokens -= 1.0
        self.sent.append(data.decode("utf-8"))
        return SimpleNamespace(status_code=200)


def test_a_rate_limit_while_the_backlog_goes_out_is_no_new_outage():
    """Review round 5 (probe probe_outbox_ratelimit): HTTP 429 counted as a network outage. Sending a backlog of 120
    kept alerts into ntfy.sh's limit (a burst of 60, then one every 5 s) started a new outage at each 429, each with
    one more ALERTS_DELAYED line ("network back ... after 30 s", though the network never dropped): 9 lines over 6
    minutes. A 429 while the backlog goes out now only makes it wait, then it goes on: one line, every alert once,
    in order."""
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    post = RateLimitedPost(clock)
    a = alerts.Alerter(alerts.Ntfy(TOPIC, post=post), None, clock=clock, sleep=clock.advance)
    post.down = True
    for i in range(120):                                       # an outage with an alert every 20 s
        a.submit("WARN", "X", f"alert {i}", f"k{i}")
        a.step()
        clock.advance(20.0)
    assert a.held() == 120
    post.down = False
    end = clock.mono() + 3 * 3600
    while clock.mono() < end and (a.held() or a.queued()):
        a.step()
        clock.advance(1.0)
    assert post.refused > 0                                    # the backlog did run into the limit
    assert _codes(post.sent).count("ALERTS_DELAYED") == 1 and _codes(post.sent)[0] == "ALERTS_DELAYED"
    assert post.sent[0].splitlines()[1].startswith("network back at 10:4")
    assert [t.splitlines()[1] for t in post.sent[1:]] == [f"alert {i}" for i in range(120)]
    assert a.held() == 0 and a.counters()["lost"] == 0


class QuotaPost:
    """ntfy.sh as alerts.Ntfy's post(): no connection while down; once up, `ok` messages go through, then HTTP 429
    for limit_s seconds (a quota; None: for the rest of the day), then everything does. Each delivery is recorded
    with the time it went out."""

    def __init__(self, clock: FakeClock, ok: int, limit_s: float | None) -> None:
        self.clock, self.left, self.limit_s = clock, ok, limit_s
        self.down = True
        self.until: float | None = None                        # mono when the 429s end (inf: never)
        self.sent: list[tuple[datetime, str]] = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        if self.down:
            raise ConnectionError("HTTPSConnectionPool(host='ntfy.sh'): Failed to resolve 'ntfy.sh'")
        if self.left == 0 and self.until is None:              # the quota is used up from here
            self.until = float("inf") if self.limit_s is None else self.clock.mono() + self.limit_s
        if self.until is not None and self.clock.mono() < self.until:
            return SimpleNamespace(status_code=429)
        self.left = max(0, self.left - 1)
        self.sent.append((self.clock.now_wall(), data.decode("utf-8")))
        return SimpleNamespace(status_code=200)


def _quota_day(box: Path, limit_s: float | None) -> tuple[alerts.Alerter, FakeClock, QuotaPost]:
    """10:00 the network drops with an unacknowledged CRITICAL (it repeats every 5 min), and 30 WARNs are kept over
    20 minutes; 10:20 it is back: the ALERTS_DELAYED line, the CRITICAL and 8 WARNs go out, then HTTP 429 (QuotaPost)
    while the other 22 wait."""
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    post = QuotaPost(clock, ok=10, limit_s=limit_s)
    a = alerts.Alerter(alerts.Ntfy(TOPIC, post=post), None, clock=clock, sleep=clock.advance, outbox_file=box)
    a.submit("CRITICAL", "MISMATCH", "LIVE: broker 0 vs bot 65", "MISMATCH:LIVE")
    a.step()
    for i in range(30):
        a.submit("WARN", "X", f"alert {i}", f"k{i}")
        a.step()
        clock.advance(40.0)
    post.down = False
    return a, clock, post


def test_a_criticals_repeats_pause_while_a_rate_limit_holds_the_kept_messages(tmp_path):
    """Review round 6 (probes probe_ratelimit_repeats and probe_ratelimit_all_day): once a 429 while the kept
    messages go out only made them wait, a CRITICAL's 5-minute repeats no longer paused then (only an outage paused
    them): each repeat was queued, got 429 on its tries and was kept as one more CRITICAL. After an hour of 429, the
    12 repeats raised in it reached the phone within 12 s once it ended, and a stop during it saved them all for the
    next start. The repeats now pause until the kept messages are all out, as in an outage, and then resume."""
    a, clock, post = _quota_day(alerts.outbox_path(tmp_path / "a"), limit_s=3600.0)
    end = clock.mono() + 3 * 3600
    while clock.mono() < end:
        a.step()
        clock.advance(1.0)
    texts = [t for _, t in post.sent]
    assert _codes(texts).count("ALERTS_DELAYED") == 1 and a.held() == 0
    assert [t.splitlines()[1] for t in texts if " WARN X " in t] == [f"alert {i}" for i in range(30)]
    lifted = post.sent[10][0]                                  # the first delivery after the hour of 429
    assert lifted - post.sent[9][0] >= timedelta(hours=1)
    critical = [(w, t.splitlines()[0]) for w, t in post.sent if " MISMATCH" in t.splitlines()[0]]
    assert len(critical) > 2 and "(repeat" not in critical[0][1]       # the first message, then the repeats
    raised = [datetime.strptime(h.split()[-1], "%H:%M:%S").time() for _, h in critical[1:]]
    assert min(raised) >= lifted.time()                        # none raised while the kept ones waited
    assert sum(1 for w, _ in critical[1:] if w <= lifted + timedelta(minutes=2)) <= 1    # no burst afterwards
    gaps = [later - earlier for (earlier, _), (later, _) in zip(critical, critical[1:])]
    assert min(gaps) >= timedelta(seconds=alerts.CRITICAL_REPEAT_S - 2)
    # a stop while the 429 lasts keeps the 22 waiting for the next start, and no repeat with them
    box = alerts.outbox_path(tmp_path / "b")
    a, clock, post = _quota_day(box, limit_s=None)
    end = clock.mono() + 3600
    while clock.mono() < end:
        a.step()
        clock.advance(1.0)
    a.stop(0.0)
    assert _box(box) == ["X"] * 22 and len(post.sent) == 10


def test_a_hold_that_began_with_a_rate_limit_says_so():
    """Review round 5: the ALERTS_DELAYED line called every hold a network outage ("network back at ..."), also one
    that began with HTTP 429 on all of a message's tries, when the network was up all along. It now names a rate
    limit as one; a hold the network caused still says "network back"."""
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    sent: list[str] = []
    until = clock.mono() + 60.0                                # the service answers HTTP 429 for a minute

    def post(url, data=None, headers=None, timeout=None):
        if clock.mono() < until:
            return SimpleNamespace(status_code=429)
        sent.append(data.decode("utf-8"))
        return SimpleNamespace(status_code=200)

    a = alerts.Alerter(alerts.Ntfy(TOPIC, post=post), None, clock=clock, sleep=clock.advance)
    a.submit("INFO", "POSITION_CLOSED", "PAPER:x closed: net Rs 120", "closed:1")
    a.step()                                                   # HTTP 429 on all 3 tries (7 s): kept
    assert sent == [] and a.held() == 1
    _run(a, clock, 120)
    assert _codes(sent) == ["ALERTS_DELAYED", "POSITION_CLOSED"]
    line = sent[0].splitlines()[1]
    assert line.startswith("rate limit (HTTP 429) over at 10:01:") and "network" not in line
    assert "; 1 alert delayed" in line


def test_alert_errors_say_whether_they_are_a_rate_limit():
    def resp(status):
        return lambda url, **kw: SimpleNamespace(status_code=status, json=lambda: {"ok": status == 200})

    def broken(url, **kw):
        raise ConnectionError("Failed to resolve")

    for status, limited in ((429, True), (500, False), (503, False)):
        for channel in (alerts.Ntfy(TOPIC, post=resp(status)), alerts.Telegram("1:tok", "1", post=resp(status))):
            with pytest.raises(alerts.AlertError) as e:
                channel.send("[titan2] WARN X\ny")
            assert e.value.retry is True and e.value.rate_limited is limited, (status, channel)
    with pytest.raises(alerts.AlertError) as e:
        alerts.Ntfy(TOPIC, post=broken).send("[titan2] WARN X\ny")
    assert e.value.retry is True and e.value.rate_limited is False
    both = alerts.Fanout([alerts.Ntfy(TOPIC, post=resp(429)), alerts.Telegram("1:tok", "1", post=resp(429))])
    with pytest.raises(alerts.AlertError) as e:
        both.send("x")
    assert e.value.rate_limited is True
    one_down = alerts.Fanout([alerts.Ntfy(TOPIC, post=resp(429)), alerts.Telegram("1:tok", "1", post=broken)])
    with pytest.raises(alerts.AlertError) as e:                # one channel has no network: that is an outage
        one_down.send("x")
    assert e.value.retry is True and e.value.rate_limited is False


def test_a_start_still_offline_counts_its_own_delayed_alerts(tmp_path):
    """Review round 5 (probe probe_restore_outage): a start that restored the previous run's 2 alerts while the network
    was still down never noted when the outage began (only a failed send of its own did, and its alerts waited
    behind the restored ones without one), so after 3 minutes the line said only "2 alerts from the previous run
    ... were not delivered" and then 5 came. The first failed try of the line now marks the outage's start."""
    box = alerts.outbox_path(tmp_path)
    alerts.save_undelivered(box, [("[titan2] WARN FEED_DEAD 11:01:10\nws down", False),
                                  ("[titan2] INFO START_CARD 11:00:05\ntitan2 is up", False)],
                            saved=ist_datetime(date(2026, 10, 15), "11:02:00"))
    clock = FakeClock(ist_datetime(date(2026, 10, 15), "11:02:05"))
    ch = Channel()
    ch.down = True
    a = _alerter(clock, ch, outbox_file=box)
    assert a.restore_leftovers() == 2
    for i, code in enumerate(("READ_LOST", "STALE_RECON", "START_CARD")):
        a.submit("INFO" if code == "START_CARD" else "WARN", code, f"run 2 alert {i}", code)
        _run(a, clock, 20)
    _run(a, clock, 120)                                        # two more minutes down
    ch.down = False
    _run(a, clock, 60)
    assert _codes(ch.sent) == ["ALERTS_DELAYED", "FEED_DEAD", "START_CARD", "READ_LOST", "STALE_RECON", "START_CARD"]
    line = ch.sent[0].splitlines()[1]
    assert line.startswith("network back at 11:05:") and "; 5 alerts delayed" in line
    assert "(2 of them from the previous run, saved 2026-10-15 11:02)" in line
    n = int(line.split(" after ")[1].split(" s;")[0])
    assert 170 <= n <= 200


# ---------------------------------------------------------------------------------------------------------------
# F1: titan2 alive on the phone
# ---------------------------------------------------------------------------------------------------------------
def _row(name: str, status: str, detail: str) -> selfcheck.CheckResult:
    effect = None if status == "PASS" else selfcheck.Effect(status, "x")
    return selfcheck.CheckResult(name, name, status, detail, effect)


def _report(*rows: selfcheck.CheckResult) -> selfcheck.Report:
    return selfcheck.Report("laptop", ist_datetime(S, "08:58"), tuple(rows))


def test_the_start_card():
    report = _report(_row("python", "PASS", "Python 3.12.7"), _row("token", "PASS", "user AB1234"),
                     _row("clock", "PASS", "titan2's clock is 0.012 s behind internet time (+-0.010 s, NTP x)"),
                     _row("network", "PASS", "IPv4 to api.kite.trade: 8/8 connects, median 23 ms (worst 41 ms)"),
                     _row("power", "WARN", "on battery (battery 80%): plug in"),
                     _row("notify", "WARN", "phone alerts by ntfy; HEALTHCHECK_URL is not set (no external dead-man)"),
                     _row("instruments", "FAIL", "InstrumentsError: no NIFTY rows"))
    text = laptop.start_card(report, error=None, signals_on=True,
                             versions={"opening_drive": "1.b230b83102", "price_spike_up": "1.3a030f3e26",
                                       "price_spike_down": "1.9f039e44a7"},
                             modes={"opening_drive": "PAPER", "price_spike_up": "PAPER", "price_spike_down": "LIVE"},
                             condor="OPEN sell 24650 PE / 25700 CE for 2026-10-20; test: running (1 of 52 weeks)")
    lines = text.splitlines()
    assert lines[0].startswith("titan2 is up on the laptop: PAPER only") and "signals on" in lines[0]
    assert "self-check FAIL" in lines[1] and "1 fail, 2 warn, 4 pass" in lines[1]
    assert lines[2] == "FAIL instruments: InstrumentsError: no NIFTY rows"           # FAIL rows first, then WARN
    assert lines[3].startswith("WARN notify: phone alerts by ntfy; HEALTHCHECK_URL is not set")
    assert "clock: titan2's clock is 0.012 s behind internet time (+-0.010 s, NTP x)" in lines
    assert "network: IPv4 to api.kite.trade: 8/8 connects, median 23 ms (worst 41 ms)" in lines
    assert "power WARN: on battery (battery 80%): plug in" in lines                  # AC or battery, each time
    strat = next(ln for ln in lines if ln.startswith("strategies: "))
    assert "opening_drive PAPER 1.b230b83102" in strat and "price_spike_up PAPER 1.3a030f3e26" in strat
    assert "price_spike_down LIVE (runs as PAPER here) 1.9f039e44a7" in strat
    assert lines[-1].startswith("condor: OPEN sell 24650 PE")
    assert len(text) < alerts.NTFY_MAX_CHARS
    ok = laptop.start_card(_report(_row("clock", "PASS", "fine")), error=None, signals_on=False, versions={},
                           modes={}, condor="no condor yet")
    assert "signals NOT on yet" in ok and "self-check PASS (0 fail, 0 warn, 1 pass)" in ok
    broken = laptop.start_card(None, error="RuntimeError('boom')", signals_on=True, versions={}, modes={},
                               condor="no condor yet")
    assert "self-check did not run (RuntimeError('boom'))" in broken


def test_condor_brief(tmp_path, monkeypatch):
    assert laptop.condor_brief(tmp_path, {"TITAN2_CONDOR": "0"}) == "off (TITAN2_CONDOR=0)"
    assert laptop.condor_brief(tmp_path, {}) == "no condor yet"
    from titan2 import condor

    condor.connect(tmp_path).close()                           # the database exists
    monkeypatch.setattr(condor, "status_text", lambda var: "condor (paper, NIFTY weekly 1.5-SD iron condor, 1 lot):\n"
                        "  OPEN   sell 24650 PE / 25700 CE for 2026-10-20: credit 15.02 x 75 = Rs 1,127\n"
                        "  closed 1: 1 won, net Rs 900\n  test: running (2 of 52 weeks entered)\n"
                        "  note 2026-10-14T15:25 ENTRY: entered")
    assert laptop.condor_brief(tmp_path, {}) == ("OPEN sell 24650 PE / 25700 CE for 2026-10-20: credit 15.02 x 75 = "
                                                 "Rs 1,127; closed 1: 1 won, net Rs 900; test: running (2 of 52 "
                                                 "weeks entered)")     # the closed line too since review round 5
    monkeypatch.setattr(condor, "status_text", lambda var: (_ for _ in ()).throw(OSError("locked")))
    assert laptop.condor_brief(tmp_path, {}).startswith("status unavailable (OSError)")


def test_condor_brief_tells_of_a_missed_check_and_a_stop(tmp_path, monkeypatch):
    """Review round 5 (probe probe_card_condor): condor_brief, the condor line of the start card and of the 15:38
    phone report, kept only the OPEN and 'test:' lines of `titan2 condor status`. So an open condor whose 15:25 stop
    check was missed showed its stale 1.00x mark and no word of the skipped check, and a condor stopped at 3.70x
    for Rs -4,100 showed as 'test: running' only. The brief now also keeps the closed line (the last close and its
    reason) and the latest MISSED note, shortened, with how many there are."""
    from titan2 import condor

    monkeypatch.setattr(condor, "reproduces", lambda row: True)    # the made-up rows have no fills
    entry = {"strikes": {"SP": 24650, "SC": 25700, "LP": 24500, "LC": 25850}, "qty": 75, "expiry": "2026-10-20",
             "credit": 15.02, "credit_2x": 12.0, "max_loss": 9000.0, "legs": {}}
    why = "no 15:25 tick was completed (titan2 was not running then, or every try failed)"

    def var_with(name: str, state: str, missed: tuple[str, ...] = ()) -> Path:
        var = tmp_path / name
        con = condor.connect(var)
        try:
            closed = state == "CLOSED"
            con.execute("INSERT INTO condors(expiry, entry_day, state, qty, credit, credit_2x, max_loss, entry, "
                        "exit_reason, exit_day, net, net_2x) VALUES ('2026-10-20', '2026-10-14', ?, 75, 15.02, 12.0, "
                        "9000.0, ?, ?, ?, ?, ?)", (state, json.dumps(entry), "STOP_3X" if closed else None,
                                                   "2026-10-16" if closed else None, -4100.0 if closed else None,
                                                   -4400.0 if closed else None))
            con.execute("INSERT INTO marks(condor_id, day, ts, value_mid, ratio, quotes) VALUES "
                        "(1, '2026-10-14', '2026-10-14T15:25:10+05:30', 15.0, 1.0, '{}')")
            for detail in missed:
                con.execute("INSERT INTO notes(ts, day, kind, detail) VALUES ('2026-10-19T09:30:00+05:30', "
                            "'2026-10-19', 'MISSED', ?)", (detail,))
            con.commit()
        finally:
            con.close()
        return var

    stop_check = f"2026-10-15: {why}: skipped the 3x stop check of the 2026-10-20 condor"
    open_ = ("OPEN sell 24650 PE / 25700 CE, buy 24500 PE / 25850 CE for 2026-10-20: credit 15.02 x 75 = Rs 1,126 "
             "(Rs 900 at 2x spreads), max loss Rs 9,000; last mark 15.00 (1.00x) on 2026-10-14")
    assert laptop.condor_brief(var_with("a", "OPEN", (stop_check,)), {}) == (
        f"{open_}; test: running (1 of 52 weeks entered); MISSED 2026-10-15: skipped the 3x stop check of the "
        "2026-10-20 condor")
    assert laptop.condor_brief(var_with("b", "CLOSED"), {}) == (
        "closed 1: 0 won, net Rs -4,100 (Rs -4,400 at 2x spreads); last: 2026-10-20 STOP_3X Rs -4,100; test: running "
        "(1 of 52 weeks entered)")
    five = tuple(f"2026-10-{d}: {why}: skipped the 3x stop check of the 2026-10-20 condor" for d in range(13, 18))
    assert laptop.condor_brief(var_with("c", "OPEN", five), {}) == (
        f"{open_}; test: running (1 of 52 weeks entered); MISSED 2026-10-17: skipped the 3x stop check of the "
        "2026-10-20 condor (5 missed days in all)")
    odd = "2026-10-15: written by an older titan2"                    # a detail of another shape is kept whole
    assert laptop.condor_brief(var_with("d", "OPEN", (odd, stop_check)), {}).endswith(
        "; MISSED 2026-10-15: skipped the 3x stop check of the 2026-10-20 condor (2 missed days in all)")
    assert laptop.condor_brief(var_with("e", "OPEN", (stop_check, odd)), {}).endswith(
        f"; MISSED {odd} (2 missed days in all)")


class RecAlerter:
    healthcheck = None

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def start(self) -> None:
        pass

    def stop(self, timeout=None) -> None:
        pass

    def submit(self, severity, code, text, dedup_key=None) -> bool:
        self.sent.append((severity, code, text, dedup_key))
        return True

    def dispatch(self, cmd) -> bool:
        return self.submit(cmd.severity, cmd.code, cmd.text, cmd.dedup_key)

    def fail(self, text) -> None:
        pass

    def ping_now(self) -> None:
        pass

    def ping_ready(self) -> None:
        pass

    def unacked_critical(self) -> int:
        return 0


def test_the_bot_sends_one_start_card_once_its_laptop_selfcheck_is_done(tmp_path, monkeypatch):
    """After prep the engine sends RunSelfcheck and StartSignals together; when the laptop phase is done, the bot
    (laptop profile only) sends one INFO START_CARD before the result reaches the core."""
    rec = RecAlerter()
    bot = runtime.BotProcess(env={"TITAN2_VAR": str(tmp_path / "var")}, laptop=True)
    bot.alerter = rec
    rt = SimpleNamespace(ctx=SimpleNamespace(versions={"price_spike_up": "1.3a030f3e26"}),
                         modes={"price_spike_up": "PAPER"}, signals=SimpleNamespace(enabled=True))
    report = _report(_row("clock", "PASS", "titan2's clock is 0.010 s behind internet time"))
    order: list[str] = []
    monkeypatch.setattr(runtime.selfcheck, "run_phase", lambda phase, ctx: report)
    runner = runtime.SelfcheckRunner(lambda phase: None, lambda ev: order.append("posted"),
                                     phases={"prestart": "laptop"},
                                     on_result=lambda res: (bot._start_card(rt, res), order.append("card")))
    res = runner.work("laptop")
    assert order == ["card", "posted"] and res.report is report
    ((sev, code, text, key),) = rec.sent
    assert (sev, code, key) == ("INFO", "START_CARD", "start_card")
    assert "PAPER only" in text and "price_spike_up PAPER 1.3a030f3e26" in text and "condor: no condor yet" in text
    # a follow-up that raises never stops the result reaching the core
    posted: list = []
    runtime.SelfcheckRunner(lambda phase: None, posted.append, on_result=lambda r: 1 / 0).work("laptop")
    assert len(posted) == 1 and isinstance(posted[0], SelfcheckResult)


def test_crash_restarts_warn_on_the_phone_and_the_give_up_is_critical(tmp_path, monkeypatch):
    """No systemd on the laptop: each crash restart was a console line only, and so was 'stopped after crashing:
    open positions are NOT managed'. Now each restart reaches the phone as a WARN (sent by the restarted bot), and
    the give-up as a CRITICAL (also pinging the dead-man's /fail), kept for the next start if it cannot go out."""
    sent: list = []
    monkeypatch.setattr(alerts, "critical_now", lambda text, **kw: sent.append((text, kw)) or 0)
    made: list = []

    def factory(**kw):
        bot = SimpleNamespace(stop=runtime.StopRequest(), kwargs=kw)
        bot.run = lambda: [3, 3, 0][len(made) - 1]
        made.append(bot)
        return bot

    env = {"TITAN2_VAR": str(tmp_path / "var"), "TITAN2_PROFILE": "laptop"}
    out: list[str] = []
    code = laptop._bot_loop(laptop.Session(), env=env, clock=FakeClock(ist_datetime(S, "10:00")), calendar=CAL,
                            halts=[], out=out.append, bot_factory=factory, bot_kwargs=None, sleep=lambda s: None)
    assert code == 0 and len(made) == 3 and sent == []
    assert made[0].kwargs["boot_alerts"] == []
    (w1,), (w2,) = made[1].kwargs["boot_alerts"], made[2].kwargs["boot_alerts"]
    assert isinstance(w1, Alert) and (w1.severity, w1.code) == ("WARN", "BOT_RESTARTED")
    assert "restart 1 of 5" in w1.text and "restart 2 of 5" in w2.text and "10:00" in w1.text
    made.clear()

    def crashing(**kw):
        bot = SimpleNamespace(stop=runtime.StopRequest(), kwargs=kw, run=lambda: 3)
        made.append(bot)
        return bot

    code = laptop._bot_loop(laptop.Session(), env=env, clock=FakeClock(ist_datetime(S, "10:00")), calendar=CAL,
                            halts=[], out=out.append, bot_factory=crashing, bot_kwargs=None, sleep=lambda s: None)
    assert code == runtime.EXIT_EXCEPTION_STORM and len(made) == 1 + laptop.CRASH_RESTARTS
    ((text, kw),) = sent
    assert kw["code"] == "BOT_GAVE_UP" and "open positions are NOT managed" in text and "6 crashes" in text
    assert kw["outbox"] == alerts.outbox_path(tmp_path / "var") and kw["env"]["TITAN2_VAR"] == env["TITAN2_VAR"]
    # a stop asked for during the wait is no give-up: nothing is sent
    sent.clear()
    session = laptop.Session()
    made.clear()

    def stop_during(**kw):
        bot = SimpleNamespace(stop=runtime.StopRequest(), kwargs=kw)
        bot.run = lambda: (session.request_stop("CTRL_C"), 3)[1]
        made.append(bot)
        return bot

    laptop._bot_loop(session, env=env, clock=FakeClock(ist_datetime(S, "10:00")), calendar=CAL, halts=[],
                     out=out.append, bot_factory=stop_during, bot_kwargs=None, sleep=lambda s: None)
    assert sent == [] and len(made) == 1


def test_only_a_sessions_first_run_sends_the_start_card(tmp_path, monkeypatch):
    """Review round 5 (probe probe_crash_loop_cards): every in-process restart built a new BotProcess, with a new
    alerter and so no dedup, and each sent its own START_CARD once its self-check was done: a loop of 6 crashes put
    18 messages on the phone, 6 of them start cards, where the docs promise one. The laptop runner now tells only a
    session's first run to send it (as it does for the self-check's halts); a restart is on the phone as
    BOT_RESTARTED."""
    clock = FakeClock(ist_datetime(S, "11:00:00"))
    phone: list[str] = []

    class Phone:
        def send(self, text: str) -> None:
            phone.append(text.splitlines()[0])

    report = _report(_row("clock", "PASS", "titan2's clock is 0.010 s behind internet time"))
    made: list = []

    def factory(**kw):
        bot = runtime.BotProcess(**kw)
        made.append(bot)

        def run() -> int:                                  # what a run does on the alert side, then a crash
            bot.alerter = alerts.Alerter(Phone(), None, clock=clock, sleep=clock.advance)
            for note in bot.boot_alerts:
                bot.alerter.dispatch(note)
            rt = SimpleNamespace(signals=SimpleNamespace(enabled=True), modes={"price_spike_up": "PAPER"},
                                 ctx=SimpleNamespace(versions={"price_spike_up": "1.3a030f3e26"}))
            bot._start_card(rt, SimpleNamespace(report=report, error=None))     # its self-check after prep
            bot.alerter.submit("CRITICAL", "BOT_CRASHED", "titan2-bot crashed (RuntimeError('x'))", "bot:crash")
            while bot.alerter.queued():
                bot.alerter.step()
            return runtime.EXIT_EXCEPTION_STORM

        bot.run = run
        return bot

    monkeypatch.setattr(alerts, "critical_now", lambda text, **kw: phone.append(f"[titan2] CRITICAL {kw['code']}") or 0)
    env = {"TITAN2_VAR": str(tmp_path / "var"), "TITAN2_PROFILE": "laptop"}
    code = laptop._bot_loop(laptop.Session(), env=env, clock=clock, calendar=CAL, halts=[], out=lambda s: None,
                            bot_factory=factory, bot_kwargs=None, sleep=lambda s: None)
    assert code == runtime.EXIT_EXCEPTION_STORM and len(made) == 1 + laptop.CRASH_RESTARTS
    codes = [h.split()[2] for h in phone]
    assert {c: codes.count(c) for c in set(codes)} == {"START_CARD": 1, "BOT_CRASHED": 6, "BOT_RESTARTED": 5,
                                                        "BOT_GAVE_UP": 1}
    assert codes[:2] == ["BOT_CRASHED", "START_CARD"]          # the first run's (its CRITICAL goes first)
    assert [bot.start_card for bot in made] == [True] + [False] * laptop.CRASH_RESTARTS


def test_the_start_card_comes_from_the_first_run_that_reaches_its_selfcheck(tmp_path):
    """Review round 6 (probe probe_card_after_early_crash): the runner told only a session's first run to send the
    START_CARD, also when that run ended before its self-check result (the card waits for prep and then the
    self-check, about 75 s): a crash then (exit 3) or a controlled restart (exit 75) left the whole session without
    a card, so "no card by 09:15 means titan2 is not running" gave the wrong answer and the card's FAIL and WARN
    rows never reached the phone. The card now comes from the first run that reaches its self-check result, once a
    session. A controlled restart now reaches the phone too, as an INFO BOT_RESTARTED: after a card and an exit 75,
    the phone's last message was the alert that announced the restart, and nothing said titan2 was back."""
    clock = FakeClock(ist_datetime(S, "09:00:00"))
    report = _report(_row("clock", "PASS", "titan2's clock is 0.010 s behind internet time"))
    rt = SimpleNamespace(signals=SimpleNamespace(enabled=True), modes={"price_spike_up": "PAPER"},
                         ctx=SimpleNamespace(versions={"price_spike_up": "1.3a030f3e26"}))

    def session(first_exit: int, card_first: bool) -> tuple[list[str], list[bool]]:
        phone: list[str] = []
        made: list = []

        class Phone:
            def send(self, text: str) -> None:
                phone.append(text.splitlines()[0])

        def factory(**kw):
            bot = runtime.BotProcess(**kw)
            made.append(bot)

            def run() -> int:                              # a run on the alert side: one real alerter each
                bot.alerter = alerts.Alerter(Phone(), None, clock=clock, sleep=clock.advance)
                for note in bot.boot_alerts:
                    bot.alerter.dispatch(note)
                code = runtime.EXIT_OK
                if len(made) == 1:                         # the first run ends early: before its card, or after
                    if card_first:
                        bot._start_card(rt, SimpleNamespace(report=report, error=None))
                    if first_exit == runtime.EXIT_EXCEPTION_STORM:
                        bot.alerter.submit("CRITICAL", "BOT_CRASHED", "titan2-bot crashed (RuntimeError('x'))",
                                           "bot:crash")
                    else:
                        bot.alerter.submit("CRITICAL", "FEED_DEAD", "WS still down 60 s after giving up: controlled "
                                           "restart", "feed:restart")
                    code = first_exit
                else:                                      # the next one is up: its self-check after prep
                    bot._start_card(rt, SimpleNamespace(report=report, error=None))
                while bot.alerter.queued():
                    bot.alerter.step()
                return code

            bot.run = run
            return bot

        env = {"TITAN2_VAR": str(tmp_path / f"var{first_exit}{card_first}"), "TITAN2_PROFILE": "laptop"}
        code = laptop._bot_loop(laptop.Session(), env=env, clock=clock, calendar=CAL, halts=[], out=lambda s: None,
                                bot_factory=factory, bot_kwargs=None, sleep=lambda s: None)
        assert code == runtime.EXIT_OK and len(made) == 2
        return [" ".join(h.split()[1:3]) for h in phone], [bot.start_card for bot in made]

    # the first run crashes in prep (exit 3): the restarted run sends the card
    phone, told = session(runtime.EXIT_EXCEPTION_STORM, card_first=False)
    assert phone == ["CRITICAL BOT_CRASHED", "WARN BOT_RESTARTED", "INFO START_CARD"] and told == [True, True]
    # the first run makes a controlled restart (exit 75) before its card: the next run sends it, and says it is back
    phone, told = session(runtime.EXIT_RESTART, card_first=False)
    assert phone == ["CRITICAL FEED_DEAD", "INFO BOT_RESTARTED", "INFO START_CARD"] and told == [True, True]
    # its card went out, then the controlled restart: no second card, and the phone hears titan2 is back
    phone, told = session(runtime.EXIT_RESTART, card_first=True)
    assert phone == ["CRITICAL FEED_DEAD", "INFO START_CARD", "INFO BOT_RESTARTED"] and told == [True, False]


def test_a_start_card_whose_run_has_ended_is_left_to_the_next_run(tmp_path, monkeypatch):
    """Review round 6: the self-check runs on a thread of its own, which can outlive its run (a crash during the
    check). A card that came after the run's alerter began to stop would reach no phone, so it is not sent, and the
    run does not count it: the runner's next run sends the session's card. One that came while the run ran counts."""
    rec = RecAlerter()
    monkeypatch.setattr(alerts.Alerter, "from_env", lambda *a, **k: rec)
    rt = SimpleNamespace(signals=SimpleNamespace(enabled=True), modes={"price_spike_up": "PAPER"},
                         ctx=SimpleNamespace(versions={"price_spike_up": "1.3a030f3e26"}))
    res = SimpleNamespace(report=_report(_row("clock", "PASS", "fine")), error=None)
    env = {"TITAN2_VAR": str(tmp_path / "var")}
    on_time = runtime.BotProcess(env=env, laptop=True, notifier=runtime.Notifier(send=lambda b: None))
    on_time._serve = lambda loaded, heartbeat: (on_time._start_card(rt, res), runtime.EXIT_RESTART)[1]
    assert on_time._run_with_alerts(None) == runtime.EXIT_RESTART
    assert on_time.card_sent is True and [s[1] for s in rec.sent] == ["START_CARD"]
    rec.sent.clear()
    late = runtime.BotProcess(env=env, laptop=True, notifier=runtime.Notifier(send=lambda b: None))
    late._serve = lambda loaded, heartbeat: runtime.EXIT_EXCEPTION_STORM
    assert late._run_with_alerts(None) == runtime.EXIT_EXCEPTION_STORM
    late._start_card(rt, res)                                  # its self-check thread is still running
    assert late.card_sent is False and rec.sent == []


def test_the_bot_sends_its_boot_alerts_once_its_alerter_runs(tmp_path, monkeypatch):
    rec = RecAlerter()
    monkeypatch.setattr(alerts.Alerter, "from_env", lambda *a, **k: rec)
    note = Alert("WARN", "BOT_RESTARTED", "titan2 (laptop) crashed (exit 3) and was restarted", "bot:restart")
    proc = runtime.BotProcess(env={"TITAN2_VAR": str(tmp_path / "var")}, clock=FakeClock(ist_datetime(
        date(2026, 10, 17), "10:00")), calendar=CAL, cwd=_clean_app(tmp_path), check_kite=lambda **_k: None,
        install_signals=False, notifier=runtime.Notifier(send=lambda b: None), laptop=True, boot_alerts=[note])
    from titan2 import settings as settings_mod

    settings_mod._reset_load_once()
    try:
        assert proc.run() == 0                                 # a Saturday: nothing to do
    finally:
        settings_mod._reset_load_once()
    assert rec.sent[0] == ("WARN", "BOT_RESTARTED", note.text, "bot:restart")


def test_a_laptop_crash_says_who_restarts_it(tmp_path, monkeypatch):
    """BOT_CRASHED said "systemd restarts it" on the laptop too, where the runner does (BOT_RESTARTED follows)."""
    from titan2 import settings as settings_mod, store as store_mod

    rec = RecAlerter()
    monkeypatch.setattr(alerts.Alerter, "from_env", lambda *a, **k: rec)

    def boom(*_a, **_k):
        raise RuntimeError("a crash inside the run")

    settings_mod._reset_load_once()
    try:
        with store_mod.core_thread():
            code = runtime.BotProcess(env={"TITAN2_VAR": str(tmp_path / "var")}, clock=FakeClock(ist_datetime(
                S, "10:00")), calendar=CAL, notifier=runtime.Notifier(send=lambda b: None), kite_factory=boom,
                ticker_factory=boom, cwd=_clean_app(tmp_path), check_kite=lambda **_k: None, install_signals=False,
                laptop=True).run()
    finally:
        settings_mod._reset_load_once()
    assert code == runtime.EXIT_EXCEPTION_STORM
    (crash,) = [s for s in rec.sent if s[1] == "BOT_CRASHED"]
    assert "its laptop runner restarts it (up to 5 times: BOT_RESTARTED)" in crash[2] and "systemd" not in crash[2]


def test_a_fatal_start_keeps_its_alert_for_the_next_start(tmp_path, monkeypatch):
    """Exit 78 sends BOT_FATAL before any alerts thread exists; with the network down it was lost. Now it waits in
    the outbox file, and the next start's alerter sends it first."""
    def down(url, **kw):
        raise ConnectionError("HTTPSConnectionPool(host='ntfy.sh'): Failed to resolve 'ntfy.sh' (getaddrinfo failed)")

    monkeypatch.setattr(alerts, "_default_post", down)
    monkeypatch.setattr(alerts, "SEND_BACKOFF_S", (0.0, 0.0))
    var = tmp_path / "var"
    proc = runtime.BotProcess(env={"TITAN2_VAR": str(var), "NTFY_TOPIC": TOPIC}, clock=FakeClock(ist_datetime(
        S, "08:41")), calendar=CAL, notifier=runtime.Notifier(send=lambda b: None), install_signals=False)
    assert proc._fatal("titan2.db integrity_check: page 7 is damaged") == runtime.EXIT_FATAL
    box = alerts.outbox_path(var)
    (kept,) = json.loads(box.read_text(encoding="utf-8"))["alerts"]
    assert kept["critical"] is True and kept["text"].startswith("[titan2] CRITICAL BOT_FATAL")
    assert "titan2-bot will not start: titan2.db integrity_check" in kept["text"] and TOPIC not in box.read_text(
        encoding="utf-8")


def _clean_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "titan2").mkdir(parents=True, exist_ok=True)
    return app


class NoNetwork:
    """A post() that fails the test: the laptop notify row must not send anything."""

    def __call__(self, *a, **k):
        pytest.fail("the laptop self-check sent a message")


def _notify(env: dict) -> tuple:
    ctx = SimpleNamespace(phase=selfcheck.LAPTOP, env=env, now=ist_datetime(S, "08:50"))
    return selfcheck._check_notify(ctx)


def test_the_laptop_notify_row_checks_the_setup_without_sending(monkeypatch):
    monkeypatch.setattr(alerts, "_default_post", NoNetwork())
    row = next(c for c in selfcheck.LAPTOP_CHECKS if c.name == "notify")
    assert (row.effect.status, row.effect.halt, row.effect.alert) == (selfcheck.WARN, None, None)   # the card lists it
    ok, detail = _notify({"NTFY_TOPIC": TOPIC, "HEALTHCHECK_URL": HC_URL})
    assert ok is True and "phone alerts by ntfy" in detail and "dead-man" in detail
    ok, detail = _notify({"NTFY_TOPIC": TOPIC})
    assert ok is False and "HEALTHCHECK_URL is not set (no external dead-man" in detail
    ok, detail = _notify({"HEALTHCHECK_URL": HC_URL})
    assert ok is False and "no phone alerts" in detail and "NTFY_TOPIC" in detail
    ok, detail = _notify({"NTFY_TOPIC": "short", "TELEGRAM_BOT_TOKEN": "1:tok", "TELEGRAM_CHAT_ID": "9",
                          "HEALTHCHECK_URL": "http://insecure/x"})
    assert ok is False and "ntfy" in detail and "phone alerts by Telegram" in detail and "HEALTHCHECK_URL" in detail
    for _, d in (_notify({"NTFY_TOPIC": TOPIC}), _notify({"NTFY_TOPIC": TOPIC, "HEALTHCHECK_URL": HC_URL})):
        assert TOPIC not in d and HC_URL not in d


def test_the_env_file_note_knows_ntfy(tmp_path):
    def note(values: dict) -> str:
        path = tmp_path / f"e{len(values)}" / "titan2.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        return selfcheck._check_env_file(SimpleNamespace(env_file=path))[1]

    keys = {"ZERODHA_API_KEY": "k", "ZERODHA_API_SECRET": "s", "ZERODHA_USER_ID": "AB1234"}
    with_ntfy = note(dict(keys, NTFY_TOPIC=TOPIC))
    assert "Telegram" not in with_ntfy and TOPIC not in with_ntfy          # ntfy carries the alerts: no Telegram note
    without = note(dict(keys))
    assert "no phone alerts" in without and "NTFY_TOPIC" in without


def test_the_no_env_file_hint_names_ntfy(tmp_path):
    out: list[str] = []
    laptop.run_day({"TITAN2_VAR": str(tmp_path / "var"), "TITAN2_LOG_DIR": str(tmp_path / "logs"),
                    "TITAN2_ENV_FILE": str(tmp_path / "none.env")}, out=out.append,
                   clock=FakeClock(ist_datetime(date(2026, 10, 17), "09:00")), calendar=CAL, dash=False,
                   handlers=False)
    hint = next(line for line in out if line.startswith("no env file at"))
    assert "NTFY_TOPIC" in hint and "HEALTHCHECK_URL" in hint
