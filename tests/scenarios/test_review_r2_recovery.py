"""End-to-end regressions for review round 2 (2026-10-14; recon/recovery, money and order-path lenses), on the real
core and the fakes. Ported from the reviewers' reproductions, which asserted the defects.

1. A1: a PAPER leg whose qty the paper exchange had already closed became a permanent CONTRADICTION: the next start's
   recon added the leg's carried qty to today's orders, which cannot show an earlier day's close, so HALT(MISMATCH,
   ACCOUNT:PAPER) could never be acked and PAPER entries never resumed.
   a. A same-day restart after the paper square-off (the bot died at 15:10, back at 15:35): recon after 15:31 ran only
      the post-start rounds, inside the start grace, and DONE ended the run with the square-off unbooked. A start at
      or after BROKER_SQUAREOFF_AT now keeps 5 s recon rounds while a leg is held and the broker differs, and holds
      the 15:33 truth check, the later EOD steps and DONE until the square-off is booked (bounded, 16:00).
   b. A weekday-holiday recovery-only run: the paper exchange settled the leftover at startup, dated the holiday. It
      settles only on trading days now: the next trading day's start settles it and recon books it from that day's
      orders.
   c. The backstop: a leftover the paper exchange closed on an earlier day that the bot never booked (here: back at
      16:30 and gone again two seconds later) is booked at the next start from the paper exchange's own record
      (FetchSquareoffs), at the record's price; with no record at all it closes UNRESOLVED (HALT(MISMATCH), which
      can be acked once recon is CLEAN).
2. A2: a leftover settled at the next start failed that day's 15:33 truth check (its trade is booked to its own
   session, its settlement is today's): today's check now leaves such orders out and checks them against their own
   trade.
3. A7: an exit place that timed out (or was in flight at a crash) and was accepted late by Kite's OMS: a restart gave
   it no late-accept window, so it was NOT_PLACED 15 s after the restart and a second SELL went out (decision B2).
4. Follow-ups from the fidelity review of these fixes: the truth check compared a leftover closed without a broker
   order (P1); the EOD steps a late restart held after 15:40 ran with DONE in one Timer, so the exit came before the
   truth check's answers and the day's files (P2); a failed square-off record read was dropped after a rollback
   reload (P5).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import requests

from titan2 import g1
from titan2.broker.paper import PaperExchange
from titan2.broker.port import AMBIGUOUS, ReadFailed, format_error
from titan2.clock import ist_datetime
from titan2.core.engine import Engine
from titan2.core.events import Alert, FetchSquareoffs, Place
from titan2.model import Account, Role, parse_tag

from fakes import universe as uni
from fakes.paper import FaultyPaperExchange
from fakes.runner import ScenarioRunner
from fakes.scenario import enter, leg_of, orders_of, route_of, state_of, struct_of, wait_for

pytestmark = pytest.mark.scenario

REL = "NSE:RELIANCE"
BOOKED = ("CLOSED", "CLOSED_EXTERNAL")


def _paper_sqoff(r: ScenarioRunner, symbol: str) -> dict:
    """The paper exchange's own square-off order on the symbol (its record in paper_exchange.db)."""
    (row,) = [o for o in r._paper_rows() if o["symbol"] == symbol and o["placed_by"] == "EXCHANGE"
              and o["account"] == "PAPER"]
    return row


def _paper_entries_resume(r: ScenarioRunner) -> None:
    """No MISMATCH halt is left on the paper accounts, recon is CLEAN and a new PAPER intent is not refused by a
    halt."""
    halts = [(h.kind, h.scope) for h in r.halts()]
    assert not [x for x in halts if x[0] == "MISMATCH"], halts
    assert r.engine.state.broker[Account.PAPER].recon.status == "CLEAN"
    j = r.intent("NSE:INFY")
    route = route_of(r, j, "PAPER:price_spike_up")
    assert route is not None and route["gate"] != "HALTS", route


