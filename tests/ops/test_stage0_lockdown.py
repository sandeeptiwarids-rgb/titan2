"""Stage 0 leaves nothing of v1's readable by other users under /home/ubuntu (DETAIL §13.1: "/home/ubuntu chmod
0751 (user titan may traverse; nothing becomes readable)"; the dashboard user titandash holds no credentials).

Stage 1's bootstrap.sh opens /home/ubuntu to 0751 and /home/ubuntu/titan is 0755, so whatever v1 left group- or
world-readable there becomes readable by titan and titandash: the frozen tree's token files, v1's daily copies of .env
and dhan_config.json in ~/backups, its logs, ~/titan/zerodha_token.json, and deploy.yml's /tmp/.env.backup.

Git for Windows mounts NTFS with `noacl`: chmod and umask do not change what stat shows for group and others. So the
fake host records every chmod and the umask in effect (wrappers around the real chmod and tar), and the tests check
what was asked for; on Linux they check the real modes as well."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import subprocess
from pathlib import Path

from opsharness import HOST, OPS, bpath, needs_bash, snapshot

FROZEN = "v1_frozen_20261011"            # the Stage 0 date of the harness's Sunday clock


def last_lines(r: subprocess.CompletedProcess) -> str:
    return r.stdout[-3000:] + "\n--- stderr ---\n" + r.stderr[-3000:]


def record_chmod_and_umask(srv) -> None:
    """Wrap the real chmod (every call logged, tab-separated) and tar (the umask it runs under)."""
    real = {t: srv.sh(f"command -v {t}").stdout.strip() for t in ("chmod", "tar")}
    assert all(real.values()), real
    wrappers = {
        "chmod": ("{ printf chmod; printf '\\t%s' \"$@\"; printf '\\n'; } >> \"$FAKE_STATE/chmod.log\"\n"
                  f'exec "{real["chmod"]}" "$@"\n'),
        "tar": ('printf "umask %s: tar %s\\n" "$(umask)" "$*" >> "$FAKE_STATE/umask.log"\n'
                f'exec "{real["tar"]}" "$@"\n'),
    }
    for name, body in wrappers.items():
        p = srv.bin / name
        p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
        p.chmod(0o755)


def chmod_calls(srv) -> list[tuple[set[str], str, list[str]]]:
    """[(options, mode, paths)] of the recorded chmod calls."""
    p = srv.state / "chmod.log"
    out = []
    for line in (p.read_text(encoding="utf-8").splitlines() if p.exists() else []):
        args = line.split("\t")[1:]
        opts = set()
        while args and re.fullmatch(r"-[RcfvHLP]+", args[0]):
            opts |= set(args.pop(0)[1:])
        out.append((opts, args[0], args[1:]))
    return out


def owner_only(mode: str) -> bool:
    """True when the chmod MODE leaves group and others no r, w or x (octal, or symbolic clauses)."""
    if re.fullmatch(r"[0-7]{3,4}", mode):
        return int(mode, 8) & 0o077 == 0
    gone: dict[str, set[str]] = {"g": set(), "o": set()}
    for clause in mode.split(","):
        m = re.fullmatch(r"([ugoa]*)([-+=])([rwxXst]*)", clause)
        assert m, f"unexpected chmod mode {mode!r}"
        who = set(m.group(1).replace("a", "ugo")) or {"u", "g", "o"}
        perms = set(m.group(3)) & {"r", "w", "x"}
        for w in who & {"g", "o"}:
            if m.group(2) == "-":
                gone[w] |= perms
            elif m.group(2) == "=":
                gone[w] = {"r", "w", "x"} - perms
            else:
                gone[w] -= perms
    return gone["g"] == gone["o"] == {"r", "w", "x"}


def reached(path: Path, calls, *, tightening: bool) -> bool:
    """Some chmod call names `path` or, with -R, a directory above it (only owner-only modes when `tightening`)."""
    b = bpath(path)
    for opts, mode, paths in calls:
        if tightening and not owner_only(mode):
            continue
        for q in (q.rstrip("/") for q in paths):
            if b == q or ("R" in opts and b.startswith(q + "/")):
                return True
    return False


def group_or_other_access(p: Path) -> list[str]:
    """Entries at or under p (a directory recursively) that group or others can access."""
    entries = [p, *p.rglob("*")] if p.is_dir() and not p.is_symlink() else [p]
    return [str(q) for q in entries if not q.is_symlink() and stat.S_IMODE(q.stat().st_mode) & 0o077]


def v1_leftovers(srv) -> dict[str, Path]:
    """v1 as it is on the server before Stage 0: the tree, plus what v1 left elsewhere under /home/ubuntu."""
    v1 = srv.root / "agentic_trader"
    (v1 / "KEEP").unlink()
    for name, text in {".env": "ZERODHA_API_SECRET=leaked\n", ".env.bak": "ZERODHA_PASSWORD=leaked\n",
                       ".kite_access_token": "tok\n", "dhan_config.json": '{"token": "leaked"}\n',
                       "zerodha_token.json": '{"access_token": "leaked"}\n', "ec2.pem": "-----BEGIN KEY-----\n",
                       "autonomous_trader.py": "# v1 bot\n"}.items():
        (v1 / name).write_text(text)
    (v1 / "logs").mkdir()
    (v1 / "logs" / "kite_token.log").write_text("Renew response missing accessToken: {...}\n")
    con = sqlite3.connect(v1 / "titan_state.db")
    con.execute("CREATE TABLE active_trades(x)")
    con.commit()
    con.close()
    (srv.root / "logs").mkdir()                                     # systemd's append: logs (root-owned on EC2)
    (srv.root / "logs" / "titan.log").write_text("Traceback ... api_secret=leaked\n")
    (srv.root / "zerodha_token.json").write_text('{"access_token": "leaked"}\n')   # daily_maintenance.sh line 82
    cacert = srv.root / "venv" / "lib" / "certifi" / "cacert.pem"   # v1's venv stays untouched (DETAIL §13.1)
    cacert.parent.mkdir(parents=True)
    cacert.write_text("public CA bundle\n")
    day = srv.home / "backups" / "2026-10-09"                       # daily_maintenance.sh lines 37-39
    day.mkdir(parents=True)
    (day / ".env").write_text("ZERODHA_API_SECRET=leaked\n")
    (day / "dhan_config.json").write_text('{"token": "leaked"}\n')
    (srv.home / "backups" / "pre-deploy-20261001_120000.tar.gz").write_bytes(b"deploy.yml: the whole tree, .env too")
    (srv.home / "deploy-key.pem").write_text("-----BEGIN KEY-----\n")
    (srv.home / "notes.txt").write_text("not a secret\n")
    for keep in (cacert, srv.home / "notes.txt"):                   # readable by all, whatever the runner's umask
        keep.chmod(0o644)
    systmp = srv.tmp / "systmp"                                     # deploy.yml: cp .env /tmp/.env.backup
    systmp.mkdir()
    (systmp / ".env.backup").write_text("ZERODHA_API_SECRET=leaked\n")
    (srv.tmp / "nginx").mkdir()                                     # no nginx site in this test
    return {"frozen": srv.root / FROZEN, "older_frozen": srv.root / "v1_frozen_20261009", "logs": srv.root / "logs",
            "backups": srv.home / "backups", "titan_token": srv.root / "zerodha_token.json",
            "home_key": srv.home / "deploy-key.pem", "tmp_copy": systmp / ".env.backup", "cacert": cacert,
            "notes": srv.home / "notes.txt", "systmp": systmp}


def stage0_env(srv) -> dict[str, str]:
    return {"TITAN2_NGINX_DIR": bpath(srv.tmp / "nginx"), "TITAN2_TMP_DIR": bpath(srv.tmp / "systmp")}


@needs_bash
def test_stage0_makes_every_v1_file_under_home_owner_only(server):
    srv = server
    p = v1_leftovers(srv)
    record_chmod_and_umask(srv)
    laptop = srv.tmp / "laptop"
    laptop.mkdir()
    env = stage0_env(srv)

    # the dry run names every lockdown and changes nothing
    before = snapshot(srv.root, srv.home, p["systmp"], laptop)
    r = srv.run("stage0_cutover.sh", HOST, env=env, cwd=laptop)
    assert r.returncode == 0, last_lines(r)
    assert f"DRY-RUN: chmod -R go-rwx,a-w {bpath(p['frozen'])}" in r.stdout, last_lines(r)
    for key in ("older_frozen", "logs", "backups", "titan_token", "home_key", "tmp_copy"):
        assert f"DRY-RUN: sudo chmod -R go-rwx {bpath(p[key])}" in r.stdout, (key, last_lines(r))
    assert chmod_calls(srv) == [] and snapshot(srv.root, srv.home, p["systmp"], laptop) == before

    r = srv.run("stage0_cutover.sh", HOST, "--execute", env=env, cwd=laptop)
    assert r.returncode == 0, last_lines(r)
    calls = chmod_calls(srv)
    frozen = p["frozen"]
    must = [frozen, frozen / ".env", frozen / ".env.bak", frozen / ".kite_access_token", frozen / "dhan_config.json",
            frozen / "zerodha_token.json", frozen / "ec2.pem", frozen / "logs" / "kite_token.log",
            frozen / "titan_state.db", frozen / "autonomous_trader.py", p["older_frozen"] / "autonomous_trader.py",
            p["logs"], p["logs"] / "titan.log", p["backups"], p["backups"] / "2026-10-09" / ".env",
            p["backups"] / "2026-10-09" / "dhan_config.json", p["backups"] / "pre-deploy-20261001_120000.tar.gz",
            p["titan_token"], p["home_key"], p["tmp_copy"]]
    assert [str(x) for x in must if not reached(x, calls, tightening=True)] == []
    # v2's directories, v1's venv and ordinary files keep their modes; nothing of /var/lib/titan2 or /etc/titan2
    for x in (srv.root / "incoming", srv.root / "releases", srv.root / "venvs", p["cacert"], p["notes"], srv.var,
              srv.etc, srv.home):
        assert not reached(x, calls, tightening=False), x
    # what Stage 0 writes itself (the DB copy, the tarball) is created owner-only
    umasks = (srv.state / "umask.log").read_text(encoding="utf-8").splitlines()
    assert umasks and all(u.startswith("umask 0077:") for u in umasks), umasks
    if os.name != "nt":                                 # NTFS (noacl) cannot show group/other bits
        for x in [frozen, p["older_frozen"], p["logs"], p["backups"], p["titan_token"], p["home_key"], p["tmp_copy"],
                  srv.home / "titan-v1-state.db", srv.home / f"{FROZEN}.tgz"]:
            assert group_or_other_access(x) == [], x
        assert stat.S_IMODE((srv.home / "v1_units_backup").stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(p["cacert"].stat().st_mode) & 0o044 and stat.S_IMODE(p["notes"].stat().st_mode) & 0o044
    assert sorted(x.name for x in (laptop / "v1_archive" / "testhost-20261011").iterdir()) == \
        ["titan-v1-state.db", f"{FROZEN}.tgz"]


@needs_bash
def test_stage0_lockdown_never_follows_a_symlink_with_sudo(server):
    srv = server
    p = v1_leftovers(srv)
    elsewhere = srv.tmp / "elsewhere"                   # e.g. a mistaken link to /var/log
    elsewhere.mkdir()
    (elsewhere / "syslog").write_text("other users read this\n")
    srv.sh(f'rm -rf "{bpath(p["logs"])}"')
    srv.make_link(bpath(elsewhere), p["logs"])
    record_chmod_and_umask(srv)
    r = srv.run("stage0_cutover.sh", "--local", "--execute", env=stage0_env(srv))
    assert r.returncode == 0, last_lines(r)
    assert f"MANUAL: {bpath(p['logs'])} is a symlink to {bpath(elsewhere)}" in r.stdout, last_lines(r)
    calls = chmod_calls(srv)
    assert not reached(elsewhere / "syslog", calls, tightening=False)
    assert reached(p["backups"] / "2026-10-09" / ".env", calls, tightening=True)


def test_lockdown_names_match_the_tarball_excludes():
    """The lockdown's secret names and the tarball's secret excludes are one list written twice: keep them equal."""
    text = (OPS / "stage0_cutover.sh").read_text(encoding="utf-8")
    excludes = re.findall(r"--exclude='([^']+)'", text)
    names = re.findall(r"-name '([^']+)'", re.search(r"(?ms)^SECRET_NAMES=\((.*?)\)$", text).group(1))
    assert names == excludes[:6] == [".env*", ".kite_access_token", "dhan_config.json", "zerodha_token.json",
                                     "*.pem", "*.key"]
