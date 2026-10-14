"""The laptop profile end to end on the real core (review findings on laptop mode, 2026-10-12).

1. PAPER only means no LIVE order at all. The laptop shares the Zerodha account with EC2 but not EC2's titan2.db:
   today's titan2 LIVE orders in that account are EC2's. The laptop never imports them (recovery halts ACCOUNT:LIVE
   with UNKNOWN_POSITION and one CRITICAL alert instead), and its KiteBroker refuses every LIVE place and cancel, so
   neither its Ctrl+C FLATTEN_ALL, its EOD flatten nor its stop can sell EC2's position or leave it net short.
2. The first Ctrl+C never turns entries back on before the stop: the FLATTEN_ALL goes out on every first press, also
   with nothing open, no RESUME follows its DONE, and the engine clears that FLATTEN halt itself once the run ends
   flat (Shutdown, or the EOD exit), so the next start trades again. A Ctrl+C FLATTEN_ALL the bot never read expires
   instead of running at a later start; a second press that stops before flat keeps the halt (`titan2 resume`).

Single-account runs on the real laptop.Supervisor (titan2.__main__ as its CLI module) where the press matters; the
runtime's poll_stop is modelled by posting Shutdown(reason) at the back of core_q.
"""

from __future__ import annotations

import pytest

import titan2.__main__ as cli
from titan2 import laptop, runtime
from titan2.core import operator
from titan2.core.events import Cancel, Place, Shutdown
from titan2.model import Account, Role

from fakes.runner import ScenarioRunner
from fakes.scenario import enter, wait_for

pytestmark = pytest.mark.scenario

REL, INFY, SBIN = "NSE:RELIANCE", "NSE:INFY", "NSE:SBIN"
BY = f"CLI:{laptop.REQUESTED_BY}"
OPEN_SQL = "SELECT structure_id, book, state FROM structures WHERE state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID')"
ORDER_CALLS = ("place_order", "cancel_order", "modify_order")


class _Bot:
    """A BotProcess as the laptop Session sees it: its StopRequest (the runtime's poll_stop turns it into Shutdown)."""

    def __init__(self) -> None:
        self.stop = runtime.StopRequest()


def _ack(r: ScenarioRunner, cmd_id: int) -> str | None:
    rows = r.rows("SELECT status FROM command_acks WHERE source = 'CLI' AND cmd_id = ?", (cmd_id,))
    return rows[0]["status"] if rows else None


def _flatten_halts(r: ScenarioRunner) -> list[dict]:
    """Every FLATTEN halt row (the file is read when the process is down)."""
    return r.rows("SELECT halt_id, reason, cleared_ts, cleared_by FROM halts WHERE kind = 'FLATTEN' ORDER BY halt_id")


def _supervised(r: ScenarioRunner) -> tuple[laptop.CtrlC, laptop.Supervisor, _Bot, list[str]]:
    ctl, session, bot, out = laptop.CtrlC(), laptop.Session(), _Bot(), []
    session.bind(bot)
    sup = laptop.Supervisor(ctl=ctl, session=session, var=r.tmp, clock=r.clock, out=out.append, cli_mod=cli,
                            mono=r.clock.mono, power=lambda: laptop.PowerStatus(on_ac=True, battery_pct=90))
    r.enable_commands()
    return ctl, sup, bot, out


def _until_stop_requested(r: ScenarioRunner, sup: laptop.Supervisor, bot: _Bot, timeout_s: float = 60.0) -> None:
    """Real cadences: the runner steps 50 ms and thread "laptop" passes every POLL_S."""
    every = max(1, round(laptop.POLL_S / r.dt))
    end, k = r.clock.mono() + timeout_s, 0
    while bot.stop.reason is None:
        assert r.clock.mono() < end, "the Supervisor never requested the stop"
        if k % every == 0:
            sup.step()
        r.step()
        k += 1


def _shutdown(r: ScenarioRunner, reason: str) -> None:
    """Runtime.poll_stop: one Shutdown at the back of core_q; the engine's Exit(0) ends the process."""
    r.proc.core_q.append(Shutdown(reason))
    r.settle()
    wait_for(r, lambda: r.proc is None, 30, "the bot to exit")


def _live_orders_sent(r: ScenarioRunner, since: int) -> list:
    return [s.cmd for s in r.sent[since:] if isinstance(s.cmd, (Place, Cancel)) and s.cmd.account is Account.LIVE]


