"""The deploy's market-hours rule holds when titan2-bot is touched, not only when the deploy starts (DETAIL §13.6 2a).

An install spends minutes between its first check and the restart of titan2-bot (a new venv, the backup, the deploy
self-check). A strategy can enter in that time, or the bot can start (the 08:45 timer). ops/release.sh therefore
checks the rule again just before the switch and again just before `systemctl restart titan2-bot`; a rollback checks
it again just before the restart. These tests change the bot's state in the middle of a deploy through hooks in the
fake host (opsharness): the fake sudo while it runs the self-check, and the fake systemctl when it restarts
titan2-dash (the last step before the bot's restart)."""

from __future__ import annotations

import shlex
import subprocess

from opsharness import BOT_READY_THEN_EXIT0, MONDAY_10, REL_A, REL_B, needs_bash, unallowed_sudo_calls

pytestmark = needs_bash

MARKET = {"TITAN2_OPS_NOW": MONDAY_10}                 # a weekday at 10:00 IST, inside the 08:40-15:45 window
BOT_RESTART = "sudo -n systemctl restart titan2-bot"
OPENS_A_LEG = 'echo OPEN_LEGS=1 >> "$FAKE_STATE/status.env"'
DASH_RESTART_THEN = 'echo active > "$FAKE_STATE/units/titan2-dash.state"\n{}\n'


def last_lines(r: subprocess.CompletedProcess) -> str:
    return r.stdout[-3000:] + "\n--- stderr ---\n" + r.stderr[-3000:]


def hook_sudo(srv, pattern: str, action: str | None) -> None:
    """Run the bash `action` whenever the fake sudo gets a command line containing `pattern` (None: no hook)."""
    sudo, orig = srv.bin / "sudo", srv.bin / "sudo.orig"
    if not orig.exists():
        sudo.rename(orig)
    if action is None:
        sudo.unlink()
        orig.rename(sudo)
        return
    sudo.write_text("#!/usr/bin/env bash\n"
                    f'case "$*" in *{shlex.quote(pattern)}*) {action} ;; esac\n'
                    'exec "$FAKE_BIN/sudo.orig" "$@"\n', encoding="utf-8", newline="\n")
    sudo.chmod(0o755)


def running_flat(srv) -> None:
    """titan2-bot is running and flat: the deploy's first check passes with --allow-market-hours."""
    srv.unit("titan2-bot", state="active", result="success")
    srv.status(OPEN_LEGS=0, NONTERMINAL=0)
    srv.clear_calls()


def test_a_position_opened_during_the_self_check_stops_the_deploy_before_the_switch(installed):
    srv = installed
    running_flat(srv)
    hook_sudo(srv, "selfcheck --phase deploy", OPENS_A_LEG)      # a strategy enters while the deploy self-checks
    r = srv.install(REL_B, "--allow-market-hours", "--restart", env=MARKET, lock="# fake lock B\n")   # new venv
    assert r.returncode == 2, last_lines(r)
    assert "1 open leg(s)" in r.stderr, last_lines(r)
    assert (srv.state / "selfcheck.log").read_text().count(f"/releases/{REL_B}|") == 1   # refused after it ran
    calls = srv.calls()
    assert BOT_RESTART not in calls and "sudo -n systemctl restart titan2-dash" not in calls
    assert srv.current() == REL_A and srv.history() == [["deploy", "-", REL_A]]
    assert srv.unit_state("titan2-bot") == "active" and srv.alerts() == []
    assert (srv.root / "releases" / REL_B / "MANIFEST.sha256").is_file()      # prepared, kept for the next try

    # once the bot is flat again the same deploy goes through and reuses the prepared release
    hook_sudo(srv, "", None)
    running_flat(srv)
    r = srv.install(REL_B, "--allow-market-hours", "--restart", env=MARKET, lock="# fake lock B\n")
    assert r.returncode == 0, last_lines(r)
    assert "identical MANIFEST: reusing it" in r.stdout
    assert "(again, just before the switch)" in r.stdout and "(again, just before restarting titan2-bot)" in r.stdout
    assert srv.current() == REL_B and srv.calls().count(BOT_RESTART) == 1
    assert unallowed_sudo_calls(srv) == []


def test_a_bot_started_during_the_deploy_is_held_to_the_market_hours_rule(installed):
    srv = installed                                     # titan2-bot stopped: the first check has nothing to protect
    assert srv.unit_state("titan2-bot") == "inactive"
    srv.status(OPEN_LEGS=0, NONTERMINAL=0)
    hook_sudo(srv, "selfcheck --phase deploy", 'echo active > "$FAKE_STATE/units/titan2-bot.state"')   # the timer
    srv.clear_calls()
    r = srv.install(REL_B, "--restart", env=MARKET)     # no --allow-market-hours
    assert r.returncode == 2, last_lines(r)
    assert "titan2-bot is inactive: ok" in r.stdout and "pass --allow-market-hours" in r.stderr, last_lines(r)
    assert BOT_RESTART not in srv.calls()
    assert srv.current() == REL_A and srv.history() == [["deploy", "-", REL_A]]


def test_a_position_opened_after_the_switch_is_never_restarted_and_the_switch_is_undone(installed):
    srv = installed
    running_flat(srv)
    srv.on_restart("titan2-dash", DASH_RESTART_THEN.format(OPENS_A_LEG))  # enters while the dashboard restarts
    r = srv.install(REL_B, "--allow-market-hours", "--restart", env=MARKET)
    assert r.returncode == 1, last_lines(r)
    assert "titan2-bot NOT restarted: titan2-bot is active with 1 open leg(s)" in r.stderr, last_lines(r)
    calls = srv.calls()
    assert BOT_RESTART not in calls and srv.unit_state("titan2-bot") == "active"
    assert srv.current() == REL_A                       # the running bot's release is current again
    assert srv.history()[-2:] == [["deploy", REL_A, REL_B], ["autorollback", REL_B, REL_A]]
    alerts = srv.alerts()
    assert len(alerts) == 1 and f"rolled back to {REL_A}" in alerts[0], alerts
    assert "titan2-bot was not restarted" in alerts[0] and "1 open leg(s)" in alerts[0], alerts
    assert unallowed_sudo_calls(srv) == []


def test_a_rollback_does_not_restart_a_bot_that_opened_a_position_meanwhile(installed):
    srv = installed
    srv.on_restart("titan2-bot", BOT_READY_THEN_EXIT0)  # Sunday: B installed, the bot had nothing to do
    r = srv.install(REL_B)
    assert r.returncode == 0 and srv.current() == REL_B, last_lines(r)
    srv.on_restart("titan2-bot", None)

    running_flat(srv)                                   # Monday 10:00: B runs flat, the operator rolls back to A
    srv.on_restart("titan2-dash", DASH_RESTART_THEN.format(OPENS_A_LEG))
    r = srv.run("release.sh", "rollback", "--allow-market-hours", "--restart", env=MARKET)
    assert r.returncode == 1, last_lines(r)
    assert BOT_RESTART not in srv.calls() and srv.unit_state("titan2-bot") == "active"
    assert srv.current() == REL_A and srv.history()[-1] == ["rollback", REL_B, REL_A]
    alerts = srv.alerts()
    assert len(alerts) == 1 and "titan2-bot was NOT restarted" in alerts[0] and "1 open leg(s)" in alerts[0], alerts
    assert unallowed_sudo_calls(srv) == []