# ---------------------------------------------------------------------------------------------------------------
# 1a. the same-day late restart books the paper square-off before the truth check and DONE
# ---------------------------------------------------------------------------------------------------------------
def test_a_late_restart_books_the_paper_squareoff_the_same_day(tmp_path):
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=uni.PREV_SESSION, start="14:50:00",
                       warmup_until="14:52:00")
    try:
        i, s = enter(r)
        sid = s["structure_id"]
        sym, lot = leg_of(r, sid).inst.symbol, leg_of(r, sid).inst.lot_size
        r.run_until("15:10:00", dt=5.0)
        assert state_of(r, i) == "OPEN"
        r.crash()                                     # the laptop sleeps at 15:10 with the leg open
        r.run_until("15:35:00", dt=5.0)
        assert r.broker_net(sym) == lot               # the paper exchange died with the bot: still held
        r.boot()                                      # back at 15:35: the paper exchange squares off at once
        r.run_until("15:39:50", dt=0.25)
        row = struct_of(r, i)
        assert r.broker_net(sym) == 0 and row["state"] == "CLOSED_EXTERNAL", row["state"]
        (tr,) = [t for t in r.trades() if t["structure_id"] == sid]
        sq = _paper_sqoff(r, sym)
        assert (tr["exit_reason"], tr["pnl_status"]) == ("BROKER_SQUAREOFF", "FINAL")
        assert tr["exit_avg"] == pytest.approx(sq["avg_px"])
        # booked (the leg and its SHADOW placebo) before the 15:33 truth check, which the late start held back
        booked = [e["seq"] for e in r.events("BROKER_SQUAREOFF")]
        (truth,) = [e for e in r.events("PHASE") if e["payload"].get("step") == "TRUTH"]
        assert len(booked) == 2 and max(booked) < truth["seq"]
        checks = [e["payload"] for e in r.events("EOD_TRUTH_CHECK") if e["payload"]["account"] == "PAPER"]
        assert checks and all(c["ok"] for c in checks), checks
        assert not r.events("EOD_TRUTH_MISMATCH")
        r.run_until("15:45:00", dt=1.0)               # DONE at 15:40 still ends the run
        assert [c for _, c in r.exits] == [0] and r.proc is None
        r.next_session(uni.SESSION)                   # the next trading day starts clean
        r.run(30)
        _paper_entries_resume(r)
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 1b. a weekday-holiday recovery-only run settles nothing; the next trading day books the settlement
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def monday_holiday(monkeypatch):
    """Monday 2026-10-12 as a weekday holiday (the fake universe builds dumps around it): Friday 10-09 and Tuesday
    10-13 trade."""
    from titan2.clock import SESSIONS_FILE, SessionCalendar
    data = json.loads(Path(SESSIONS_FILE).read_text(encoding="utf-8"))
    data["years"]["2026"]["holidays"] = sorted(data["years"]["2026"]["holidays"] + [uni.SESSION.isoformat()])
    cal = SessionCalendar.from_dict(data)
    monkeypatch.setattr(uni, "CALENDAR", cal)
    return cal


def test_a_holiday_start_leaves_the_paper_leftover_to_the_next_trading_day(tmp_path, monday_holiday):
    fri, mon, tue = uni.PREV_SESSION, uni.SESSION, uni.NEXT_SESSION
    assert monday_holiday.is_trading_day(fri) and not monday_holiday.is_trading_day(mon)
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=fri)
    try:
        i, s = enter(r)
        sid = s["structure_id"]
        sym, lot = leg_of(r, sid).inst.symbol, leg_of(r, sid).inst.lot_size
        r.run(5)
        r.crash()                                     # down from Friday mid-day
        r.clock.advance_to(ist_datetime(mon, "08:45:00"))
        r.boot()                                      # titan2-bot.timer on the weekday holiday: recovery-only
        r.run(60)
        assert r.broker_net(sym) == lot and state_of(r, i) not in BOOKED   # nothing settles on a holiday
        assert not [o for o in r._paper_rows() if o["placed_by"] == "EXCHANGE"]
        assert not r.halts("MISMATCH")
        r.crash()                                     # the recovery-only run has ended
        r.clock.advance_to(ist_datetime(tue, "09:18:30"))
        r.boot()
        r.run_until("09:21:00", dt=0.25)
        r.run(30)
        row = struct_of(r, i)
        assert row["state"] == "CLOSED_EXTERNAL" and r.broker_net(sym) == 0
        (tr,) = [t for t in r.trades() if t["structure_id"] == sid]
        assert (tr["exit_reason"], tr["exit_avg"], tr["pnl_status"]) == ("BROKER_SQUAREOFF", 0.05, "FINAL")
        sq = _paper_sqoff(r, sym)
        assert sq["placed_ts"][:10] == tue.isoformat() and sq["status_msg"] == "SIM: missed square-off"
        _paper_entries_resume(r)
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 1c. the backstop: an earlier day's paper close is booked from the paper exchange's record
# ---------------------------------------------------------------------------------------------------------------
def _down_until_after_the_bound(r: ScenarioRunner, i) -> tuple[int, str]:
    """The leg is open at 15:10; the bot comes back at 16:30 (the paper exchange squares off at its first step) and
    is gone again two seconds later, before recon could confirm the close: the square-off of that day is unbooked."""
    _, s = i
    sid = s["structure_id"]
    sym = leg_of(r, sid).inst.symbol
    r.run_until("15:10:00", dt=5.0)
    r.crash()
    r.run_until("16:30:00", dt=60.0)
    r.boot()
    r.run(2.0, dt=0.25)
    assert r.broker_net(sym) == 0 and struct_of(r, i[0])["state"] not in BOOKED
    r.crash()
    return sid, sym