def _kite_order_calls(r: ScenarioRunner, since: int) -> list[str]:
    return [name for _, name, _ in r.kite.calls[since:] if name in ORDER_CALLS]


# ---------------------------------------------------------------------------------------------------------------
# 1. No LIVE order from the laptop
# ---------------------------------------------------------------------------------------------------------------
def test_laptop_never_trades_another_titan2s_live_position(tmp_path):
    """EC2 trades LIVE, stops, and the laptop takes over the shared Kite account the same afternoon with its own
    titan2.db. The laptop imports nothing of EC2's: HALT(UNKNOWN_POSITION, ACCOUNT:LIVE) and a CRITICAL alert that
    say so. Its Ctrl+C FLATTEN_ALL, its EOD flatten and its stop place and cancel nothing on LIVE: EC2's position is
    left exactly as it was, for Kite or EC2 to manage."""
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)          # EC2, trading LIVE today
    try:
        _, s = enter(r)
        sym = r.rows("SELECT symbol FROM legs WHERE structure_id = ?", (s["structure_id"],))[0]["symbol"]
        held = r.broker_net(sym)
        assert held > 0 and r.kite.api_orders()
        r.crash()                                               # EC2 stops
        for p in r.db_path.parent.glob("titan2.db*"):
            p.unlink()                                          # the laptop's titan2.db holds nothing of EC2's
        r.run_until("14:30:00", dt=30.0)
        r.laptop_profile()                                      # BotProcess(laptop=True)
        sent, calls = len(r.sent), len(r.kite.calls)
        r.boot()
        r.run(5)
        assert not [x for x in r.structures() if x["account"] == "LIVE"]       # nothing imported, no RECOVERED:LIVE
        (halt,) = r.halts("UNKNOWN_POSITION", "ACCOUNT:LIVE")
        assert halt.reason.startswith("titan2 LIVE orders exist in this Kite account today (1 order(s) on ")
        assert "manage them in Kite or on EC2" in halt.reason
        alerts = [a for _, a in r.alerts if a.dedup_key == "recovery:LIVE:not_imported"]
        assert [(a.severity, a.code) for a in alerts] == [("CRITICAL", "UNKNOWN_POSITION")]
        assert [e for e in r.events("UNKNOWN_POSITION") if e["payload"].get("not_imported")]
        c = r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)       # the laptop's first Ctrl+C
        wait_for(r, lambda: _ack(r, c) == "DONE", 30, "the Ctrl+C flatten")
        r.run_until("15:22:00", dt=1.0)                         # the EOD flatten and STOP_SENDING pass too
        _shutdown(r, "CTRL_C")
        assert _live_orders_sent(r, sent) == [] and _kite_order_calls(r, calls) == []
        assert r.broker_net(sym) == held                        # EC2's position is untouched
    finally:
        r.close()


