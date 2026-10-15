"""Measurement, laptop checks and housekeeping (2026-10-14 night; the self-audit's F6, F7 and F8). Information and
display only: no strategy, setting or version changes, and no decision reads anything measured here.

- F6: every quote-client call measured (the log file and the daily report), every broker read's round trip per
  account and kind, the book a paper order filled on in its COMPLETE line, and the bar check's signed volumes with
  the pre-open volume in bar_checks.detail.
- F7: the laptop self-check's lid, restart_pending, update_hours and wifi rows (WARN only, SKIP when unreadable) and
  a switch from AC to battery sent to the phone.
- F8: placebo alerts labelled as control trades, the 15:38 phone report's new lines, SESSION_IMPAIRED as an alert,
  the laptop's LIVE-account read warnings worded as a Kite account check, and the dashboard's events tail without
  DEBUG rows.

Offline and deterministic: Kite, the host's tools and the registry are fakes; no network call, no real env file, no
real Windows setting is read.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import requests
import urllib3

from titan2 import laptop, ledger, runtime, selfcheck, strategies
from titan2 import store as store_mod
from titan2.broker import lanes
from titan2.broker.kite import KiteBuckets, KiteData
from titan2.broker.lanes import ReadsLane
from titan2.clock import FakeClock, SessionCalendar, ist_datetime
from titan2.core import positions
from titan2.core.events import (
    BarsClosed, FetchOrders, FetchPositions, FetchQuotes, HistoricalResult, IntentReceived, OrdersSnapshot,
    PositionsSnapshot, Priority, QuoteResult,
)
from titan2.market import bars as bars_mod
from titan2.market.bars import Bars, check_bar
from titan2.market.feed import FeedLink, SignalQueue
from titan2.market.quotes import QuoteCache
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, Bar, Direction, FeedHealth, Intent, Recheck, StrikePref, StructState, StructureType,
    UnderlyingTick,
)
from titan2.settings import DEFAULTS

S = date(2026, 10, 12)                                   # a Monday session (the engine rig's)
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]})
A, B, C = "NFO:INDIGO26OCT4700PE", "NFO:INDIGO26OCT4700CE", "NFO:PAYTM26OCT1620CE"
ROW = {"instrument_token": 1234, "last_price": 10.05,
       "depth": {"buy": [{"price": 10.0, "quantity": 100}], "sell": [{"price": 10.1, "quantity": 100}]}}
RELEASE_VERSIONS = {"opening_drive": "1.b230b83102", "price_spike_up": "1.3a030f3e26",
                    "price_spike_down": "1.9f039e44a7"}


def dropped() -> requests.exceptions.ConnectionError:
    """What requests raises when Kite has closed an idle keep-alive connection (urllib3 2.x, requests 2.32)."""
    return requests.exceptions.ConnectionError(urllib3.exceptions.ProtocolError(
        "Connection aborted.", http.client.RemoteDisconnected("Remote end closed connection without response")))


# ---------------------------------------------------------------------------------------------------------------
# F6 (a): every quote-client call measured
# ---------------------------------------------------------------------------------------------------------------
class TimedKite:
    """The quotes client: each quote() takes a scripted time on the fake clock (Kite's answer time) and answers every
    symbol, the scripted ones, or raises; ltp() (the keep-warm call) takes 30 ms."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.script: list[tuple[float, object]] = []

    def quote(self, symbols):
        seconds, answer = self.script.pop(0)
        self.clock.advance(seconds)
        if isinstance(answer, BaseException):
            raise answer
        return {s: dict(ROW) for s in symbols if answer == "all" or s in answer}

    def ltp(self, symbols):
        self.clock.advance(0.03)
        return {s: {"instrument_token": 256265, "last_price": 25145.3} for s in symbols}


class Reads:
    accounts = frozenset({Account.LIVE})


def _quote_lane(clock: FakeClock, active=None) -> tuple[ReadsLane, TimedKite, list]:
    kite = TimedKite(clock)
    posted: list = []
    qd = KiteData(kite, clock=clock, buckets=KiteBuckets.create(mono=clock.mono, sleep=clock.advance))
    return ReadsLane(Reads(), posted.append, lambda q: None, mono=clock.mono, quote_data=qd, active=active), kite, posted


def _ask(lane: ReadsLane, rid: str, *symbols: str) -> list:
    lane.submit(FetchQuotes(rid, tuple(symbols), int(Priority.EXIT)))
    return lane.step()


def test_every_quote_call_is_measured_and_logged_off_the_console(caplog):
    """Each quote() call: the whole call as the lane saw it (the 1-a-second token's wait included), Kite's own answer
    time, the symbols, the seconds since the previous call (a cold connection), a dropped-connection retry and the
    outcome. Kept for the core's LATENCY event and logged once to the file only (not the laptop's console)."""
    clock = FakeClock(ist_datetime(S, "09:20:00"))
    lane, kite, _ = _quote_lane(clock)
    kite.script = [(0.5, "all"), (0.5, "all"), (1.5, "all"), (0.12, dropped()), (0.3, "all"),
                   (0.2, requests.exceptions.ReadTimeout("read timed out")), (0.05, {A})]
    with caplog.at_level(logging.INFO, logger="titan2.broker.lanes"):
        assert _ask(lane, "q1", A, B, C)[0].ok                       # the run's first call: no previous one
        assert _ask(lane, "q2", A)[0].ok                             # at once: waits 0.5 s for the next token
        clock.advance(61.0)
        assert _ask(lane, "q3", B)[0].ok                             # 62 s after the previous one: cold
        assert _ask(lane, "q4", C)[0].ok                             # dropped before Kite answered: asked again once
        failed = _ask(lane, "q5", A)[0]
        assert not failed.ok and failed.error.startswith("AMBIGUOUS:ReadTimeout")
        partial = _ask(lane, "q6", A, B)[0]
        assert partial.ok and partial.missing == (B,)                # Kite left B out
    calls = lane.take_quote_calls()
    assert lane.take_quote_calls() == []                             # taken once
    assert [(c.symbols, c.outcome, c.retried, c.kind) for c in calls] == [
        (3, "OK", False, "QUOTE"), (1, "OK", False, "QUOTE"), (1, "OK", False, "QUOTE"), (1, "OK", True, "QUOTE"),
        (1, "AMBIGUOUS:ReadTimeout", False, "QUOTE"), (2, "MISSING 1", False, "QUOTE")]
    first, second, cold, retried = calls[:4]
    assert first.idle_s is None and first.ms == pytest.approx(500.0) and first.kite_ms == pytest.approx(500.0)
    assert second.ms == pytest.approx(1000.0) and second.kite_ms == pytest.approx(500.0)   # 500 ms of token wait
    assert second.idle_s == pytest.approx(0.5)
    assert cold.idle_s == pytest.approx(62.0) and cold.kite_ms == pytest.approx(1500.0)
    assert retried.kite_ms == pytest.approx(420.0) and retried.ms == pytest.approx(1300.0)  # two tries, two tokens
    lines = [r for r in caplog.records if r.getMessage().startswith("quote call: ")]
    assert len(lines) == 6 and all(r.levelno == logging.INFO for r in lines)
    assert all(getattr(r, laptop.ON_CONSOLE) is False for r in lines)          # the file has it, the console not
    assert lanes.OFF_CONSOLE == {laptop.ON_CONSOLE: False}
    text = [r.getMessage() for r in lines]
    assert text[0] == "quote call: 3 symbols in 500 ms (Kite 500 ms), the first of this run: OK"
    assert text[1] == ("quote call: 1 symbol in 1000 ms (Kite 500 ms, 500 ms waiting for the 1-a-second token), "
                       "0.5 s after the previous call: OK")
    assert "62.0 s after the previous call (cold): OK" in text[2]
    assert text[3].endswith("retried once after a dropped connection: OK")
    assert text[4].endswith(": AMBIGUOUS:ReadTimeout") and text[5].endswith(": MISSING 1")


def test_keep_warm_calls_are_measured_and_a_stand_in_client_still_works():
    """The keep-warm ltp() is measured as kind WARM (it is what keeps the next quote warm); a quote client that does
    not report Kite's own time (a stand-in) is measured with kite_ms and retried unknown."""
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    lane, kite, _ = _quote_lane(clock, active=lambda: True)
    assert lane.keep_warm() is True
    (warm,) = lane.take_quote_calls()
    assert (warm.kind, warm.symbols, warm.outcome, warm.idle_s, warm.retried) == ("WARM", 1, "OK", None, None)
    assert warm.ms == pytest.approx(30.0)

    class Plain:
        def quote(self, symbols):
            return {s: None for s in ()}

    plain = ReadsLane(Reads(), lambda e: None, lambda q: None, mono=clock.mono, quote_data=Plain())
    assert plain.execute(FetchQuotes("p", (A,), int(Priority.EXIT)))[0].ok
    (call,) = plain.take_quote_calls()
    assert (call.kite_ms, call.retried, call.outcome) == (None, None, "MISSING 1")


def test_the_quote_measurement_never_costs_a_quote(monkeypatch, caplog):
    """The measurement is information only: if it breaks, the quote still reaches its requester."""
    clock = FakeClock(ist_datetime(S, "10:00:00"))
    lane, kite, _ = _quote_lane(clock)
    kite.script = [(0.05, "all")]
    monkeypatch.setattr(lanes, "QuoteCall", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken")))
    with caplog.at_level(logging.ERROR, logger="titan2.broker.lanes"):
        assert _ask(lane, "q", A) == [QuoteResult("q", True, (A,), (), None)]
    assert "quote call measurement failed" in caplog.text and lane.take_quote_calls() == []


# ---------------------------------------------------------------------------------------------------------------
# F6 (a, b): the engine's LATENCY event (quote calls and broker read round trips) and the daily report's rows
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def core():
    with store_mod.core_thread():
        yield


def test_the_engine_writes_the_minutes_measurements_into_one_latency_event(tmp_path, core):
    """Once a minute with the heartbeat: the quote lane's measured calls (EngineContext.take_quote_calls) and every
    broker read's round trip, the engine's send to its answer, per account and kind; DEBUG, never an input."""
    from test_engine import Rig

    rig = Rig(tmp_path)
    rig.auto_reads = False
    taken = [[lanes.QuoteCall(rig.clock.mono() - 2.0, 1984.0, 1978.0, 3, 412.3, False, "OK")]]
    rig.ctx.take_quote_calls = lambda: taken.pop(0) if taken else []
    cmds = rig.tick()                                   # the first Timer: the start's recon reads go out
    (first,) = rig.events("LATENCY")                    # with the first heartbeat: the quote call so far
    assert first["severity"] == "DEBUG" and "reads" not in first["payload"]
    assert first["payload"]["quote_calls"] == [{"at": "09:59:58.000", "ms": 1984, "kite_ms": 1978, "n": 3,
                                                "idle_s": 412.3, "retried": False, "outcome": "OK", "kind": "QUOTE"}]
    reads = [c for c in cmds if isinstance(c, (FetchPositions, FetchOrders))]
    assert {(c.account, type(c)) for c in reads} >= {(Account.LIVE, FetchPositions), (Account.LIVE, FetchOrders)}
    rig.clock.advance(0.4)                              # the answers come 400 ms later; LIVE's positions read fails
    for c in reads:
        snap = rig.reply(c)
        if isinstance(c, FetchPositions) and c.account is Account.LIVE:
            snap = PositionsSnapshot(Account.LIVE, False, (), snap.req_mono, snap.recv_mono,
                                     "AMBIGUOUS:ConnectionError: getaddrinfo failed")
        rig.core(snap)
    rig.auto_reads = True
    rig.until("10:01:05", step=1.0)
    second = rig.events("LATENCY")[1]["payload"]
    assert "quote_calls" not in second                  # nothing new from the lane
    assert second["reads"]["LIVE:POSITIONS"][0] == 400 and second["reads"]["LIVE:ORDERS"][0] == 400
    assert second["reads"]["PAPER:POSITIONS"][0] == 400
    assert second["read_failures"] == {"LIVE:POSITIONS": 1}
    rig.store.close()


def _latency_event(st, wall: datetime, payload: dict) -> None:
    with st.tx():
        st.append_event("LATENCY", wall=wall, mono=0.0, payload=payload, severity="DEBUG")


def test_the_daily_report_has_quote_call_and_broker_read_rows(tmp_path, core):
    """The latency table gets quote_call_ms (the whole call), quote_kite_ms (Kite's own share) and one row per account
    and read kind, each with n, p50, p95 and max; one line counts cold calls, failures, retries and keep-warm calls.
    The existing rows keep their exact values."""
    st = store_mod.Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")

    def call(ms, kite, idle, outcome="OK", retried=False, kind="QUOTE"):
        return {"at": "10:00:00.000", "ms": ms, "kite_ms": kite, "n": 1, "idle_s": idle, "retried": retried,
                "outcome": outcome, "kind": kind}

    _latency_event(st, ist_datetime(S, "10:00:00"), {
        "quote_calls": [call(2003, 1997, None), call(110, 105, 4.0), call(980, 975, 412.0),
                        call(1300, 420, 1.5, retried=True), call(30, None, 61.0, kind="WARM")],
        "reads": {"LIVE:POSITIONS": [180, 220], "PAPER:POSITIONS": [250]}})
    _latency_event(st, ist_datetime(S, "10:01:00"), {
        "quote_calls": [call(1850, 1845, 2.0, outcome="AMBIGUOUS:ReadTimeout"), call(90, 85, 3.0, "MISSING 1")],
        "reads": {"LIVE:POSITIONS": [1900]}, "read_failures": {"LIVE:POSITIONS": 1}})
    rep = ledger.daily_report(st, S, ist_datetime(S, "15:38"))
    lat = rep.data["latency"]
    assert lat["quote_call_ms"] == {"n": 6, "p50": 980.0, "p95": 2003.0}
    assert lat["quote_kite_ms"] == {"n": 6, "p50": 420.0, "p95": 1997.0}
    assert lat["read_ms:LIVE:POSITIONS"] == {"n": 3, "p50": 220.0, "p95": 1900.0}
    assert lat["read_ms:PAPER:POSITIONS"] == {"n": 1, "p50": 250.0, "p95": 250.0}
    assert lat["max_ms"]["quote_call_ms"] == 2003.0 and lat["max_ms"]["read_ms:LIVE:POSITIONS"] == 1900.0
    assert lat["quote_calls"] == {"n": 6, "cold": 2, "cold_pct": 33.3, "cold_p50_ms": 980.0, "failed": 1,
                                  "failures": {"AMBIGUOUS:ReadTimeout": 1}, "missing": 1, "retried": 1,
                                  "keep_warm": 1, "keep_warm_failed": 0}
    assert lat["read_failures"] == {"LIVE:POSITIONS": 1}
    assert lat["exit_decision_to_submit_ms"] == {"n": 0, "p50": None, "p95": None}      # unchanged shape
    md = rep.markdown
    assert "| what | n | p50 | p95 | max |" in md
    assert "| quote_call_ms | 6 | 980.00 | 2003.00 | 2003.00 |" in md
    assert "| read_ms:LIVE:POSITIONS | 3 | 220.00 | 1900.00 | 1900.00 |" in md
    assert ("- quote calls: 6; cold (no quote call in the 60 s before) 2 (33.30%), median 980.00 ms; failed 1 "
            "(AMBIGUOUS:ReadTimeout 1); retried 1; symbols left out 1; keep-warm calls 1 (failed 0)") in md
    assert "- broker reads failed: LIVE:POSITIONS 1" in md
    assert ledger.QUOTE_COLD_S == lanes.KEEP_WARM_IDLE_S          # a cold call: one the keep-warm did not prevent
    st.close()


# ---------------------------------------------------------------------------------------------------------------
# F6 (c): the book a paper order filled on, in its COMPLETE log line
# ---------------------------------------------------------------------------------------------------------------
def test_a_paper_fill_logs_the_book_it_filled_on(tmp_path, caplog):
    """The COMPLETE line names the fill book: WS or REST, receipt and exchange times, its age at the due time, the
    wait past due, the walked side's levels down to the limit and the order's draw. Log only: the fill is the same."""
    from test_paper import Rig as PaperRig, terminal

    rig = PaperRig(tmp_path / "paper.db")
    rig.set_book(bids=[(9.95, 1000)], asks=[(10.0, 50), (10.05, 100), (10.15, 500)], ltp=10.0,
                 exch_wall=rig.clock.now_wall())
    res = rig.place(limit=10.10, qty=50)
    with caplog.at_level(logging.INFO, logger="titan2.broker.paper"):
        events = rig.run(3.5)
    snap = terminal(events, res.broker_order_id)
    assert snap.status == "COMPLETE" and snap.filled_qty == 50
    (line,) = [r.getMessage() for r in caplog.records if " COMPLETE: " in r.getMessage()]
    assert line.startswith(f"paper order {res.broker_order_id} ")
    # here the order's due time came before the next feed packet, so it waited for that one (10.15: beyond the limit)
    assert re.search(r"50/50 filled avg 10\.0000; book WS received 10:00:00\.\d{3} \(exchange 10:00:00\), it came "
                     r"(\d+) ms after the due time, filled \1 ms past due; asks to the limit 10\.10: 10\.00x50 "
                     r"10\.05x100; draw 0\.\d{4}$", line), line


def test_the_fill_book_note_says_how_old_the_book_was_at_the_due_time():
    from titan2.broker import paper
    from titan2.model import Level, Quote, Side

    due = ist_datetime(S, "10:00:00.500")
    row = {"due_ts": due.isoformat(), "limit_px": 41.0}
    q = Quote("NFO:X", 1, (Level(40.8, 65), Level(40.75, 130)), (Level(40.95, 65), Level(41.0, 65), Level(41.1, 65)),
              40.9, None, None, ist_datetime(S, "10:00:00"), 5.0, ist_datetime(S, "10:00:00.361"), "REST")
    assert paper._book_note(q, row, Side.BUY, due + timedelta(milliseconds=12), 0.73456) == (
        "book REST received 10:00:00.361 (exchange 10:00:00), 139 ms old at the due time, filled 12 ms past due; "
        "asks to the limit 41.00: 40.95x65 41.00x65; draw 0.7346")
    sell = dict(row, limit_px=40.8)
    assert paper._book_note(dataclasses.replace(q, exch_wall=None), sell, Side.SELL, due, 0.5).endswith(
        "(exchange none), 139 ms old at the due time, filled 0 ms past due; bids to the limit 40.80: 40.80x65; "
        "draw 0.5000")


# ---------------------------------------------------------------------------------------------------------------
# F6 (d): the bar check's signed volumes and the pre-open volume
# ---------------------------------------------------------------------------------------------------------------
def _utick(symbol: str, clock: FakeClock, ltp: float, cum: int) -> UnderlyingTick:
    return UnderlyingTick(symbol, 738561, ltp, cum, 2900.0, 2880.0, clock.now_wall(), clock.mono(), clock.now_wall())


def test_bars_keep_the_first_bars_preopen_volume():
    """The 09:15 bar's volume is the cumulative volume, the pre-open auction's included; the builder also keeps the
    cumulative volume at the symbol's last tick before 09:15:00 (take_preopen, once). Its bars are unchanged."""
    clock = FakeClock(ist_datetime(S, "09:00:00"))
    b = Bars(ist_datetime(S, "09:15"), ist_datetime(S, "15:40"))
    b.on_link(True, clock.now_wall())
    clock.advance_to(ist_datetime(S, "09:07:30"))
    b.on_tick(_utick("NSE:RELIANCE", clock, 2905.0, 12_000))                  # the pre-open auction's volume
    clock.advance_to(ist_datetime(S, "09:15:01"))
    b.on_tick(_utick("NSE:RELIANCE", clock, 2906.0, 15_000))
    b.on_tick(_utick("NSE:INFY", clock, 1500.0, 7_000))                       # no pre-open tick seen
    clock.advance_to(ist_datetime(S, "09:19:59"))
    b.on_tick(_utick("NSE:RELIANCE", clock, 2950.0, 20_000))
    closed = b.close_due(ist_datetime(S, "09:20:00"))
    rel = next(x for x in closed if x.symbol == "NSE:RELIANCE")
    assert (rel.volume, rel.valid) == (20_000, True)                          # unchanged: pre-open included
    assert b.take_preopen() == (("NSE:RELIANCE", 12_000),)
    assert b.take_preopen() == ()                                              # taken once


def test_the_signals_host_sends_the_preopen_volume_with_the_first_bars():
    clock = FakeClock(ist_datetime(S, "09:06:00"))
    sq, posted = SignalQueue(), []
    host = runtime.SignalsHost(signal_q=sq, runners={}, quotes=QuoteCache(mono=clock.mono), clock=clock, calendar=CAL,
                               settings=DEFAULTS, post=posted.append, alert=lambda *x: None,
                               prep_state=runtime.PrepState(S, DEFAULTS),
                               feed_health=lambda: FeedHealth(True, 0.0, clock.mono(), 0, False),
                               mode_of=lambda name: None)

    def pump() -> None:
        for _ in range(4):
            host.step(0.0)

    sq.put_link(FeedLink(True, False, clock.now_wall(), clock.mono()))
    pump()
    clock.advance_to(ist_datetime(S, "09:07:30"))
    sq.put_ticks((_utick("NSE:RELIANCE", clock, 2905.0, 12_000),))
    pump()
    clock.advance_to(ist_datetime(S, "09:15:01"))
    sq.put_ticks((_utick("NSE:RELIANCE", clock, 2906.0, 15_000),))
    pump()
    clock.advance_to(ist_datetime(S, "09:20:00.300"))
    pump()
    (closed,) = [p for p in posted if isinstance(p, BarsClosed)]
    assert closed.bars[0].volume == 15_000 and closed.preopen == (("NSE:RELIANCE", 12_000),)


def test_the_bar_check_detail_has_the_signed_volumes_and_the_preopen_volume():
    """bar_checks.detail keeps both volumes and the pre-open volume, so the gap's sign and its likely cause (Kite's
    candle without the auction) can be read the next morning. The check itself still judges |live/Kite - 1|."""
    t = ist_datetime(S, "09:15")
    live = Bar("NSE:RELIANCE", t, 2900.0, 2950.0, 2890.0, 2940.0, 50_000, True, "LIVE")
    hist = Bar("NSE:RELIANCE", t, 2900.0, 2951.0, 2889.0, 2940.0, 45_000, True, "HIST")
    bc = check_bar(live, hist, preopen_volume=5_000)
    assert not bc.ok and bc.reasons == ("VOLUME",)
    d = json.loads(bc.detail())
    assert d["reasons"] == ["VOLUME"] and d["volume_dev"] == pytest.approx(0.111111)
    assert (d["live_volume"], d["kite_volume"], d["preopen_volume"], d["volume_gap"]) == (50_000, 45_000, 5_000,
                                                                                           5_000)
    assert d["volume_rel"] == pytest.approx(0.111111) and d["volume_rel_ex_preopen"] == 0.0
    lower = json.loads(check_bar(live, dataclasses.replace(hist, volume=52_000)).detail())
    assert lower["volume_rel"] == pytest.approx(-0.038462) and lower["volume_gap"] == -2_000    # the sign is kept
    assert lower["preopen_volume"] is None and lower["volume_rel_ex_preopen"] is None
    missing = json.loads(check_bar(live, None, preopen_volume=5_000).detail())
    assert missing["reasons"] == ["HIST_MISSING"] and missing["kite_volume"] is None and missing["volume_gap"] is None
    assert missing["live_volume"] == 50_000 and missing["preopen_volume"] == 5_000
    assert check_bar(live, hist).ok is check_bar(live, hist, preopen_volume=5_000).ok        # never judged on it


def test_the_engine_copies_the_preopen_volume_into_the_bar_check(tmp_path, core):
    from test_engine import Rig

    rig = Rig(tmp_path)
    rig.tick()
    t = ist_datetime(S, "09:15")
    rig.run(BarsClosed((Bar("NSE:RELIANCE", t, 2900.0, 2950.0, 2890.0, 2940.0, 50_000, True, "LIVE"),),
                       (("NSE:RELIANCE", 5_000),)))
    (ev,) = rig.events("PREOPEN_VOLUME")
    assert ev["severity"] == "DEBUG" and ev["payload"] == {"volumes": {"NSE:RELIANCE": 5_000}}
    rig.eng.state.bar_requests["bar:test:1"] = "NSE:RELIANCE"
    rig.run(HistoricalResult("bar:test:1", "NSE:RELIANCE", True,
                             (Bar("NSE:RELIANCE", t, 2900.0, 2951.0, 2889.0, 2940.0, 45_000, True, "HIST"),)))
    (row,) = rig.store.bar_checks(S)
    d = json.loads(row["detail"])
    assert row["ok"] == 0 and d["preopen_volume"] == 5_000 and d["volume_rel_ex_preopen"] == 0.0
    rig.store.close()


def test_the_v_notes_on_the_first_bars_volume_say_the_check_failed_them_and_no_version_moved():
    """bars.py and opening_drive.py said the first bar's volume matches Kite's 09:15 candle '[V by the bar check]';
    the first full check (2026-10-14) failed that. Docstrings only: every strategy keeps its version."""
    import titan2.strategies.opening_drive as od_mod

    assert "[V by the bar check]" not in bars_mod.__doc__
    assert "the first full bar check failed it (2026-10-14)" in " ".join(bars_mod.__doc__.split())
    od = " ".join(od_mod.__doc__.split())
    assert "[V] by the 15:36 bar check)" not in od
    assert "was a [V] note the first full 15:36 bar check failed" in od
    assert {n: strategies.version(c, DEFAULTS) for n, c in strategies.REGISTRY.items()} == RELEASE_VERSIONS


# ---------------------------------------------------------------------------------------------------------------
# F7: the laptop settings that can end a session, and AC to battery on the phone
# ---------------------------------------------------------------------------------------------------------------
# What `powercfg /qh SCHEME_CURRENT SUB_BUTTONS LIDACTION` printed on the laptop on 2026-10-14 (lid: sleep, sleep)
POWERCFG_OUT = """Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
  GUID Alias: SCHEME_BALANCED
  Subgroup GUID: 4f971e89-eebd-4455-a8de-9e59040e7347  (Power buttons and lid)
    GUID Alias: SUB_BUTTONS
    Power Setting GUID: 5ca83367-6e45-459f-a27b-476b1d01c936  (Lid close action)
      GUID Alias: LIDACTION
      Possible Setting Index: 000
      Possible Setting Friendly Name: Do nothing
      Possible Setting Index: 001
      Possible Setting Friendly Name: Sleep
      Possible Setting Index: 002
      Possible Setting Friendly Name: Hibernate
      Possible Setting Index: 003
      Possible Setting Friendly Name: Shut down
    Current AC Power Setting Index: 0x00000001
    Current DC Power Setting Index: 0x00000001
"""
# `netsh wlan show profiles` and `... show profile name=<n>`, as Windows 11 prints them (network names made up)
NETSH_PROFILES = """Profiles on interface Wi-Fi:

Group policy profiles (read only)
---------------------------------
    <None>

User profiles
-------------
    All User Profile     : Office_5G
    Current User Profile : Home Net
    All User Profile     : Cafe Guest
"""


def netsh_profile(name: str, mode: str) -> str:
    return (f"Profile {name} on interface Wi-Fi: \n{'=' * 71}\n\nApplied: All User Profile\n\nProfile information \n"
            f"-------------------\n    Version                : 1\n    Type                   : Wireless LAN\n"
            f"    Name                   : {name}\n    Control options        : \n"
            f"        Connection mode    : {mode}\n        Network broadcast  : Connect only if this network is "
            "broadcasting\n        AutoSwitch         : Do not switch to other networks\n")


def test_lid_close_action_reads_powercfg():
    calls = []

    def run(cmd, timeout_s):
        calls.append((tuple(cmd), timeout_s))
        return 0, POWERCFG_OUT

    assert laptop.lid_close_action(run=run) == (1, 1)
    assert calls == [(("powercfg", "/qh", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION"), laptop.PROBE_TIMEOUT_S)]
    plugged_nothing = POWERCFG_OUT.replace("Current AC Power Setting Index: 0x00000001",
                                           "Current AC Power Setting Index: 0x00000000")
    assert laptop.lid_close_action(run=lambda c, t: (0, plugged_nothing)) == (0, 1)
    other_language = "\n".join(line.split(":")[0].replace("Current", "Aktuell") + ": " + line.split(": ")[-1]
                               if "0x" in line else line for line in POWERCFG_OUT.splitlines())
    assert laptop.lid_close_action(run=lambda c, t: (0, other_language)) == (1, 1)     # AC is listed before DC
    for answer in (None, (1, "Invalid Parameters"), (0, "nothing useful"), (124, "timed out")):
        assert laptop.lid_close_action(run=lambda c, t, a=answer: a) is None
    assert laptop.LID_ACTIONS == Settings.LID_ACTIONS                         # powercfg's LIDACTION index


def test_restart_pending_and_active_hours_read_the_registry():
    keys = {(laptop.REBOOT_REQUIRED_KEY, None): True, (laptop.UPDATE_UX_KEY, "ActiveHoursStart"): 9,
            (laptop.UPDATE_UX_KEY, "ActiveHoursEnd"): 2}

    def reg(path, name):
        if (path, name) not in keys:
            raise FileNotFoundError(path)
        v = keys[(path, name)]
        if isinstance(v, BaseException):
            raise v
        return v

    assert laptop.restart_pending(reg=reg) is True
    assert laptop.update_active_hours(reg=reg) == (9, 2)
    del keys[(laptop.REBOOT_REQUIRED_KEY, None)]
    assert laptop.restart_pending(reg=reg) is False                           # no key: nothing pending
    keys[(laptop.REBOOT_REQUIRED_KEY, None)] = PermissionError("denied")
    assert laptop.restart_pending(reg=reg) is None                            # cannot tell
    del keys[(laptop.UPDATE_UX_KEY, "ActiveHoursEnd")]
    assert laptop.update_active_hours(reg=reg) is None                        # not set
    keys[(laptop.UPDATE_UX_KEY, "ActiveHoursEnd")] = 24
    assert laptop.update_active_hours(reg=reg) is None                        # nonsense
    assert "WindowsUpdate\\Auto Update\\RebootRequired" in laptop.REBOOT_REQUIRED_KEY


def test_wifi_auto_connect_reads_netsh_without_any_key():
    modes = {"Office_5G": "Connect automatically", "Home Net": "Connect automatically", "Cafe Guest": "Connect manually"}
    asked = []

    def run(cmd, timeout_s):
        asked.append(tuple(cmd))
        if tuple(cmd) == ("netsh", "wlan", "show", "profiles"):
            return 0, NETSH_PROFILES
        name = cmd[-1].removeprefix("name=")
        return (0, netsh_profile(name, modes[name])) if name in modes else (1, f'Profile "{name}" is not found')

    assert laptop.wifi_auto_connect(run=run) == (["Office_5G", "Home Net"], 3)
    assert asked[1:] == [("netsh", "wlan", "show", "profile", f"name={n}") for n in ("Office_5G", "Home Net",
                                                                                      "Cafe Guest")]
    assert not any("key=clear" in " ".join(c) for c in asked)                # a Wi-Fi password is never read
    no_wifi = (1, "There is no wireless interface on the system.")
    assert laptop.wifi_auto_connect(run=lambda c, t: no_wifi) is None
    assert laptop.wifi_auto_connect(run=lambda c, t: None) is None             # not Windows / no netsh
    empty = NETSH_PROFILES.split("User profiles")[0] + "User profiles\n-------------\n    <None>\n"
    assert laptop.wifi_auto_connect(run=lambda c, t: (0, empty)) == ([], 0)
    german = NETSH_PROFILES.replace("User profiles", "Benutzerprofile").replace("All User Profile", "Profil für alle")
    assert laptop.wifi_auto_connect(run=lambda c, t: (0, german.replace("Current User Profile", "Aktuell"))) is None


class Settings:
    """titan2.laptop's session-settings probes for the rows (each answer scripted; an exception is raised)."""

    LID_ACTIONS = {0: "do nothing", 1: "sleep", 2: "hibernate", 3: "shut down"}     # == laptop.LID_ACTIONS

    def __init__(self, lid=(0, 1), restart=False, hours=(7, 1), wifi=(["Home"], 2)) -> None:
        self.answers = {"lid_close_action": lid, "restart_pending": restart, "update_active_hours": hours,
                        "wifi_auto_connect": wifi}

    def __getattr__(self, name):
        answers = self.__dict__["answers"]
        if name not in answers:
            raise AttributeError(name)

        def probe():
            if isinstance(answers[name], BaseException):
                raise answers[name]
            return answers[name]
        return probe


ROWS4 = ("lid", "restart_pending", "update_hours", "wifi")


def _rows4(monkeypatch, fake: Settings) -> dict[str, selfcheck.CheckResult]:
    import sys

    import titan2

    monkeypatch.setitem(sys.modules, "titan2.laptop", fake)
    monkeypatch.setattr(titan2, "laptop", fake, raising=False)
    ctx = SimpleNamespace(phase=selfcheck.LAPTOP, scrub=lambda text: text)
    checks = {c.name: c for c in selfcheck.LAPTOP_CHECKS}
    return {n: selfcheck._run_one(checks[n], ctx) for n in ROWS4}


def test_the_laptop_settings_rows_warn_skip_and_never_fail(monkeypatch):
    """The 10-14 laptop: every one of the four WARNs, with what to change; none raises a halt or an alert of its own
    (the start card lists them). Unreadable: SKIP. A probe that breaks: WARN 'could not check', never FAIL."""
    rows = _rows4(monkeypatch, Settings(lid=(1, 1), restart=True, hours=(9, 2),
                                        wifi=(["Office_5G", "Home Net", "Cafe Guest", "Hotel", "Phone"], 6)))
    assert {n: r.status for n, r in rows.items()} == dict.fromkeys(ROWS4, "WARN")
    assert all(r.effect.halt is None and r.effect.alert is None for r in rows.values())
    assert rows["lid"].detail.startswith("closing the lid while plugged in: sleep; on battery: sleep. A closed lid "
                                         "sleeps the laptop")
    assert "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0" in rows["lid"].detail
    assert rows["restart_pending"].detail.startswith("Windows Update waits for a restart")
    assert rows["update_hours"].detail.startswith("Windows Update's active hours are 09:00-02:00: outside them")
    assert "Set them to 07:00-01:00" in rows["update_hours"].detail
    assert rows["wifi"].detail.startswith("5 of 6 saved Wi-Fi networks connect automatically: Windows can switch")
    assert not any(n in rows["wifi"].detail for n in ("Office_5G", "Home Net", "Cafe Guest"))   # names never reach the phone
    assert all(len(r.detail) <= selfcheck.DETAIL_MAX for r in rows.values())
    rows = _rows4(monkeypatch, Settings(lid=None, restart=None, hours=None, wifi=None))
    assert {n: r.status for n, r in rows.items()} == dict.fromkeys(ROWS4, "SKIP")
    rows = _rows4(monkeypatch, Settings(lid=OSError("x"), restart=OSError("x"), hours=OSError("x"), wifi=OSError("x")))
    assert {n: r.status for n, r in rows.items()} == dict.fromkeys(ROWS4, "WARN")
    rows = _rows4(monkeypatch, Settings(hours=(8, 16), wifi=(["Home"], 1)))
    assert rows["update_hours"].status == "PASS" and rows["wifi"].detail == ("1 of 1 saved Wi-Fi network connects "
                                                                             "automatically (Home)")
    assert _rows4(monkeypatch, Settings(hours=(8, 15)))["update_hours"].status == "WARN"   # 15:00-16:00 is open
    assert _rows4(monkeypatch, Settings(hours=(22, 21)))["update_hours"].status == "PASS"  # past midnight


def test_the_start_card_lists_the_laptop_settings_warnings():
    def row(name, status, detail):
        return selfcheck.CheckResult(name, name, status, detail, None)

    report = selfcheck.Report("laptop", ist_datetime(S, "08:50"), (
        row("clock", "PASS", "this clock is 0.120 s behind internet time"),
        row("lid", "WARN", "closing the lid while plugged in: sleep; on battery: sleep. A closed lid sleeps"),
        row("restart_pending", "WARN", "Windows Update waits for a restart"),
        row("wifi", "SKIP", "the saved Wi-Fi networks could not be listed")))
    card = laptop.start_card(report, error=None, versions={}, modes={}, condor="none open", signals_on=True)
    assert "WARN lid: closing the lid while plugged in: sleep" in card
    assert "WARN restart_pending: Windows Update waits for a restart" in card and "wifi" not in card


class RecAlerter:
    def __init__(self, accept=True) -> None:
        self.sent: list[tuple] = []
        self.accept = accept
        self.running = True

    def submit(self, severity, code, text, dedup_key=None):
        self.sent.append((severity, code, text, dedup_key))
        return self.accept


def _power_watch(tmp_path, states, alerter=None):
    session = laptop.Session()
    if alerter is not None:
        session.bind(SimpleNamespace(stop=runtime.StopRequest(), alerter=alerter))
    readings, mono = iter(states), [100.0]
    sup = laptop.Supervisor(ctl=laptop.CtrlC(), session=session, var=tmp_path / "var",
                            clock=FakeClock(ist_datetime(S, "11:00")), out=lambda s: None,
                            power=lambda: next(readings), mono=lambda: mono[0])
    return sup, session, mono


AC = laptop.PowerStatus(on_ac=True, battery_pct=96)
BATTERY = laptop.PowerStatus(on_ac=False, battery_pct=95)


def test_a_switch_to_battery_reaches_the_phone_and_back_on_ac_once(tmp_path, caplog):
    """Looked at every 30 s: AC to battery is one WARN POWER (it was a 15-minute log line only); back on AC after it,
    one INFO. The first reading only notes the source (the start card reports it)."""
    rec = RecAlerter()
    sup, _, mono = _power_watch(tmp_path, [AC, AC, BATTERY, BATTERY, AC], rec)
    with caplog.at_level(logging.INFO, logger="titan2.laptop"):
        for _ in range(5):
            sup.step()
            mono[0] += laptop.POWER_WATCH_S
    assert [(s, c, k) for s, c, _, k in rec.sent] == [("WARN", "POWER", "power:battery"), ("INFO", "POWER", "power:ac")]
    assert rec.sent[0][2].startswith("the laptop switched to battery power at 11:00:00 (battery 95%): plug the "
                                     "charger in")
    assert rec.sent[1][2] == "back on AC power at 11:00:00 (battery 96%), after 1 min on battery"
    power = [r for r in caplog.records if r.getMessage().startswith("power: ")]
    assert [r.levelname for r in power] == ["INFO", "WARNING", "INFO"]          # the start, and each change at once


def test_power_alerts_stay_few(tmp_path):
    """Starting on battery is no alert (the start card's power row says it). A battery warning the alerter
    suppressed as a repeat (its 30-minute WARN dedup) brings no 'back on AC'. Between two runs of the bot the
    warning waits for the next running alerter; a switch back before then sends nothing at all."""
    rec = RecAlerter()
    sup, _, mono = _power_watch(tmp_path, [BATTERY, AC], rec)
    sup.step()
    mono[0] += laptop.POWER_WATCH_S
    sup.step()
    assert rec.sent == []
    quiet = RecAlerter(accept=False)
    sup, _, mono = _power_watch(tmp_path, [AC, BATTERY, AC], quiet)
    for _ in range(3):
        sup.step()
        mono[0] += laptop.POWER_WATCH_S
    assert [c[3] for c in quiet.sent] == ["power:battery"]
    later = RecAlerter()
    sup, session, mono = _power_watch(tmp_path, [AC, BATTERY, BATTERY], None)
    sup.step()
    mono[0] += laptop.POWER_WATCH_S
    sup.step()                                                       # no bot bound: the warning waits
    session.bind(SimpleNamespace(stop=runtime.StopRequest(), alerter=later))
    mono[0] += 1.0
    sup.step()
    assert [c[3] for c in later.sent] == ["power:battery"]
    gone = RecAlerter()
    sup, session, mono = _power_watch(tmp_path, [AC, BATTERY, AC, AC], None)
    for _ in range(3):
        sup.step()
        mono[0] += laptop.POWER_WATCH_S
    session.bind(SimpleNamespace(stop=runtime.StopRequest(), alerter=gone))
    sup.step()
    assert gone.sent == []                                           # the episode ended before anyone could hear
    stopped = RecAlerter()
    stopped.running = False                                          # a bot whose alerter already stopped
    sup, _, mono = _power_watch(tmp_path, [AC, BATTERY], stopped)
    sup.step()
    mono[0] += laptop.POWER_WATCH_S
    sup.step()
    assert stopped.sent == [] and sup._power_alerts


# ---------------------------------------------------------------------------------------------------------------
# F8: placebo alerts, the 15:38 phone report, SESSION_IMPAIRED, the laptop's LIVE reads, the events tail
# ---------------------------------------------------------------------------------------------------------------
def test_placebo_alerts_say_they_are_a_control_trade(tmp_path, core):
    """On 10-14 the phone showed 'PLACEBO:price_spike_up ... net Rs -5,299.97' like a normal trade. A placebo's opened
    and closed alerts now begin with 'control trade (placebo), not a strategy result'; a strategy's do not."""
    from test_engine import INST, STRAT, VERSION, Rig

    rig = Rig(tmp_path)
    rig.tick()
    rig.gateway.book = f"PLACEBO:{STRAT}"
    rig.book(41.90, 42.15)
    it = Intent(intent_id="iplacebo0001", strategy=STRAT, strategy_version=VERSION, underlying="NSE:RELIANCE",
                direction=Direction.UP, structure=StructureType.LONG_CE, strike_pref=StrikePref.ATM,
                expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=2900.0, recheck=Recheck(-math.inf, math.inf), ttl_s=10.0,
                created_mono=rig.clock.mono(), created_wall=rig.clock.now_wall(), reason={"why": "test"},
                is_placebo=True)
    rig.run(IntentReceived(it))
    pk = rig.gateway.placed[-1]
    rig.book(42.10, 42.30)
    rig.fill(pk, 42.20)
    (opened,) = rig.alerts("POSITION_OPENED")
    assert opened.text == (f"control trade (placebo), not a strategy result: PLACEBO:{STRAT} {INST.symbol} opened: "
                           "100 @ 42.20")
    leg = rig.eng.state.legs[rig.eng.state.orders[pk].leg_id]
    st = rig.struct(leg.leg_id)
    row = {"symbol": INST.symbol, "exit_reason": "HARD_SL", "net": -5299.97, "pnl_status": "FINAL"}
    assert positions._closed_alert(st, row).text == (
        f"control trade (placebo), not a strategy result: PLACEBO:{STRAT} {INST.symbol} closed (HARD_SL): net Rs "
        "-5,299.97 [FINAL]")
    parent = dataclasses.replace(st, is_placebo=False, book=f"PAPER:{STRAT}")
    assert positions._closed_alert(parent, row).text.startswith(f"PAPER:{STRAT} {INST.symbol} closed")
    assert positions.PLACEBO_LABEL == "control trade (placebo), not a strategy result"
    rig.store.close()


def test_the_phone_report_has_the_days_refusals_whatif_condor_bar_check_alerts_and_g1_today(tmp_path, core):
    from test_ledger_g1 import NOW, REG, SESSION, V, make_intent, make_structure, put_trade
    from titan2 import g1

    st = store_mod.Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    with st.tx():
        for name, mode in (("alpha", "PAPER"), ("beta", "PAPER"), ("gamma", "PAPER"), ("delta", "SHADOW")):
            st.put_strategy_mode(name, version=V, mode=mode, changed_wall=REG, changed_by="test")
            if mode == "PAPER":
                g1.ensure_trial(st, name, V, {"params": {"x": 1}}, REG, 0.0)
        for n, (gate, detail) in enumerate((("BOOK_CAPS", "ENTRIES 3/3"), ("BOOK_CAPS", "ENTRIES 3/3"),
                                            ("COOLDOWN", "90 s"))):
            it = make_intent(f"iref{n:08d}", NOW.replace(hour=11), strategy="alpha", version=V)
            st.insert_intent(it, SESSION)
            st.put_intent_route(it.intent_id, "PAPER:alpha", "REFUSED", NOW.replace(hour=11), gate=gate, detail=detail)
        vit = make_intent("ivoid0000001", NOW.replace(hour=11), strategy="alpha", version=V)
        st.insert_intent(vit, SESSION)
        void = make_structure(0, vit, book="PAPER:alpha", state=StructState.VOID, exit_reason=None)
        void.void_reason = "SIZE:AT_DECISION: DEPTH"
        st.insert_structure(void)
        for n, (ok, reasons) in enumerate(((True, []), (True, []), (False, ["VOLUME"]))):
            st.put_bar_check(SESSION, f"NSE:S{n}", ist_datetime(SESSION, "09:15"), ok,
                             json.dumps({"reasons": reasons}))
        st.flag_session(SESSION, "beta", "heartbeat gap 2400 s (09:23 -> 10:03)")
        st.append_event("MISMATCH", wall=NOW.replace(hour=12), mono=0.0, payload={"symbol": "NFO:X"},
                        severity="CRITICAL", session=SESSION, account=Account.PAPER, book="PAPER:gamma")
    put_trade(st, session=SESSION, exit_px=106.0, strategy="alpha")
    put_trade(st, session=SESSION, exit_px=96.0, strategy="alpha", book="PLACEBO:alpha", placebo=True, minute=1)
    reports = tmp_path / "reports"
    reports.mkdir()
    rows = [{"why": "SIZE:AT_DECISION", "outcome": {"net": 150.0}}, {"why": "LIQUIDITY:X", "outcome": {"net": -100.0}},
            {"why": "QUOTE:X", "outcome": None, "note": "no minute bars"}]
    (reports / "whatif-2026-10-09.json").write_text(json.dumps({"day": "2026-10-09", "rows": rows}), encoding="utf-8")
    (reports / "whatif-2026-10-13.json").write_text(json.dumps({"day": "2026-10-13", "rows": rows}), encoding="utf-8")
    rep = ledger.daily_report(st, SESSION, NOW, reports_dir=reports,
                              alerts={"sent": 41, "held": 3, "resent": 3, "lost": 0, "dropped": 0},
                              notes={"condor": "OPEN NIFTY 2026-10-19 short 25400/24300; test: running (1 of 52)"})
    lines = rep.telegram.splitlines()
    assert any(x.startswith("PLACEBO:alpha (control trades, placebo: not a strategy result): n=1 ") for x in lines)
    assert any(x.startswith("PAPER:alpha: n=1 ") for x in lines)
    assert "refused 3 (BOOK_CAPS 2, COOLDOWN 1); voided 1 (SIZE 1)" in lines
    assert ("what-if (saved days before today; today's runs after the session): 2 refused entries replayed over 1 "
            "day: net +Rs 50, 1 won") in lines
    assert "condor: OPEN NIFTY 2026-10-19 short 25400/24300; test: running (1 of 52)" in lines
    assert "bar check: 3 checked, 1 mismatched (VOLUME 1)" in lines
    assert "alerts this run: 41 sent, 3 delayed by the network (0 still waiting), none lost" in lines
    today = {x.split(" ")[1]: x.split("; today counts for G1: ")[1] for x in lines if x.startswith("G1 ")}
    assert today == {
        "alpha": "yes",
        "beta": "no (marked IMPAIRED: heartbeat gap 2400 s (09:23 -> 10:03); its trades still count, the day uses "
                "the allowance)",
        "gamma": "no (1 PAPER mismatch today: G1 fails every later look of this version)",
        "delta": "no (mode SHADOW: G1 tests PAPER and LIVE strategies only)"}
    assert len(rep.telegram) <= 3500
    md = rep.markdown
    assert "- bar check: 3 checked, 1 mismatched (VOLUME 1)" in md and "- condor: OPEN NIFTY 2026-10-19" in md
    assert "| strategy | version | mode | trial | n | sessions | exp_pct | lb_pct | next_look | futility_ub_pct | today |" \
        in md
    lost = ledger.daily_report(st, SESSION, NOW, alerts={"sent": 9, "held": 5, "resent": 1, "lost": 2, "dropped": 1})
    assert "alerts this run: 9 sent, 5 delayed by the network (2 still waiting), 3 not delivered (see the log)" in \
        lost.telegram.splitlines()
    bare = ledger.daily_report(st, SESSION, NOW)                                 # the CLI's report: no extras
    assert not [x for x in bare.telegram.splitlines() if x.startswith(("what-if", "condor:", "alerts this run"))]
    (reports / "whatif-2026-10-12.json").write_text(json.dumps({"day": "2026-10-12", "rows": rows[:1]}),
                                                     encoding="utf-8")
    again = ledger.daily_report(st, SESSION, NOW, reports_dir=reports)
    assert ("what-if (today included): 3 refused entries replayed over 2 days: net +Rs 200, 2 won"
            in again.telegram.splitlines())
    assert ledger.WHATIF_PREFIX == __import__("titan2.whatif", fromlist=["x"]).REPORT_PREFIX
    st.close()


def test_session_impaired_reaches_the_phone_once(tmp_path, core):
    """A new IMPAIRED mark was an event only (10-13's marks after the 10:03 start never reached the phone). Now each
    event's new marks make one WARN SESSION_IMPAIRED; a mark already made is not sent again."""
    from test_engine import STRAT, Rig

    rig = Rig(tmp_path, start="2026-10-09T15:00:00+05:30")                   # Friday: heartbeats written
    rig.tick()
    rig.store.close()
    rig2 = Rig(tmp_path, clock=FakeClock("2026-10-13T10:05:00+05:30"))       # Tuesday 10:05: Monday and the
    rig2.tick()                                                               #   morning were missed
    (alert,) = rig2.alerts("SESSION_IMPAIRED")
    assert alert.severity == "WARN" and alert.dedup_key == f"session_impaired:2026-10-12:2026-10-13:{STRAT}"
    assert alert.text.startswith(f"G1 marked 2 sessions disturbed (IMPAIRED): 2026-10-12 for {STRAT}: heartbeat gap ")
    assert f"; 2026-10-13 for {STRAT}: heartbeat gap " in alert.text
    assert alert.text.endswith("Its trades still count, but a strategy whose IMPAIRED days pass 10% of the days it "
                               "traded cannot pass G1")
    assert len(rig2.events("SESSION_IMPAIRED")) == 2                          # the events are as before
    rig2.until("10:10:00", step=5.0)
    assert len(rig2.alerts("SESSION_IMPAIRED")) == 1                          # nothing new: nothing sent
    rig2.store.close()


def _failed_live_reads(rig) -> None:
    """The rig's broker: the LIVE account's positions and orders reads fail (no DNS), the others answer."""
    real = rig.reply

    def reply(c):
        snap = real(c)
        if isinstance(c, (FetchPositions, FetchOrders)) and c.account is Account.LIVE:
            cls = PositionsSnapshot if isinstance(c, FetchPositions) else OrdersSnapshot
            return cls(Account.LIVE, False, (), snap.req_mono, snap.recv_mono, "AMBIGUOUS:ConnectionError: getaddrinfo")
        return snap

    rig.reply = reply


@pytest.mark.parametrize("laptop_profile", [True, False])
def test_the_laptops_live_account_read_warnings_say_kite_account_check(tmp_path, core, laptop_profile):
    """On the laptop no LIVE order leaves titan2: its LIVE reads only check the Kite account, so their warnings say
    'Kite account check (no LIVE trading here)' instead of 'LIVE: ...'. The server's wording is unchanged."""
    from test_engine import Rig

    rig = Rig(tmp_path)
    rig.ctx.live_orders = not laptop_profile
    _failed_live_reads(rig)
    rig.tick()
    rig.until("10:01:00", step=1.0)
    (stale,) = rig.alerts("STALE_RECON")
    words = "Kite account check (no LIVE trading here)" if laptop_profile else "LIVE"
    assert stale.text.startswith(f"{words}: 3 broker reads failed in a row: AMBIGUOUS:ConnectionError")
    rig.auto_reads = False                                    # the next reads go unanswered: lost after 15 s
    rig.until("10:01:40", step=1.0)
    lost = {a.dedup_key: a.text for a in rig.alerts("READ_LOST")}
    assert lost["read_lost:LIVE:POSITIONS"].startswith(f"{words}: 1 POSITIONS read(s) unanswered for 15 s")
    assert lost["read_lost:PAPER:POSITIONS"].startswith("PAPER: 1 POSITIONS read(s) unanswered")   # as before
    assert [e["payload"]["account"] for e in rig.events("READ_LOST")][:1] == ["LIVE"]              # events unchanged
    rig.store.close()


def test_the_dashboards_events_tail_leaves_out_debug_rows(tmp_path, core):
    """pub/events_tail.json (the last 500 events): at the 10-14 close 227 of them were heartbeats and the morning had
    dropped out. DEBUG rows (HEARTBEAT, LATENCY, PREOPEN_VOLUME) stay in titan2.db and the exports, not in the tail."""
    st = store_mod.Store(tmp_path / "db" / "titan2.db", backup_dir=tmp_path / "backups")
    t = ist_datetime(S, "09:20")
    with st.tx():
        st.append_event("ENTRY_REFUSED", wall=t, mono=0.0, payload={"gate": "SIZE"}, severity="INFO")
        for k in range(5):
            st.append_event("HEARTBEAT", wall=t + timedelta(minutes=k), mono=60.0 * k, severity="DEBUG")
            st.append_event("LATENCY", wall=t + timedelta(minutes=k), mono=60.0 * k, payload={"reads": {}},
                            severity="DEBUG")
        st.append_event("NO_BID", wall=t + timedelta(minutes=6), mono=360.0, severity="WARN")
    pub = tmp_path / "pub"
    seq = ledger.publish_events_tail(st, pub, t, n=2)
    doc = json.loads((pub / "events_tail.json").read_text(encoding="utf-8"))
    assert [e["kind"] for e in doc["events"]] == ["ENTRY_REFUSED", "NO_BID"] and seq == doc["events"][-1]["seq"]
    with st.tx():
        st.append_event("HEARTBEAT", wall=t + timedelta(minutes=7), mono=420.0, severity="DEBUG")
    (pub / "events_tail.json").unlink()
    assert ledger.publish_events_tail(st, pub, t, n=2, last_seq=seq) == seq        # a heartbeat alone: no rewrite
    assert not (pub / "events_tail.json").exists()
    assert [e["kind"] for e in st.events_tail(2)] == ["NO_BID", "HEARTBEAT"]       # the store's own tail: unchanged
    st.close()