def test_an_earlier_days_paper_close_is_booked_from_the_paper_exchanges_record(tmp_path):
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=uni.PREV_SESSION, start="14:50:00",
                       warmup_until="14:52:00")
    try:
        i = enter(r)
        sid, sym = _down_until_after_the_bound(r, i)
        sq = _paper_sqoff(r, sym)
        assert sq["placed_ts"][:10] == uni.PREV_SESSION.isoformat()
        r.next_session(uni.SESSION)
        wait_for(r, lambda: struct_of(r, i[0])["state"] in BOOKED, 30, "the leftover to be booked")
        (tr,) = [t for t in r.trades() if t["structure_id"] == sid]
        assert (tr["exit_reason"], tr["pnl_status"]) == ("BROKER_SQUAREOFF", "FINAL")
        assert tr["exit_avg"] == pytest.approx(sq["avg_px"])           # the record's own price, never better
        (ext,) = orders_of(r, sid, "EXTERNAL")
        assert (ext["broker_order_id"], ext["external_kind"]) == (sq["broker_order_id"], "AUTO_SQUAREOFF")
        assert [e for e in r.events("SQUAREOFF_RECORD") if e["structure_id"] == sid]
        r.run(30)
        assert not [e for e in r.events("MISMATCH") if e["account"] == "PAPER"]
        _paper_entries_resume(r)
        day = uni.SESSION.isoformat()
        assert g1.count_mismatches(r.store, "PAPER:price_spike_up", "PAPER", day, day) == 0
    finally:
        r.close()