def test_laptop_refuses_live_orders_even_for_a_live_leg_in_its_own_db(tmp_path):
    """Defence in depth: EC2's titan2.db copied onto the laptop (a LIVE leg in its own DB). The exit engine still
    tries the FLATTEN_ALL's exits, but the laptop's KiteBroker refuses each one locally: every LIVE EXIT order ends
    REJECTED "LOCAL: laptop profile: no LIVE orders" and Kite sees no order call."""
    r = ScenarioRunner(tmp_path / "run", Account.LIVE)
    try:
        _, s = enter(r)
        sym = r.rows("SELECT symbol FROM legs WHERE structure_id = ?", (s["structure_id"],))[0]["symbol"]
        held = r.broker_net(sym)
        r.crash()
        r.laptop_profile()
        calls = len(r.kite.calls)
        r.boot()
        r.run(3)
        r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)
        r.run(20)
        exits = r.orders(role="EXIT", account=Account.LIVE)
        assert exits and r.places(Role.EXIT, account=Account.LIVE)
        assert {(o["state"], o["status_msg"]) for o in exits} == {("REJECTED", "LOCAL: laptop profile: no LIVE orders")}
        assert _kite_order_calls(r, calls) == [] and r.broker_net(sym) == held
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------------------------
# 2. The first Ctrl+C: entries stay off until the stop
# ---------------------------------------------------------------------------------------------------------------
def test_first_ctrl_c_keeps_entries_off_until_the_stop(tmp_path):
    r = ScenarioRunner(tmp_path / "run", Account.PAPER, laptop=True)
    try:
        _, first = enter(r)                                     # a paper position is open
        ctl, sup, bot, out = _supervised(r)
        ctl.press("SIGINT")                                     # the first Ctrl+C
        _until_stop_requested(r, sup, bot)
        assert bot.stop.reason == "CTRL_C" and any(line.startswith("verified flat") for line in out)
        assert [row.kind for row in operator.read_new(r.cli_db, "CLI", 0)] == ["FLATTEN_ALL"]    # never a RESUME
        assert r.halts("FLATTEN", "GLOBAL")                     # still up between DONE and the Shutdown
        # an intent the core handles in that gap (before the runtime posts Shutdown) is refused: nothing opens
        gap = r.intent(INFY)
        r.run(2)
        assert r.rows("SELECT structure_id FROM structures WHERE intent_id = ?", (gap.intent_id,)) == []
        routes = r.rows("SELECT book, outcome, gate FROM intent_routes WHERE intent_id = ?", (gap.intent_id,))
        assert routes and "RESERVED" not in {x["outcome"] for x in routes}
        assert {x["gate"] for x in routes if x["outcome"] == "REFUSED"} == {"HALTS"}
        _shutdown(r, bot.stop.reason)
        assert r.rows(OPEN_SQL) == []                           # verified flat really is flat at the exit
        (halt,) = _flatten_halts(r)                             # cleared by the engine as it stopped flat
        assert halt["reason"] == f"operator FLATTEN_ALL ({BY})" and halt["cleared_ts"] and halt["cleared_by"] == BY
        # the next start trades normally: no FLATTEN halt, no RESUME needed
        r.boot()
        r.run(75, dt=0.25)                                      # the feed warms, recon is clean twice
        assert not r.halts("FLATTEN")
        enter(r, SBIN)
    finally:
        r.close()


def test_first_ctrl_c_with_nothing_open_still_flattens_before_the_stop(tmp_path):
    """No 'nothing is open' shortcut: an intent arriving right after the press is either refused by the FLATTEN
    halt or flattened by the FLATTEN_ALL before the stop; the stop never leaves it open. With nothing open at all
    the halt does not mark the session IMPAIRED (a stop is the heartbeat gap's to judge)."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER, laptop=True)
    try:
        ctl, sup, bot, out = _supervised(r)
        ctl.press("SIGINT")
        sup.step()                                              # the FLATTEN_ALL is written
        gap = r.intent(REL)                                     # the core handles an intent before reading it
        _until_stop_requested(r, sup, bot)
        _shutdown(r, bot.stop.reason)
        assert r.rows(OPEN_SQL) == []
        states = {x["state"] for x in r.rows("SELECT state FROM structures WHERE intent_id = ?", (gap.intent_id,))}
        assert states <= {"CLOSED", "VOID"}
        assert all(x["cleared_by"] == BY for x in _flatten_halts(r))
    finally:
        r.close()
    r = ScenarioRunner(tmp_path / "quiet", Account.PAPER, laptop=True)
    try:
        ctl, sup, bot, out = _supervised(r)
        ctl.press("SIGINT")
        _until_stop_requested(r, sup, bot)
        assert any(line.startswith("verified flat (already flat") for line in out)
        _shutdown(r, bot.stop.reason)
        assert [x["cleared_by"] for x in _flatten_halts(r)] == [BY]
        assert r.rows("SELECT strategy FROM session_flags WHERE flag = 'IMPAIRED'") == []
    finally:
        r.close()


def test_second_press_before_flat_keeps_the_flatten_halt(tmp_path):
    """Stopped before verified flat: the FLATTEN halt stays, the next start keeps exiting and entries stay off until
    `titan2 resume` (an operator decision, as documented)."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER, laptop=True)
    try:
        _, s = enter(r)
        r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)
        wait_for(r, lambda: bool(r.halts("FLATTEN")), 5, "the FLATTEN halt")
        _shutdown(r, "CTRL_C_TWICE")                            # the second press, the exit still in flight
        (halt,) = _flatten_halts(r)
        assert halt["cleared_ts"] is None
        r.boot()
        wait_for(r, lambda: r.rows(OPEN_SQL) == [], 30, "the next start to finish the exits")
        r.run(75, dt=0.25)
        assert r.halts("FLATTEN")                               # entries stay off...
        r.command("RESUME", by="operator")
        wait_for(r, lambda: not r.halts("FLATTEN"), 5, "RESUME")
        enter(r, SBIN)                                          # ...until the operator resumes
    finally:
        r.close()


