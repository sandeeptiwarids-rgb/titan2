"""The ops scripts run end to end against temp directories (DETAIL §13.6, §13.8, §13.11, §16.7).

ops/release.sh is driven directly or through deploy.sh / rollback.sh / pull_drift.sh with fake ssh and scp; the
host is faked by opsharness (systemctl state machine, titan2 CLI, sudo, venv). Each test starts from a copy of a
server where release A was installed (fixture `installed`) or from an empty one (fixture `server`)."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from opsharness import (BOT_READY_THEN_EXIT0, BOT_START_FAILS, HOST, MONDAY_10, OPS, REL_A, REL_B, REL_C, SUNDAY, bpath,
                        build_tarball, needs_bash, release_files, sha256_file, snapshot, unallowed_sudo_calls)

pytestmark = needs_bash


def last_lines(r: subprocess.CompletedProcess) -> str:
    return (r.stdout[-3000:] + "\n--- stderr ---\n" + r.stderr[-3000:])


# ---------------------------------------------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------------------------------------------
def test_drift_aborts(installed):
    srv = installed
    rel = srv.root / "releases" / REL_A
    # The MANIFEST is the spec's literal check: `cd current && sha256sum -c --quiet MANIFEST.sha256`
    r = srv.sh(f'cd "{bpath(srv.root)}/current" && sha256sum -c --quiet MANIFEST.sha256')
    assert r.returncode == 0, r.stdout + r.stderr
    # Hand edits on the server: one modified file, one new file, one deleted file
    r = srv.sh(f'cd "{bpath(rel)}" && chmod u+w titan2 titan2/core titan2/core/mod.py && '
               f'echo "# hotfix" >> titan2/core/mod.py && echo "X = 1" > titan2/core/extra.py && rm -f titan2/__init__.py')
    assert r.returncode == 0, r.stderr

    r = srv.run("release.sh", "preflight")
    assert r.returncode == 2, last_lines(r)
    for line in ("M titan2/core/mod.py", "A titan2/core/extra.py", "D titan2/__init__.py"):
        assert line in r.stderr
    assert "pull_drift" in r.stderr

    # A deploy is refused before it changes anything
    sha = srv.upload(REL_B)
    before = snapshot(srv.root, srv.units)
    r = srv.run("release.sh", "install", REL_B, sha)
    assert r.returncode == 2, last_lines(r)
    assert "drifted" in r.stderr
    assert snapshot(srv.root, srv.units) == before
    assert srv.current() == REL_A
    assert not [c for c in srv.calls() if c.startswith(("sudo", "systemctl restart"))]

    # pull_drift.sh copies the M and A files home and never overwrites a local file
    laptop = srv.tmp / "laptop"
    mine = laptop / "drift" / REL_A / "titan2" / "core" / "extra.py"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine\n")
    r = srv.run("pull_drift.sh", HOST, cwd=laptop)
    assert r.returncode == 0, last_lines(r)
    assert (laptop / "drift" / REL_A / "titan2" / "core" / "mod.py").read_text().endswith("# hotfix\n")
    assert mine.read_text() == "mine\n"
    assert "titan2/core/extra.py" in r.stdout          # named as kept
    assert "D titan2/__init__.py" in r.stdout


# ---------------------------------------------------------------------------------------------------------------
# Ownership (G12)
# ---------------------------------------------------------------------------------------------------------------
_WRITES = re.compile(r"(?:^|[;&|({!]|\bthen\b|\bdo\b|\belse\b)\s*(?:sudo\s+-n\s+(?:-u\s+\w+\s+)?)?"
                     r"(rm|rmdir|mv|ln|cp|install|chmod|chown|mkdir|touch|tee|truncate|dd)\b(?!\))")
_TAR_X = re.compile(r"\btar\s+-x(?![a-zA-Z]*O)")
_PY_WRITES = re.compile(r"-m\s+(venv|pip|compileall)\b")
_REDIRECT = re.compile(r"\d*>>?\s*([^\s;)|]+)")


def _blank_quotes_and_comments(text: str) -> str:
    """The script with the contents of quoted strings, comments and (( )) arithmetic blanked; newlines kept."""
    out, quote, i = [], None, 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
                out.append(ch)
            else:
                out.append("\n" if ch == "\n" else " ")
        elif quote == '"':
            if ch == "\\" and i + 1 < len(text):
                out.append("  ")
                i += 1
            elif ch == '"':
                quote = None
                out.append(ch)
            else:
                out.append("\n" if ch == "\n" else " ")
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 1
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\n"):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r"\(\((.*?)\)\)", lambda m: "((" + " " * len(m.group(1)) + "))", "".join(out))


def _writes_outside_owned_helpers(script: Path) -> list[str]:
    code = _blank_quotes_and_comments(script.read_text(encoding="utf-8"))
    bad, func = [], None
    for n, line in enumerate(code.splitlines(), 1):
        m = re.match(r"^([a-z_]+)\(\)\s*\{", line)
        if m:
            func = m.group(1)
        hits = [w.group(1) for w in _WRITES.finditer(line)]
        hits += ["tar -x"] if _TAR_X.search(line) else []
        hits += [f"-m {w.group(1)}" for w in _PY_WRITES.finditer(line)]
        hits += [f">{t}" for t in (m.group(1) for m in _REDIRECT.finditer(line))
                 if t not in ("/dev/null",) and not t.startswith("&")]
        if hits and not (func or "").startswith("owned_"):
            bad.append(f"line {n} in {func or 'main'}: {hits}")
        if line.startswith("}") or (m and line.rstrip().endswith("}")):     # end of a function (or a one-liner)
            func = None
    return bad


def test_owned_paths_only(installed):
    srv = installed
    r_ = srv.root
    # 1. The rule itself: owned paths pass ...
    ok = [r_ / "incoming" / "release-x.tgz", r_ / "releases" / "abc" / "titan2" / "a.py", r_ / "venvs" / "h" / "bin",
          r_ / "current", r_ / "current.new", srv.units / "titan2-bot.service",
          srv.units / "titan2-bot.service.d" / "override.conf"]
    srv.make_link("../../var", r_ / "incoming" / "evil")                       # a link out of an owned directory
    srv.make_link("/dev/null", srv.units / "titan2-dash.service")             # a masked unit
    denied = [bpath(p) for p in (srv.var / "db" / "titan2.db", srv.etc / "titan2.env", r_ / "agentic_trader" / "titan2",
                                 r_ / "v1_frozen_20261009" / "autonomous_trader.py", r_ / "releases", r_ / "incoming",
                                 r_ / "incoming" / "evil" / "db" / "titan2.db", srv.units / "titan2-dash.service",
                                 srv.units / "titan-bot.service", srv.home / "x", srv.tmp / "titan2-x")]
    denied += ["/var/lib/titan2/db/titan2.db", "/etc/titan2/titan2.env", "/etc/systemd/system/ssh.service",
               bpath(r_) + "/incoming/../../var/db/titan2.db", bpath(r_) + "/releases/abc/", "releases/abc", ""]
    r = srv.run("release.sh", "assert-owned", *map(bpath, ok))
    assert r.returncode == 0, last_lines(r)
    r = srv.run("release.sh", "assert-owned", *map(bpath, ok), *denied)
    assert r.returncode == 2, last_lines(r)
    verdicts = [ln.split("] ", 1)[1] for ln in r.stdout.splitlines() if ": " in ln and ln.startswith("[titan2 release]")]
    assert verdicts == [f"owned: {bpath(p)}" for p in ok] + [f"REFUSED: {p}" for p in denied]

    # 2. Statically: every write in release.sh is inside an owned_* helper, and each helper asserts ownership
    assert _writes_outside_owned_helpers(OPS / "release.sh") == []
    text = (OPS / "release.sh").read_text(encoding="utf-8")
    mutant = srv.tmp / "mutant.sh"                       # the scan must catch raw writes added outside the helpers
    mutant.write_text(text.replace("cmd_preflight() {\n", 'cmd_preflight() {\n    rm -rf "$ROOT/x"\n'
                                   '    echo x > "$STATE_DIR/y"\n    tar -xzf a -C b\n', 1), encoding="utf-8")
    assert [h.split(": ", 1)[1] for h in _writes_outside_owned_helpers(mutant)] == ["['rm']", "['>\"']", "['tar -x']"]
    for m in re.finditer(r"(?ms)^(owned_[a-z_]+)\(\)\s*\{(.*?)^\}", text):
        assert "assert_owned" in m.group(2) or m.group(1) == "owned_unlock", m.group(1)
    for m in re.finditer(r"(?m)^(owned_[a-z_]+)\(\) \{ (.*) \}$", text):         # one-line helpers
        assert m.group(2).startswith("assert_owned "), m.group(1)

    # 3. A real deploy (new lock -> new venv, a changed unit, a live DB to back up) changes only owned paths
    db = srv.var / "db" / "titan2.db"
    db.write_bytes(b"SQLite format 3\x00 (fake)")
    srv.status(DB_EXISTS=1)
    srv.unit("titan2-dash", enabled="masked")              # the operator stopped both on purpose
    srv.unit("titan2-bot", enabled="masked")
    files = release_files(REL_B, lock="# fake lock B\n")
    files["ops/systemd/titan2-bot.service"] = files["ops/systemd/titan2-bot.service"] + b"# changed\n"
    sha = build_tarball(srv.root / "incoming", REL_B, files)[1]
    before = snapshot(srv.tmp, skip=(srv.state,))
    r = srv.run("release.sh", "install", REL_B, sha)
    assert r.returncode == 0, last_lines(r)
    after = snapshot(srv.tmp, skip=(srv.state,))
    owned = [str(r_ / d) for d in ("incoming", "releases", "venvs")]
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    deploy_backup = str(srv.var / "backups" / f"deploy-{REL_B}-")

    def allowed(p: str) -> bool:
        if any(p == o or p.startswith(o + os.sep) for o in owned) or p in (str(r_ / "current"), str(r_ / "current.new")):
            return True
        if os.path.dirname(p) == str(srv.units) and os.path.basename(p).startswith("titan2-"):
            return True
        return p.startswith(deploy_backup)          # written by `titan2 backup` (the CLI, as titan), not by deploy

    assert [p for p in changed if not allowed(p)] == []
    assert srv.current() == REL_B
    assert any(p.startswith(str(r_ / "venvs") + os.sep) for p in changed)            # the new venv
    assert str(srv.units / "titan2-bot.service") in changed                             # the changed unit
    assert srv.readlink(srv.units / "titan2-dash.service") == "/dev/null"              # a masked unit stays masked
    assert "titan2-dash.service is a symlink" in r.stderr
    assert "titan2-dash is masked: not restarted" in r.stderr and "titan2-bot is masked: not started" in r.stderr
    assert not [c for c in srv.calls() if "systemctl restart" in c or "systemctl start" in c]
    log = (srv.state / "selfcheck.log").read_text().splitlines()[-1]
    cwd, args = log.split("|")
    assert cwd.endswith(f"/releases/{REL_B}")
    assert re.search(rf"selfcheck --phase deploy --db-copy \S+/backups/deploy-{REL_B}-\d{{8}}T\d{{6}}Z/titan2\.db$", args)
    assert unallowed_sudo_calls(srv) == []


def test_first_install_sudo_calls_are_allow_listed(template_a):
    """The first install (every unit installed, daemon-reload, both restarts, the deploy self-check as titan) needs
    no sudo command beyond ops/sudoers.titan2."""
    calls = [c for c in template_a.calls() if c.startswith("sudo ")]
    assert any("install -m 0644" in c for c in calls) and any("-u titan" in c for c in calls)
    assert any(c.endswith("systemctl daemon-reload") for c in calls)
    assert unallowed_sudo_calls(template_a) == []


# ---------------------------------------------------------------------------------------------------------------
# Market-hours rule
# ---------------------------------------------------------------------------------------------------------------
MH_CASES = [
    # (IST now, titan2-bot state, status (None: the CLI cannot tell), flags, exit code, text)
    (MONDAY_10, "inactive", {}, [], 0, "titan2-bot is inactive: ok"),
    (MONDAY_10, "failed", {"OPEN_LEGS": 3}, [], 0, "titan2-bot is failed: ok"),
    (MONDAY_10, "active", {}, [], 2, "pass --allow-market-hours"),
    (MONDAY_10, "active", {}, ["--allow-market-hours"], 0, "is flat"),
    (MONDAY_10, "active", {"OPEN_LEGS": 1}, ["--allow-market-hours"], 2, "1 open leg(s)"),
    (MONDAY_10, "active", {"NONTERMINAL": 2}, ["--allow-market-hours"], 2, "2 non-terminal order(s)"),
    (MONDAY_10, "active", None, ["--allow-market-hours"], 2, "cannot prove it is flat"),
    (MONDAY_10, "active", {"DB_OK": 0}, ["--allow-market-hours"], 2, "cannot prove it is flat"),
    ("2026-10-12 0850", "activating", {}, [], 2, "pass --allow-market-hours"),     # recovering counts as running
    ("2026-10-12 0839", "active", {}, [], 0, "outside 0840-1545"),
    ("2026-10-12 0840", "active", {}, [], 2, "pass --allow-market-hours"),
    ("2026-10-12 1544", "active", {}, [], 2, "pass --allow-market-hours"),
    ("2026-10-12 1545", "active", {}, [], 0, "outside 0840-1545"),
    ("2026-10-12 1630", "active", {"OPEN_LEGS": 1}, ["--allow-market-hours"], 2, "1 open leg(s)"),
    (SUNDAY, "active", {}, [], 2, "pass --allow-market-hours"),     # a running bot on any day: same rule
]


def test_market_hours_guard(server):
    srv = server                     # no release yet: preflight is the market-hours rule and the disk check
    for now, state, status, flags, code, text in MH_CASES:
        srv.unit("titan2-bot", state=state)
        if status is None:
            srv.no_status()
        else:
            srv.status(**status)
        r = srv.run("release.sh", "preflight", *flags, env={"TITAN2_OPS_NOW": now})
        case = (now, state, status, flags)
        assert r.returncode == code, (case, last_lines(r))
        assert text in r.stdout + r.stderr, (case, last_lines(r))
    # an install refused by the rule changes nothing
    srv.unit("titan2-bot", state="active")
    srv.status(OPEN_LEGS=1)
    sha = srv.upload(REL_A)
    before = snapshot(srv.root, srv.units)
    r = srv.run("release.sh", "install", REL_A, sha, "--allow-market-hours", "--restart", env={"TITAN2_OPS_NOW": MONDAY_10})
    assert r.returncode == 2 and "1 open leg(s)" in r.stderr, last_lines(r)
    assert snapshot(srv.root, srv.units) == before
    # the fake clock can never reach a real server: without a test root it is refused, not obeyed
    r = srv.run("release.sh", "preflight", env={"TITAN2_ROOT": "", "TITAN2_OPS_NOW": "2026-10-12 2300"})
    assert r.returncode == 2 and "TITAN2_OPS_NOW is a test clock" in r.stderr, last_lines(r)
    # low disk is refused too
    srv.unit("titan2-bot", state="inactive")
    srv.set("df_avail_kb", "900000")
    r = srv.run("release.sh", "preflight")
    assert r.returncode == 2 and "need at least 1 GB" in r.stderr, last_lines(r)


# ---------------------------------------------------------------------------------------------------------------
# Atomic switch, automatic rollback, manual rollback
# ---------------------------------------------------------------------------------------------------------------
def test_atomic_switch_rollback(installed):
    srv = installed                                   # current -> A, titan2-bot inactive
    assert srv.current() == REL_A

    # B: the bot fails to start after the switch -> current flips back to A, CRITICAL alert, exit 1
    srv.on_restart("titan2-bot", BOT_START_FAILS)
    r = srv.install(REL_B, "--restart")
    assert r.returncode == 1, last_lines(r)
    assert srv.current() == REL_A
    assert srv.history()[-2:] == [["deploy", REL_A, REL_B], ["autorollback", REL_B, REL_A]]
    assert (srv.root / "releases" / REL_B).is_dir()              # kept for inspection
    assert srv.readlink(srv.root / "current.new") is None and not (srv.root / "current.new").exists()
    alerts = srv.alerts()
    assert len(alerts) == 1 and f"deploy {REL_B} FAILED" in alerts[0] and f"rolled back to {REL_A}" in alerts[0]
    calls = srv.calls()
    assert calls.count("sudo -n systemctl restart titan2-dash") == 2   # after the switch, and after the rollback
    assert calls.count("sudo -n systemctl restart titan2-bot") == 2
    assert unallowed_sudo_calls(srv) == []

    # C: READY arrives but the core heartbeat never gets fresh -> rolled back too
    srv.on_restart("titan2-bot", None)
    srv.status(TS_SHIFT=-60)
    r = srv.install(REL_C, "--restart")
    assert r.returncode == 1, last_lines(r)
    assert "no heartbeat under 10s" in r.stderr
    assert srv.current() == REL_A and len(srv.alerts()) == 2

    # C again with a healthy bot, in the evening (the bot is running now, so the market-hours window matters):
    # the prepared release is reused and the switch sticks
    evening = {"TITAN2_OPS_NOW": "2026-10-11 1800"}
    assert srv.unit_state("titan2-bot") == "active"
    srv.status()
    srv.clear_calls()
    r = srv.install(REL_C, "--restart", env=evening)
    assert r.returncode == 0, last_lines(r)
    assert "identical MANIFEST: reusing it" in r.stdout and "READY with a fresh heartbeat" in r.stdout
    assert srv.current() == REL_C
    assert srv.history()[-1] == ["deploy", REL_A, REL_C]
    assert not any(c.startswith("python3 -m venv") or "compileall" in c for c in srv.calls())   # nothing rebuilt

    # The switch is a rename of a new link over the old one
    text = (OPS / "release.sh").read_text(encoding="utf-8")
    assert 'ln -sfn -- "$1" "$2.new"' in text and 'mv -T -- "$2.new" "$2"' in text

    # rollback.sh (through ssh) flips to the release current replaced: A
    srv.clear_calls()
    r = srv.run("rollback.sh", HOST, env=evening)
    assert r.returncode == 0, last_lines(r)
    assert srv.current() == REL_A
    assert srv.history()[-1] == ["rollback", REL_C, REL_A]
    assert any(c.startswith("ssh testhost bash -s -- rollback") for c in srv.calls())
    assert unallowed_sudo_calls(srv) == []


def test_first_deploy_failure_has_nothing_to_roll_back_to(server):
    srv = server
    srv.on_restart("titan2-bot", BOT_START_FAILS)
    r = srv.install(REL_A)
    assert r.returncode == 1, last_lines(r)
    assert srv.current() == REL_A
    assert srv.history() == [["deploy", "-", REL_A]]
    assert "no previous release" in srv.alerts()[0]


def test_install_refusals_change_nothing(installed):
    srv = installed
    sha_b = srv.upload(REL_B)
    r = srv.run("release.sh", "install", REL_B, "0" * 64)
    assert r.returncode == 2 and "corrupt upload" in r.stderr, last_lines(r)

    srv.upload(REL_A, extra={"titan2/core/mod.py": b"CHANGED = 1\n"})
    sha_a2 = (srv.root / "incoming" / f"release-{REL_A}.tgz")
    r = srv.run("release.sh", "install", REL_A, sha256_file(sha_a2))
    assert r.returncode == 2 and "different MANIFEST" in r.stderr, last_lines(r)

    (srv.root / "incoming" / ".lock").mkdir()
    r = srv.run("release.sh", "install", REL_B, sha_b)
    assert r.returncode == 2 and "another deploy or rollback" in r.stderr, last_lines(r)
    (srv.root / "incoming" / ".lock").rmdir()

    srv.set("selfcheck_rc", "2")
    srv.clear_calls()
    r = srv.run("release.sh", "install", REL_B, sha_b)
    assert r.returncode == 2 and "selfcheck --phase deploy failed" in r.stderr, last_lines(r)
    assert srv.current() == REL_A and srv.history() == [["deploy", "-", REL_A]]
    assert not [c for c in srv.calls() if "restart" in c or "install -m" in c]
    assert not (srv.root / "incoming" / ".lock").exists()

    srv.set("selfcheck_rc", "0")
    sha_b = srv.upload(REL_B)
    r = srv.run("release.sh", "install", REL_B, sha_b)
    assert r.returncode == 0 and "identical MANIFEST: reusing it" in r.stdout, last_lines(r)
    assert srv.current() == REL_B


def test_prune_keeps_current_previous_and_newest(installed):
    srv = installed
    rel = bpath(srv.root / "releases")
    venvs = bpath(srv.root / "venvs")
    script = [f'cd "{rel}"']
    for i in range(1, 7):
        name = f"00000000000{i}"
        script += [f'mkdir -p {name} "{venvs}/v{i}"', f"ln -s ../../venvs/v{i} {name}/venv",
                   f'touch -d "2026-01-0{i} 10:00" {name}']
    script += [f'touch -d "2025-12-01 10:00" {REL_A}', f'mkdir -p "{venvs}/orphan" zzz.tmp',
               f'echo x > "{bpath(srv.root / "incoming")}/release-dead.tgz"']
    r = srv.sh(" && ".join(script))
    assert r.returncode == 0, r.stderr
    r = srv.install(REL_B)
    assert r.returncode == 0, last_lines(r)
    kept = sorted(p.name for p in (srv.root / "releases").iterdir() if p.is_dir())
    # current B, previous A (old, but never pruned), and the five newest: B and old 6..3
    assert kept == sorted([REL_A, REL_B, "000000000003", "000000000004", "000000000005", "000000000006"])
    lock_venv = srv.readlink(srv.root / "releases" / REL_A / "venv").rsplit("/", 1)[-1]
    assert sorted(p.name for p in (srv.root / "venvs").iterdir()) == sorted([lock_venv, "v3", "v4", "v5", "v6"])
    assert sorted(p.name for p in (srv.root / "incoming").iterdir()) == []


# ---------------------------------------------------------------------------------------------------------------
# deploy.sh end to end, from a git checkout
# ---------------------------------------------------------------------------------------------------------------
def _git(repo: Path, *args: str, env: dict) -> str:
    r = subprocess.run(["git", "-c", "user.name=ops-test", "-c", "user.email=ops-test@example.invalid",
                        "-c", "core.autocrlf=false", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_deploy_end_to_end(server):
    srv = server
    repo = srv.tmp / "repo"
    app = repo / "agentic_trader"
    shutil.copytree(OPS, app / "ops")
    (app / "titan2" / "core").mkdir(parents=True)
    (app / "titan2" / "__init__.py").write_text("__version__ = 'e2e'\n")
    (app / "titan2" / "core" / "__init__.py").write_text("")
    (app / "titan2" / "core" / "mod.py").write_text("X = 1\n")
    (app / "requirements.lock").write_text("# fake lock\n")
    (app / "tests").mkdir()
    (app / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    (srv.tmp / "gitconfig").write_text("")
    genv = dict(os.environ, GIT_CONFIG_GLOBAL=str(srv.tmp / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    _git(repo, "init", "-q", env=genv)
    _git(repo, "add", "-A", env=genv)
    _git(repo, "commit", "-q", "-m", "release", env=genv)
    rel = _git(repo, "rev-parse", "--short=12", "HEAD", env=genv)
    srv.on_restart("titan2-bot", BOT_READY_THEN_EXIT0)              # a Sunday deploy: the bot has nothing to do
    deploy = app / "ops" / "deploy.sh"
    env = {"GIT_CONFIG_GLOBAL": str(srv.tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}

    # an uncommitted change is refused before anything runs
    (app / "titan2" / "core" / "mod.py").write_text("X = 2\n")
    r = srv.run(deploy, HOST, env=env)
    assert r.returncode == 2 and "commit or remove" in r.stderr, last_lines(r)
    (app / "titan2" / "core" / "mod.py").write_text("X = 1\n")

    # the preflight refuses (bot running during market hours) before the upload
    srv.unit("titan2-bot", state="active")
    r = srv.run(deploy, HOST, env=dict(env, TITAN2_OPS_NOW=MONDAY_10))
    assert r.returncode == 2 and "pass --allow-market-hours" in r.stderr, last_lines(r)
    assert list((srv.root / "incoming").iterdir()) == [] and list((srv.root / "releases").iterdir()) == []
    srv.unit("titan2-bot", state="inactive")

    # a clean deploy
    r = srv.run(deploy, HOST, env=env)
    assert r.returncode == 0, last_lines(r)
    assert "1 passed" in r.stdout                                   # the tests ran first
    assert srv.current() == rel
    out = srv.root / "releases" / rel
    assert (out / "VERSION").read_text().splitlines()[0] == f"rel={rel}"
    manifest = {line.split("  ", 1)[1] for line in (out / "MANIFEST.sha256").read_text().splitlines()}
    assert {"titan2/core/mod.py", "ops/release.sh", "ops/systemd/titan2-bot.service", "requirements.lock",
            "VERSION"} <= manifest
    assert not [p for p in manifest if p.startswith("tests/") or p.endswith(".gitattributes")]
    assert list((srv.root / "incoming").iterdir()) == []

    # the same commit again: identical MANIFEST, reused, no switch
    r = srv.run(deploy, HOST, env=env)
    assert r.returncode == 0, last_lines(r)
    assert "identical MANIFEST: reusing it" in r.stdout and "already current: no switch" in r.stdout
    assert srv.history() == [["deploy", "-", rel]]


# ---------------------------------------------------------------------------------------------------------------
# Stage 0
# ---------------------------------------------------------------------------------------------------------------
def test_stage0_dry_run(server):
    srv = server
    v1 = srv.root / "agentic_trader"
    (v1 / "KEEP").unlink()
    (v1 / ".env").write_text("ZERODHA_API_SECRET=leaked\n")
    (v1 / ".kite_access_token").write_text("tok\n")
    (v1 / "autonomous_trader.py").write_text("# v1 bot\n")
    con = sqlite3.connect(v1 / "titan_state.db")
    con.execute("CREATE TABLE active_trades(x)")
    con.execute("INSERT INTO active_trades VALUES (1)")
    con.commit()
    con.close()
    (srv.units / "titan-dashboard.service").write_text("[Service]\nExecStart=/bin/false\n")
    nginx = srv.tmp / "nginx"
    for d in ("sites-available", "sites-enabled", "conf.d"):
        (nginx / d).mkdir(parents=True)
    (nginx / "sites-available" / "titan").write_text("server { listen 80; }\n")
    srv.make_link("../sites-available/titan", nginx / "sites-enabled" / "titan")
    srv.set("crontab", "30 8 * * 1-5 /home/ubuntu/titan/agentic_trader/cron_kite_token.sh\n")
    srv.set("unit_files", "titan-bot.service enabled enabled\ntitan-dashboard.service enabled enabled\n"
                          "titan2-bot.service enabled enabled\n")
    for u in ("titan-bot", "titan-dashboard"):
        srv.unit(u, state="active", enabled="enabled")
    env = {"TITAN2_NGINX_DIR": bpath(nginx)}
    laptop = srv.tmp / "laptop"
    laptop.mkdir()

    watched = (srv.root, srv.units, nginx, srv.home, laptop, srv.state / "units")
    before = snapshot(*watched)
    srv.clear_calls()
    r = srv.run("stage0_cutover.sh", HOST, env=env, cwd=laptop)            # the default: a dry run
    assert r.returncode == 0, last_lines(r)
    for expected in ("DRY-RUN: sudo systemctl disable --now titan-bot.service",
                     "DRY-RUN: sudo systemctl mask titan-dashboard.service", "DRY-RUN: crontab -r",
                     f"DRY-RUN: mv -T {bpath(v1)}", "v1_frozen_20261011", "DRY-RUN: sudo systemctl disable --now nginx",
                     "DRY RUN: nothing was changed", "MANUAL steps"):
        assert expected in r.stdout, expected
    assert "titan2-bot" not in "\n".join(ln for ln in r.stdout.splitlines() if ln.startswith("DRY-RUN"))
    assert snapshot(*watched) == before
    mutating = [c for c in srv.calls() if re.match(r"(systemctl (disable|enable|mask|stop|start|restart|reload|"
                                                   r"daemon-reload)|crontab -r|sudo (?!-n crontab -l))", c)]
    assert mutating == []

    # --execute inside market hours on a weekday is refused
    r = srv.run("stage0_cutover.sh", HOST, "--execute", env=dict(env, TITAN2_OPS_NOW=MONDAY_10), cwd=laptop)
    assert r.returncode == 2 and "outside market hours" in r.stderr, last_lines(r)
    assert snapshot(*watched) == before

    # --execute on Sunday
    r = srv.run("stage0_cutover.sh", HOST, "--execute", env=env, cwd=laptop)
    assert r.returncode == 0, last_lines(r)
    frozen = srv.root / "v1_frozen_20261011"
    assert not v1.exists() and (frozen / "autonomous_trader.py").is_file()
    for u in ("titan-bot", "titan-dashboard"):
        assert (srv.state / "units" / f"{u}.enabled").read_text().strip() == "masked"
        assert srv.unit_state(u) == "inactive"
        assert not (srv.units / f"{u}.service").exists()
        assert (srv.home / "v1_units_backup" / f"{u}.service").is_file()
    calls = srv.calls()
    assert calls.index("systemctl disable --now titan-bot.service") < calls.index("systemctl daemon-reload") \
        < calls.index("systemctl mask titan-bot.service")
    assert not (srv.state / "crontab").exists() and "cron_kite_token" in (srv.home / "v1-crontab.bak").read_text()
    assert not (nginx / "sites-available" / "titan").exists() and srv.readlink(nginx / "sites-enabled" / "titan") is None
    assert (srv.home / "v1_nginx_backup").is_dir() and "systemctl disable --now nginx" in calls
    copy = sqlite3.connect(srv.home / "titan-v1-state.db")
    assert copy.execute("SELECT x FROM active_trades").fetchall() == [(1,)]
    copy.close()
    with tarfile.open(srv.home / "v1_frozen_20261011.tgz") as t:
        names = {n.rsplit("/", 1)[-1] for n in t.getnames()}
    assert "autonomous_trader.py" in names
    assert not names & {".env", ".kite_access_token", "titan_state.db"}
    if os.name != "nt":
        assert (frozen / ".env").stat().st_mode & 0o077 == 0
        assert not os.access(frozen / "autonomous_trader.py", os.W_OK) or os.geteuid() == 0
    archive = laptop / "v1_archive" / "testhost-20261011"
    assert sorted(p.name for p in archive.iterdir()) == ["titan-v1-state.db", "v1-crontab.bak", "v1_frozen_20261011.tgz"]


# ---------------------------------------------------------------------------------------------------------------
# Backups (ops/backup.sh)
# ---------------------------------------------------------------------------------------------------------------
def test_backup_retention_and_s3(server):
    srv = server
    broot = srv.tmp / "backups"
    broot.mkdir()
    today = date.fromisoformat(srv.sh("date +%F").stdout.strip())
    names = [(today - timedelta(days=k)).isoformat() for k in range(1, 45)]
    names.append((today - timedelta(days=3)).isoformat() + "-120000")              # a second run that day
    for k in range(2, 17):                                                         # late-month sets, older months
        y, m = today.year, today.month - k
        while m <= 0:
            y, m = y - 1, m + 12
        names.append(date(y, m, 28).isoformat())
    names = sorted(set(names))
    for n in names:
        (broot / n).mkdir()
    deploys = srv.var / "backups"
    old = []
    for i, age in enumerate((40, 41, 42, 43)):
        d = deploys / f"deploy-old{i}"
        d.mkdir()
        old.append((d, age))
    (deploys / "deploy-new").mkdir()
    r = srv.sh(" && ".join(f'touch -d "{age} days ago" "{bpath(d)}"' for d, age in old))
    assert r.returncode == 0, r.stderr
    fakepy = srv.bin / "fakepy"
    fakepy.write_text('#!/usr/bin/env bash\necho "fakepy $*" >> "$FAKE_LOG"\n'
                      '[ "$1 $2 $3 $4" = "-m titan2 backup --to" ] && [ "$6" = --gzip ] || exit 9\n'
                      'mkdir -p "$5" && echo db | gzip > "$5/titan2.db.gz"\n', newline="\n")
    fakepy.chmod(0o755)                     # Linux runs only executable files; Windows never checks
    (srv.bin / "aws").write_text('#!/usr/bin/env bash\necho "aws $*" >> "$FAKE_LOG"\n', newline="\n")
    (srv.bin / "aws").chmod(0o755)
    (srv.etc / "titan2.env").write_text("ZERODHA_API_SECRET=not-for-backups\nBACKUP_S3_URI='s3://bucket/titan2/'\n")
    env = {"TITAN2_APP_DIR": bpath(srv.tmp), "TITAN2_PYTHON": bpath(fakepy), "TITAN2_BACKUP_DIR": bpath(broot),
           "TITAN2_ENV_FILE": bpath(srv.etc / "titan2.env")}
    r = srv.run("backup.sh", env=env)
    assert r.returncode == 0, last_lines(r)

    # the rule: the newest set of each of the 30 newest days and of each of the 12 newest months
    every = sorted(names + [today.isoformat()], reverse=True)
    keep, days, months = set(), [], []
    for n in every:
        if n[:10] not in days:
            days.append(n[:10])
            if len(days) <= 30:
                keep.add(n)
        if n[:7] not in months:
            months.append(n[:7])
            if len(months) <= 12:
                keep.add(n)
    assert sorted(p.name for p in broot.iterdir()) == sorted(keep)
    assert (broot / today.isoformat() / "titan2.db.gz").is_file()
    assert sorted(p.name for p in deploys.iterdir()) == ["KEEP", "deploy-new", "deploy-old0", "deploy-old1"]
    assert f"aws s3 cp --recursive --only-show-errors {bpath(broot)}/{today.isoformat()} s3://bucket/titan2/{today.isoformat()}/" \
        in srv.calls()
    assert "not-for-backups" not in "".join(srv.calls())


# ---------------------------------------------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------------------------------------------
def test_bootstrap_plans_then_applies(server):
    srv = server
    sysroot = srv.tmp / "sys"
    (sysroot / "home" / "ubuntu").mkdir(parents=True)
    srv.set("timezone", "UTC\n")
    env = {"TITAN2_SYSROOT": bpath(sysroot)}
    before = snapshot(sysroot)
    srv.clear_calls()
    r = srv.run("bootstrap.sh", env=env)
    assert r.returncode == 0, last_lines(r)
    for expected in ("PLAN: create the system user titan", "PLAN: create the system user titandash",
                     "PLAN: apt-get install python3-venv chrony", "PLAN: directory /var/lib/titan2: (missing) -> titan:titandash 710",
                     "PLAN: directory /var/lib/titan2/pub: (missing) -> titan:titandash 2750",
                     "PLAN: symlink /home/ubuntu/titan/agentic_trader/titan2 -> ../current/titan2",
                     "PLAN: install ops/sudoers.titan2 -> /etc/sudoers.d/titan2 (root:root 0440)",
                     "PLAN: timezone UTC -> Asia/Kolkata", "re-run with --execute"):
        assert expected in r.stdout, expected
    assert "+Cmnd_Alias TITAN2_CLI" in r.stdout                        # the diff of a new file
    assert snapshot(sysroot) == before
    assert not [c for c in srv.calls() if c.split()[:1] in (["useradd"], ["apt-get"], ["chown"], ["chronyc"])
                or c.startswith("timedatectl set-timezone")]

    r = srv.run("bootstrap.sh", "--execute", env=env)
    assert r.returncode == 0, last_lines(r)
    for d in ("var/lib/titan2/db", "var/lib/titan2/dash", "var/lib/titan2/pub", "var/lib/titan2/session",
              "var/log/titan2", "var/backups/titan2", "etc/titan2", "home/ubuntu/titan/releases",
              "home/ubuntu/titan/venvs", "home/ubuntu/titan/incoming"):
        assert (sysroot / d).is_dir(), d
    for src, dest in (("titan2-cli", "usr/local/bin/titan2"), ("sudoers.titan2", "etc/sudoers.d/titan2"),
                      ("logrotate.titan2", "etc/logrotate.d/titan2"),
                      ("journald-titan2.conf", "etc/systemd/journald.conf.d/titan2.conf"),
                      ("chrony-aws.sources", "etc/chrony/sources.d/titan2-aws.sources")):
        assert (sysroot / dest).read_bytes() == (OPS / src).read_bytes(), dest
    assert srv.readlink(sysroot / "home" / "ubuntu" / "titan" / "agentic_trader" / "titan2") == "../current/titan2"
    calls = srv.calls()
    for c in ("useradd --system --user-group --home-dir /var/lib/titan2 --no-create-home --shell /usr/sbin/nologin titan",
              "apt-get install -y python3-venv chrony", "timedatectl set-timezone Asia/Kolkata",
              "systemctl restart systemd-journald", "chronyc reload sources"):
        assert c in calls, c
    assert any(c.startswith("visudo -cf ") for c in calls)
    assert not (sysroot / "etc" / "titan2" / "titan2.env").exists()            # env files stay manual

    # v1's tree still in the WorkingDirectory: refused
    (sysroot / "home" / "ubuntu" / "titan" / "agentic_trader" / "autonomous_trader.py").write_text("# v1\n")
    r = srv.run("bootstrap.sh", env=env)
    assert r.returncode == 2 and "stage0_cutover.sh" in r.stderr, last_lines(r)