def test_an_earlier_days_paper_close_without_a_record_closes_unresolved(tmp_path, monkeypatch):
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=uni.PREV_SESSION, start="14:50:00",
                       warmup_until="14:52:00")
    try:
        i = enter(r)
        sid, sym = _down_until_after_the_bound(r, i)
        real = PaperExchange.squareoffs

        def no_record(self, account, symbols, since):
            _, carried = real(self, account, symbols, since)
            return (), carried                        # a paper_exchange.db that lost its earlier days' orders

        monkeypatch.setattr(FaultyPaperExchange, "squareoffs", no_record)
        r.next_session(uni.SESSION)
        wait_for(r, lambda: struct_of(r, i[0])["state"] in BOOKED, 30, "the leftover to be closed")
        row = struct_of(r, i[0])
        (tr,) = [t for t in r.trades() if t["structure_id"] == sid]
        assert (row["state"], tr["pnl_status"]) == ("CLOSED_EXTERNAL", "UNRESOLVED")
        assert "MISMATCH" in r.alert_codes("CRITICAL")
        (h,) = r.halts("MISMATCH", "ACCOUNT:PAPER")
        wait_for(r, lambda: r.engine.state.broker[Account.PAPER].recon.status == "CLEAN", 30, "recon CLEAN")
        cid = r.command("ACK_HALT", args={"halt_id": h.halt_id, "note": "checked the paper ledger"})
        r.run(3)
        (ack,) = r.rows("SELECT status FROM command_acks WHERE source = 'CLI' AND cmd_id = ?", (cid,))
        assert ack["status"] == "DONE" and not r.halts("MISMATCH", "ACCOUNT:PAPER")
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 2. the truth check of a day whose start settled an earlier session's leftover
# ---------------------------------------------------------------------------------------------------------------
def test_the_truth_check_of_a_day_that_settled_a_leftover_passes(tmp_path):
    """Ported from the review's recon/recovery probe (and R2-money's attribution probe)."""
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=uni.PREV_SESSION)
    try:
        i, s = enter(r)
        r.run(5)
        r.next_session(uni.SESSION)                   # the bot died mid-day; the next start is the next session
        wait_for(r, lambda: state_of(r, i) == "CLOSED_EXTERNAL", 30)
        (tr,) = [t for t in r.trades() if t["structure_id"] == s["structure_id"]]
        assert (tr["session"], tr["exit_reason"], tr["exit_avg"]) == (uni.PREV_SESSION.isoformat(),
                                                                       "BROKER_SQUAREOFF", 0.05)
        r.run_until("15:34:00", dt=5.0)               # past the 15:33 truth check
        checks = [e["payload"] for e in r.events("EOD_TRUTH_CHECK") if e["payload"]["account"] == "PAPER"]
        assert checks and all(c["ok"] for c in checks), checks
        (c,) = checks
        assert c["carried"] and c["carried"][0]["session"] == uni.PREV_SESSION.isoformat()
        assert [b for b in c["books"] if b.get("session") == uni.PREV_SESSION.isoformat() and b["diff"] == 0.0]
        day = uni.SESSION.isoformat()
        n = {b: g1.count_mismatches(r.store, b, "PAPER", day, day)
             for b in ("PAPER:price_spike_up", "PAPER:opening_drive", "PAPER:price_spike_down")}
        assert not any(n.values()), n
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 3. a restart inside the late-accept window never sells twice (LIVE: paper never times out)
# ---------------------------------------------------------------------------------------------------------------
def _accept_late(r: ScenarioRunner, role: Role, delay_s: float) -> list:
    """The first `role` place times out at the client; Kite's OMS registers it delay_s later."""
    real_place = r.kite.place_order
    held: list = []

    def place_order(**kw):
        p = parse_tag(kw.get("tag"))
        if p is not None and p.role is role and not held:
            held.append([r.clock.mono() + delay_s, kw])
            raise requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='api.kite.trade'): Read timed out.")
        return real_place(**kw)

    def release(runner: ScenarioRunner) -> None:
        if held and held[0][1] is not None and runner.clock.mono() >= held[0][0]:
            kw, held[0][1] = held[0][1], None
            real_place(**kw)

    r.kite.place_order = place_order
    r.after_step.append(release)
    return held


def _stop_quote(r: ScenarioRunner, sid: int) -> tuple[float, float]:
    p = r.engine.state.structures[sid].exit_params
    bid = round(math.floor(p.hard_sl_px * 0.9 / 0.05) * 0.05, 2)
    return bid, round(bid + 0.2, 2)


def _sold_once(r: ScenarioRunner, sym: str, net: list) -> None:
    sells = [x for x in r.kite.orders_by_id.values() if x.symbol == sym and x.side == "SELL" and x.filled]
    assert len(sells) == 1, "sold twice: a second SELL went out inside the 30 s late-accept window"
    assert min(n for _, n in net) >= 0, "the account went short (G3)"


def test_a_restart_inside_the_late_window_never_sells_twice(tmp_path):
    """Ported from the review's order-path probe: the SELL times out, the bot crashes a second later and restarts
    5 s after that (systemd RestartSec=5); the OMS takes the timed-out SELL 25 s after the answer."""
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        i, s = enter(r, REL)
        sid = s["structure_id"]
        sym = leg_of(r, sid).inst.symbol
        held = _accept_late(r, Role.EXIT, 25.0)
        r.market.quote(sym, *_stop_quote(r, sid))         # HARD_SL: the first SELL times out
        assert r.run_until_true(lambda: bool(held), 10.0), "no EXIT place was attempted"
        answered = r.clock.mono()
        r.run(1.0)                                        # the AMBIGUOUS answer is in: the order is UNKNOWN
        assert [o["state"] for o in r.orders(role="EXIT")] == ["UNKNOWN"]
        r.restart(downtime_s=5.0)
        net: list[tuple[float, int]] = []
        r.after_step.append(lambda rr: net.append((round(rr.clock.mono() - answered, 2), rr.kite.net(sym))))
        r.run(60.0)
        _sold_once(r, sym, net)
        assert r.kite.net(sym) == 0                       # the late-accepted SELL, or the attempt after it, sold it
    finally:
        r.close()