def test_an_unread_ctrl_c_flatten_never_runs_at_a_later_start(tmp_path):
    """A Ctrl+C FLATTEN_ALL written as the bot was ending (never read) expires like a non-operator command: the next
    start, 300 s or more later, runs nothing and raises no halt. Within that window it runs (an in-process restart)
    and its halt clears at that run's stop. An operator's own FLATTEN_ALL still waits for the bot and its halt stays
    until RESUME: EC2's semantics are unchanged."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER, laptop=True)
    try:
        r.enable_commands()
        r.crash()
        stale = r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)
        r.run(operator.EXPIRE_AFTER_S + 30, dt=5.0)
        r.boot()
        r.run(3)
        assert _ack(r, stale) == "EXPIRED" and not r.halts("FLATTEN")
        r.crash()
        fresh = r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)
        r.run(10, dt=1.0)                                       # the laptop restarts in-process within seconds
        r.boot()
        r.run(3)
        assert _ack(r, fresh) == "DONE" and r.halts("FLATTEN")
        _shutdown(r, "CTRL_C")
        assert [x["cleared_by"] for x in _flatten_halts(r)] == [BY]
        own = r.command("FLATTEN_ALL", confirm="FLATTEN", by="operator")    # `titan2 flatten-all`, bot down
        r.run(operator.EXPIRE_AFTER_S + 30, dt=5.0)
        r.boot()
        r.run(3)
        assert _ack(r, own) == "DONE" and r.halts("FLATTEN")
        _shutdown(r, "SIGTERM")
        assert [x["cleared_ts"] is None for x in _flatten_halts(r)] == [False, True]       # the operator's stays
    finally:
        r.close()


def test_a_ctrl_c_flatten_halt_left_by_a_crash_impairs_g1_once_it_censors(tmp_path):
    """The process dies between DONE and its Shutdown (a closed window, the laptop sleeping): the FLATTEN halt stays
    and keeps entries off at the next start, flat or not. The halt itself never marks the session IMPAIRED while it
    only bridges a stop, but once it has blocked PAPER entries for G1_IMPAIRED_GAP_S of the entry window it does,
    like a stopped bot."""
    from titan2.settings import G1_IMPAIRED_GAP_S

    r = ScenarioRunner(tmp_path / "run", Account.PAPER, laptop=True)
    try:
        c = r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)
        wait_for(r, lambda: _ack(r, c) == "DONE", 10, "the flatten")
        r.crash()                                               # no Shutdown: the halt is not cleared
        r.boot()
        r.run(G1_IMPAIRED_GAP_S - 60, dt=1.0)
        assert r.halts("FLATTEN") and r.rows("SELECT * FROM session_flags WHERE flag = 'IMPAIRED'") == []
        r.run(120, dt=1.0)
        flags = r.rows("SELECT strategy, detail FROM session_flags WHERE flag = 'IMPAIRED'")
        assert flags and all(f["detail"].startswith("FLATTEN [GLOBAL] (RESUME) blocked PAPER entries") for f in flags)
    finally:
        r.close()


def test_the_eod_exit_clears_the_ctrl_c_flatten_halt(tmp_path):
    """Ctrl+C at 15:39: the EOD timeline's DONE step (15:40) ends the run before any Shutdown. Flat, so the engine
    clears the laptop's FLATTEN halt there too: tomorrow's start is not blocked."""
    r = ScenarioRunner(tmp_path / "run", Account.PAPER, start="15:38:00", warmup_until="15:38:30", laptop=True)
    try:
        c = r.command("FLATTEN_ALL", confirm="FLATTEN", by=laptop.REQUESTED_BY)
        wait_for(r, lambda: _ack(r, c) == "DONE", 10, "the flatten")
        assert r.halts("FLATTEN")
        wait_for(r, lambda: r.proc is None, 180, "the EOD exit")
        assert [code for _, code in r.exits] == [0]
        assert [x["cleared_by"] for x in _flatten_halts(r)] == [BY]
    finally:
        r.close()