def test_a_crash_while_the_exit_place_is_in_flight_never_sells_twice(tmp_path):
    """Ported from the review's order-path probe: the process dies while the EXIT place is in flight (SUBMITTING at
    the restart, its answer lost); the OMS registers that SELL 26 s after it was sent."""
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        i, s = enter(r, REL)
        sid = s["structure_id"]
        sym = leg_of(r, sid).inst.symbol
        held = _accept_late(r, Role.EXIT, 26.0)
        r.crash_on = lambda c: ("after_send" if isinstance(c, Place) and c.req.role is Role.EXIT
                                and c.account is Account.LIVE else None)
        r.market.quote(sym, *_stop_quote(r, sid))         # HARD_SL: the SELL is sent, the process dies
        for _ in range(400):
            if r.proc is None:
                break
            r.step()
        assert r.proc is None and held, "the crash after the EXIT send did not happen"
        sent = held[0][0] - 26.0
        r.run(5.0)                                        # systemd RestartSec=5
        r.boot()
        assert [o["state"] for o in r.orders(role="EXIT")] == ["SUBMITTING"]
        net: list[tuple[float, int]] = []
        r.after_step.append(lambda rr: net.append((round(rr.clock.mono() - sent, 2), rr.kite.net(sym))))
        r.run(60.0)
        _sold_once(r, sym, net)
        assert r.kite.net(sym) == 0
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 4. follow-ups to round 2 (the fidelity review of its fixes)
# ---------------------------------------------------------------------------------------------------------------
def _events(r: ScenarioRunner, *kinds: str) -> list[dict]:
    """titan2.db's events of these kinds, payloads parsed (also after the process has exited)."""
    rows = r.rows(f"SELECT seq, kind, ts, payload FROM events WHERE kind IN ({','.join('?' * len(kinds))}) "
                  "ORDER BY seq", kinds)
    return [dict(e, payload=json.loads(e["payload"]) if e["payload"] else {}) for e in rows]


def test_the_truth_check_leaves_out_a_leftover_closed_without_a_broker_order(tmp_path, monkeypatch):
    """P1, ported from the fidelity review's probe: the no-record backstop closes the leftover UNRESOLVED (an
    EX:<leg_id>:1 row, no broker order). That day's 15:33 check took it as a carried check of its own, which passed
    here only because the close is unpriced (Rs 0 on both sides) and failed whenever the close had a price. No
    tradebook can show that close: it stays out of the check (it is reported as unresolved: MISMATCH)."""
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=uni.PREV_SESSION, start="14:50:00",
                       warmup_until="14:52:00")
    try:
        i = enter(r)
        sid, sym = _down_until_after_the_bound(r, i)
        real = PaperExchange.squareoffs

        def no_record(self, account, symbols, since):
            _, carried = real(self, account, symbols, since)
            return (), carried

        monkeypatch.setattr(FaultyPaperExchange, "squareoffs", no_record)
        r.next_session(uni.SESSION)
        wait_for(r, lambda: struct_of(r, i[0])["state"] in BOOKED, 30, "the leftover to be closed")
        (ext,) = orders_of(r, sid, "EXTERNAL")
        assert ext["broker_order_id"] is None and ext["order_pk"].startswith("EX:")
        r.run_until("15:34:00", dt=5.0)
        checks = [e["payload"] for e in _events(r, "EOD_TRUTH_CHECK")]
        assert sorted(c["account"] for c in checks) == ["PAPER", "SHADOW"]
        assert all(c["ok"] and not c.get("carried") for c in checks), checks
    finally:
        r.close()


@pytest.mark.parametrize("boot_at", ["15:39:55", "15:30:00"], ids=["booked-after-1540", "booked-before-1533"])
def test_a_late_restart_finishes_the_days_checks_and_files_before_it_exits(tmp_path, boot_at):
    """P2, ported from the fidelity review's probe. Back at 15:39:55, the square-off is booked after DONE's 15:40,
    so TRUTH, BAR_CHECK, EOD_TASKS and DONE ran in one Timer. In production core_loop stops on that batch's Exit(0)
    (this runner goes on): the tradebook answers were never handled (no truth check that day), and the files
    EOD_TASKS marks, which _publish writes at the next handle(), were never written (no exports, no daily report
    or DAILY_REPORT alert, no g1.json or trades_90d). Now each step waits for what the steps before it sent: the
    nightly G1 after the truth check, the exit after the files. Back at 15:30, the steps keep their own times."""
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=uni.PREV_SESSION, start="14:50:00",
                       warmup_until="14:52:00")
    try:
        enter(r)
        r.run_until("15:10:00", dt=5.0)
        r.crash()
        r.run_until(boot_at, dt=5.0)
        r.boot()
        r.run_until("15:46:00", dt=0.25)
        assert [c for _, c in r.exits] == [0] and r.proc is None
        steps = {e["payload"]["step"]: e for e in _events(r, "PHASE") if e["payload"].get("step")}
        truth = _events(r, "EOD_TRUTH_CHECK")
        assert sorted(e["payload"]["account"] for e in truth) == ["PAPER", "SHADOW"]
        assert all(e["payload"]["ok"] for e in truth), [e["payload"] for e in truth]
        assert max(e["seq"] for e in truth) < steps["EOD_TASKS"]["seq"] < steps["DONE"]["seq"]
        sent = [s.cmd.code if isinstance(s.cmd, Alert) else type(s.cmd).__name__ for s in r.sent]
        assert "DAILY_REPORT" in sent and sent.index("DAILY_REPORT") < sent.index("Exit")
        day = uni.PREV_SESSION.isoformat()
        assert (r.tmp / "reports" / f"{day}.md").is_file()
        assert (r.tmp / "exports" / "events" / f"{day}.jsonl.gz").is_file()
        if boot_at == "15:30:00":                     # the normal timeline: every step at its own time
            assert {k: steps[k]["ts"][11:23] for k in ("TRUTH", "BAR_CHECK", "EOD_TASKS", "DONE")} == {
                "TRUTH": "15:33:00.000", "BAR_CHECK": "15:36:00.000", "EOD_TASKS": "15:38:00.000",
                "DONE": "15:40:00.000"}
    finally:
        r.close()


@pytest.mark.parametrize("failures", [1, 3])
def test_a_failed_squareoff_record_read_after_a_reload_is_still_asked_again(tmp_path, monkeypatch, failures):
    """P5: the backstop's FetchSquareoffs was remembered in EngineState, which a rollback reload rebuilds empty, so
    a failed answer that came after a reload was dropped: no retry and no STALE_STRUCTURE alert, and the leftover
    stayed recon's contradiction until a restart. The ask is kept in EngineContext now (it outlives a reload): one
    failure is asked again and the record books the leftover; three in a row raise the CRITICAL STALE_STRUCTURE."""
    r = ScenarioRunner(tmp_path / "r", Account.PAPER, session=uni.PREV_SESSION, start="14:50:00",
                       warmup_until="14:52:00")
    try:
        i = enter(r)
        sid, sym = _down_until_after_the_bound(r, i)
        sq = _paper_sqoff(r, sym)
        real, failed, reloaded = PaperExchange.squareoffs, [], []

        def flaky(self, account, symbols, since):
            if Account(account) is Account.PAPER and len(failed) < failures:
                failed.append(tuple(symbols))
                raise ReadFailed(format_error(AMBIGUOUS, "OperationalError", "database is locked"))
            return real(self, account, symbols, since)

        def reload_before_its_answer(runner: ScenarioRunner, c) -> None:
            if isinstance(c, FetchSquareoffs) and c.account is Account.PAPER and not reloaded:
                reloaded.append(runner.proc.ctx.recovered)          # READY came first: a reload as after a
                runner.proc.engine = Engine.load(runner.proc.store, runner.proc.ctx)   # rolled-back event

        monkeypatch.setattr(FaultyPaperExchange, "squareoffs", flaky)
        r.dispatch_hooks.append(reload_before_its_answer)
        r.next_session(uni.SESSION)
        r.run(10)
        assert reloaded == [True] and len(failed) == failures
        asks = r.commands(FetchSquareoffs)
        if failures == 1:
            wait_for(r, lambda: struct_of(r, i[0])["state"] in BOOKED, 30, "the leftover to be booked")
            (tr,) = [t for t in r.trades() if t["structure_id"] == sid]
            assert (tr["exit_reason"], tr["pnl_status"]) == ("BROKER_SQUAREOFF", "FINAL")
            assert tr["exit_avg"] == pytest.approx(sq["avg_px"])
            assert len(asks) == 2
        else:
            assert len(asks) == 3 and struct_of(r, i[0])["state"] not in BOOKED
            (a,) = [a for _, a in r.alerts if a.code == "STALE_STRUCTURE" and a.dedup_key.startswith("squareoffs:")]
            assert a.severity == "CRITICAL" and sym in a.text
    finally:
        r.close()
