"""Helpers for the ops tests (DETAIL §16.7: "Ops tests run the deploy scripts against temp directories").

The scripts run under bash with every outside command faked on PATH:
- ssh runs the remote command locally (stdin passed through) and scp copies locally, so deploy.sh, rollback.sh,
  pull_drift.sh and stage0_cutover.sh drive ops/release.sh exactly as they would over the network;
- sudo drops straight to the command (every call is logged, and test_sudo_calls_are_allow_listed checks each one
  against ops/sudoers.titan2);
- systemctl is a small state machine under FAKE_STATE/units; titan2 answers `status --json`, `backup` and `alert`
  from FAKE_STATE files in the shape of titan2/__main__.py;
- python3 is the test interpreter, except that `python3 -m venv DIR` builds a fake venv whose python runs
  compileall for real and logs `-m titan2 selfcheck ...` (exit code from FAKE_STATE/selfcheck_rc);
- plus df, crontab, nginx, ss, id, useradd, dpkg, apt-get, visudo, chown, timedatectl and chronyc.
On Windows the scripts run in Git for Windows' bash with MSYS=winsymlinks:sys: `ln -s` then makes links that every
MSYS tool follows (native Windows symlinks need a privilege). Python's os functions do not see those links, so the
tests read links through bash (Server.readlink).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[2] / "ops"
APP = OPS.parent
REPO = APP.parent
HOST = "testhost"
SUNDAY = "2026-10-11 1000"          # outside market hours (Sunday)
MONDAY_10 = "2026-10-12 1000"       # a weekday inside the 08:40-15:45 window


def _find_bash() -> str | None:
    if os.name == "nt":
        for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(c).is_file():
                return c
        w = shutil.which("bash")
        return w if w and "system32" not in w.lower() else None      # System32\bash.exe is WSL, not Git bash
    return shutil.which("bash")


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash (Git for Windows on Windows)")


def bpath(p: os.PathLike | str) -> str:
    """A path as bash sees it (C:\\x\\y -> /c/x/y under MSYS)."""
    s = str(p)
    if os.name == "nt":
        s = s.replace("\\", "/")
        if len(s) > 1 and s[1] == ":":
            s = "/" + s[0].lower() + s[2:]
    return s


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# Fake commands
# ---------------------------------------------------------------------------------------------------------------
FAKES: dict[str, str] = {}

FAKES["ssh"] = r'''#!/usr/bin/env bash
# ssh [-o opt]... [-T] host command...: run the command here, in the fake remote user's home
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -T|-q|-n|-t) shift ;; *) break ;; esac; done
host=$1; shift
echo "ssh $host $*" >> "$FAKE_LOG"
cd "$HOME"
exec bash -c "$*"
'''

FAKES["scp"] = r'''#!/usr/bin/env bash
# scp [-q] [-o opt]... SRC DST, where either side may be "testhost:path"
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -q|-p|-r|-B) shift ;; *) break ;; esac; done
echo "scp $*" >> "$FAKE_LOG"
src=${1#testhost:}; dst=${2#testhost:}
cp -- "$src" "$dst"
'''

FAKES["sudo"] = r'''#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do case "$1" in -n|-H|-E|-k) shift ;; -u) shift 2 ;; --) shift; break ;; *) break ;; esac; done
exec "$@"
'''

# The fakes use bash builtins only where they can: every fork costs ~50 ms under MSYS.
FAKES["systemctl"] = r'''#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
U="$FAKE_STATE/units"
quiet=0; args=()
for a in "$@"; do
    case "$a" in
        --quiet|-q) quiet=1 ;;
        --no-legend|--plain|--value|--no-block|--no-pager) ;;
        *) args+=("$a") ;;
    esac
done
get() { local v=""; [ -f "$1" ] && read -r v < "$1"; printf '%s' "$v"; }
n=${args[1]:-}; n=${n%.service}
case "${args[0]:-}" in
    is-active)
        s=$(get "$U/$n.state"); s=${s:-inactive}
        [ "$quiet" = 1 ] || echo "$s"
        [ "$s" = active ] ;;
    is-enabled)
        if [ -f "$U/$n.enabled" ]; then get "$U/$n.enabled"; echo; exit 0; fi
        echo "Failed to get unit file state for ${args[1]}: No such file or directory" >&2; exit 1 ;;
    show)   # show -p PROP UNIT (with --value)
        n=${args[3]%.service}; get "$U/$n.${args[2]}"; echo ;;
    list-unit-files)
        [ -f "$FAKE_STATE/unit_files" ] && while IFS= read -r l; do echo "$l"; done < "$FAKE_STATE/unit_files"; exit 0 ;;
    restart|start)
        if [ -f "$FAKE_STATE/on_restart_$n" ]; then . "$FAKE_STATE/on_restart_$n"; exit $?; fi
        echo active > "$U/$n.state"; echo success > "$U/$n.Result" ;;
    stop)
        echo inactive > "$U/$n.state" ;;
    disable)
        for u in "${args[@]:1}"; do [ "$u" = --now ] && continue; u=${u%.service}
            echo disabled > "$U/$u.enabled"; echo inactive > "$U/$u.state"; done ;;
    mask)
        echo masked > "$U/$n.enabled" ;;
    *) exit 0 ;;
esac
'''

# `titan2 status --json` in the shape of titan2/__main__.py status_info, from FAKE_STATE/status.env (KEY=VALUE:
# OPEN_LEGS, NONTERMINAL, HB_AGE, TS_SHIFT, DB_OK, DB_EXISTS, NO_STATUS), with a fresh UTC timestamp.
FAKES["titan2"] = r'''#!/usr/bin/env bash
echo "titan2 $*" >> "$FAKE_LOG"
export TZ=UTC
case "${1:-}" in
    status)
        if [ -f "$FAKE_STATE/status.json" ]; then
            while IFS= read -r l; do echo "$l"; done < "$FAKE_STATE/status.json"; exit "${STATUS_RC:-0}"; fi
        if [ ! -f "$FAKE_STATE/status.env" ]; then echo '{"db": {"ok": false, "error": "fake: no status"}}'; exit 2; fi
        OPEN_LEGS=0 NONTERMINAL=0 HB_AGE=0.3 TS_SHIFT=0 DB_OK=1 DB_EXISTS=1 NO_STATUS=0
        . "$FAKE_STATE/status.env"
        printf -v ts '%(%Y-%m-%dT%H:%M:%S)T' "$(( EPOCHSECONDS + TS_SHIFT ))"
        status="{\"ts\": \"$ts.000+00:00\", \"phase\": \"TRADING\", \"heartbeat_age_s\": $HB_AGE}"
        [ "$NO_STATUS" = 1 ] && status=null
        ok=true; [ "$DB_OK" = 1 ] || ok=false
        ex=true; [ "$DB_EXISTS" = 1 ] || ex=false
        printf '{"status": %s, "db": {"ok": %s, "exists": %s, "open_legs": %s, "nonterminal_orders": %s, "error": null}}\n' \
            "$status" "$ok" "$ex" "$OPEN_LEGS" "$NONTERMINAL"
        [ "$DB_OK" = 1 ] || exit 2 ;;
    backup)   # backup --to DIR [--gzip]
        dest=$3; mkdir -p "$dest"
        if [ -f "$TITAN2_VAR/db/titan2.db" ]; then cp "$TITAN2_VAR/db/titan2.db" "$dest/titan2.db"; echo "titan2.db: ok -> $dest/titan2.db"; fi
        if [ "${4:-}" = --gzip ] && [ -f "$dest/titan2.db" ]; then gzip "$dest/titan2.db"; fi
        [ -f "$FAKE_STATE/backup_fails" ] && exit 2; exit 0 ;;
    alert)
        printf '%s\n' "$3" >> "$FAKE_STATE/alerts.log" ;;
    *) echo "fake titan2: unsupported: $*" >&2; exit 2 ;;
esac
'''

FAKES["python3"] = r'''#!/usr/bin/env bash
if [ "${1:-}" = -m ] && [ "${2:-}" = venv ]; then
    echo "python3 -m venv $3" >> "$FAKE_LOG"
    mkdir -p "$3/bin" && cp "$FAKE_BIN/venv-python" "$3/bin/python" && chmod +x "$3/bin/python"
    exit 0
fi
exec "$REAL_PYTHON" "$@"
'''

FAKES["venv-python"] = r'''#!/usr/bin/env bash
echo "venv-python $*" >> "$FAKE_LOG"
if [ "${1:-}" = -m ]; then
    case "${2:-}" in
        pip) exit 0 ;;
        compileall) shift 2; exec "$REAL_PYTHON" -m compileall "$@" ;;
        titan2) printf '%s|%s\n' "$PWD" "$*" >> "$FAKE_STATE/selfcheck.log"
                exit "$(cat "$FAKE_STATE/selfcheck_rc" 2>/dev/null || echo 0)" ;;
    esac
fi
exec "$REAL_PYTHON" "$@"
'''

FAKES["df"] = r'''#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 30000000 1000 %s 1%% /\n' \
    "$(cat "$FAKE_STATE/df_avail_kb" 2>/dev/null || echo 20000000)"
'''

FAKES["crontab"] = r'''#!/usr/bin/env bash
echo "crontab $*" >> "$FAKE_LOG"
case "${1:-}" in
    -l) if [ -s "$FAKE_STATE/crontab" ]; then cat "$FAKE_STATE/crontab"; else echo "no crontab for ubuntu" >&2; exit 1; fi ;;
    -r) rm -f "$FAKE_STATE/crontab" ;;
esac
'''

for _name in ("nginx", "ss", "useradd", "apt-get", "chown", "chronyc"):
    FAKES[_name] = f'#!/usr/bin/env bash\necho "{_name} $*" >> "$FAKE_LOG"\n'

FAKES["visudo"] = '#!/usr/bin/env bash\necho "visudo $*" >> "$FAKE_LOG"\nexit "$(cat "$FAKE_STATE/visudo_rc" 2>/dev/null || echo 0)"\n'
FAKES["dpkg"] = '#!/usr/bin/env bash\necho "dpkg $*" >> "$FAKE_LOG"\n[ -f "$FAKE_STATE/dpkg_$2" ]\n'
FAKES["timedatectl"] = ('#!/usr/bin/env bash\necho "timedatectl $*" >> "$FAKE_LOG"\n'
                        '[ "${1:-}" = show ] && cat "$FAKE_STATE/timezone" 2>/dev/null\nexit 0\n')
FAKES["id"] = r'''#!/usr/bin/env bash
case "${1:-}" in
    -u) if [ -n "${2:-}" ]; then grep -qx "$2" "$FAKE_STATE/users" 2>/dev/null || { echo "id: '$2': no such user" >&2; exit 1; }; echo 999; else echo 1000; fi ;;
    -nG) grep -qx "$2" "$FAKE_STATE/users" 2>/dev/null && echo "$2" ;;
    *) /usr/bin/id "$@" ;;
esac
'''


def write_fakes(fakebin: Path) -> None:
    fakebin.mkdir(parents=True, exist_ok=True)
    for name, text in FAKES.items():
        p = fakebin / name
        p.write_text(text, encoding="utf-8", newline="\n")
        p.chmod(0o755)
    # Sourced by every non-interactive bash (BASH_ENV), nested ones included: the fakes come first on PATH even
    # where a launcher (Git for Windows' bin\bash.exe) puts its own /usr/bin (real ssh, scp, df, chown) in front.
    (fakebin.parent / "bashenv").write_text('PATH="$FAKE_BIN:$PATH"\n', encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------------------------------------------
# sudoers
# ---------------------------------------------------------------------------------------------------------------
def sudo_rules() -> list[tuple[str, str, str]]:
    """ops/sudoers.titan2 -> [(runas, command path pattern, argument pattern)]."""
    text = (OPS / "sudoers.titan2").read_text(encoding="ascii")
    logical = re.sub(r"\\\n\s*", " ", text)
    aliases: dict[str, list[tuple[str, str]]] = {}
    rules = []
    for line in logical.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^Cmnd_Alias\s+([A-Z0-9_]+)\s*=\s*(.+)$", line)
        if m:
            aliases[m.group(1)] = [tuple(c.strip().split(" ", 1)) for c in m.group(2).split(",")]
            continue
        m = re.match(r"^ubuntu\s+ALL=\((\w+)\)\s+NOPASSWD:\s*(.+)$", line)
        assert m, f"unexpected sudoers line: {line}"
        for alias in (a.strip() for a in m.group(2).split(",")):
            rules += [(m.group(1), path, args) for path, args in aliases[alias]]
    return rules


def sudo_allowed(runas: str, argv: list[str], rules=None) -> bool:
    """sudoers matching: '*' in the command path stops at '/', '*' in the arguments matches anything."""
    cmd = argv[0] if argv[0].startswith("/") else "/usr/bin/" + argv[0]     # sudo's secure_path
    args = " ".join(argv[1:])
    for r, path, pattern in (rules or sudo_rules()):
        path_re = "^" + re.escape(path).replace(r"\*", "[^/]*") + "$"
        args_re = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
        if r == runas and re.match(path_re, cmd) and re.match(args_re, args):
            return True
    return False


def unallowed_sudo_calls(srv: "Server") -> list[str]:
    """The fake sudo log lines whose command, mapped to the production paths, ops/sudoers.titan2 does not allow."""
    r = srv.sh(f'realpath -m -- "{bpath(srv.root)}" "{bpath(srv.units)}"')
    real_root, real_units = r.stdout.splitlines()[:2]
    subs = [(real_root, "/home/ubuntu/titan"), (bpath(srv.root), "/home/ubuntu/titan"),
            (real_units, "/etc/systemd/system"), (bpath(srv.units), "/etc/systemd/system"),
            (bpath(srv.var), "/var/lib/titan2")]
    rules = sudo_rules()
    bad = []
    for line in srv.calls():
        if not line.startswith("sudo "):
            continue
        argv, runas = line.split()[1:], "root"
        while argv and argv[0].startswith("-"):
            if argv[0] == "-u":
                runas, argv = argv[1], argv[2:]
            else:
                argv = argv[1:]
        mapped = []
        for a in argv:
            for old, new in subs:
                if a.startswith(old):
                    a = new + a[len(old):]
                    break
            mapped.append(a)
        if not sudo_allowed(runas, mapped, rules):
            bad.append(line)
    return bad


# ---------------------------------------------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------------------------------------------
def release_files(rel: str, *, lock: str = "# fake lock A\n", extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "titan2/__init__.py": f"__version__ = {rel!r}\n".encode(),
        "titan2/core/__init__.py": b"",
        "titan2/core/mod.py": f"REL = {rel!r}\n".encode(),
        "requirements.lock": lock.encode(),
        "ops/release.sh": (OPS / "release.sh").read_bytes(),
    }
    for p in sorted((OPS / "systemd").iterdir()):
        files[f"ops/systemd/{p.name}"] = p.read_bytes()
    files.update(extra or {})
    lockhash = hashlib.sha256(files["requirements.lock"]).hexdigest()[:16]
    files["VERSION"] = f"rel={rel}\ncommit={rel}\ncommitted=2026-10-10T18:00:00+05:30\nlockhash={lockhash}\n".encode()
    return files


def build_tarball(dest: Path, rel: str, files: dict[str, bytes]) -> tuple[Path, str]:
    """release-REL.tgz as deploy.sh makes it: ./-relative members plus MANIFEST.sha256 ("<sha256>  <path>")."""
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in sorted(files.items()))
    members = dict(files, **{"MANIFEST.sha256": manifest.encode()})
    dest.mkdir(parents=True, exist_ok=True)
    tb = dest / f"release-{rel}.tgz"
    with tarfile.open(tb, "w:gz") as t:
        for path, data in sorted(members.items()):
            info = tarfile.TarInfo("./" + path)
            info.size, info.mtime = len(data), 1_791_000_000
            info.mode = 0o755 if path.endswith(".sh") else 0o644
            t.addfile(info, io.BytesIO(data))
    return tb, sha256_file(tb)


def snapshot(*roots: Path, skip: tuple[Path, ...] = ()) -> dict[str, str]:
    """path -> content hash, or 'L:<target>' for a symlink (an MSYS link reads as a small file on Windows)."""
    out: dict[str, str] = {}
    skip_s = {str(s) for s in skip}

    def walk(p: Path) -> None:
        if str(p) in skip_s:
            return
        if p.is_symlink():
            out[str(p)] = "L:" + os.readlink(p)
        elif p.is_dir():
            out[str(p)] = "D"
            for c in sorted(p.iterdir()):
                walk(c)
        elif p.exists():
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()

    for r in roots:
        walk(r)
    return out


# ---------------------------------------------------------------------------------------------------------------
# The fake server
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Server:
    tmp: Path
    bin: Path
    state: Path
    home: Path
    root: Path            # TITAN2_ROOT (/home/ubuntu/titan)
    units: Path           # TITAN2_UNIT_DIR (/etc/systemd/system)
    var: Path             # TITAN2_VAR (/var/lib/titan2)
    etc: Path             # TITAN2_ETC (/etc/titan2)
    env: dict[str, str] = field(default_factory=dict)

    # -- running scripts --------------------------------------------------------------------------------------------
    def run(self, script: str | Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None,
            stdin: str | None = None, timeout: float = 240) -> subprocess.CompletedProcess:
        path = script if isinstance(script, Path) else OPS / script
        e = dict(self.env, **(env or {}))
        return subprocess.run([BASH, bpath(path), *args], env=e, cwd=str(cwd or self.tmp), input=stdin,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def sh(self, command: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", command], env=dict(self.env, **(env or {})), cwd=str(self.tmp),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)

    def readlink(self, p: Path) -> str | None:
        r = self.sh(f'[ -L "{bpath(p)}" ] && readlink -- "{bpath(p)}"')
        return r.stdout.strip() if r.returncode == 0 else None

    def make_link(self, target: str, link: Path) -> None:
        r = self.sh(f'ln -sfn -- "{target}" "{bpath(link)}"')
        assert r.returncode == 0, r.stderr

    def current(self) -> str | None:
        t = self.readlink(self.root / "current")
        return t.rsplit("/", 1)[-1] if t else None

    def history(self) -> list[list[str]]:
        p = self.root / "releases" / "HISTORY"
        return [ln.split()[1:] for ln in p.read_text().splitlines()] if p.exists() else []

    def calls(self) -> list[str]:
        p = self.state / "calls.log"
        return p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []

    def alerts(self) -> list[str]:
        p = self.state / "alerts.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def clear_calls(self) -> None:
        (self.state / "calls.log").write_text("")

    # -- fake host state -------------------------------------------------------------------------------------------
    def unit(self, name: str, state: str | None = None, result: str | None = None, enabled: str | None = None) -> None:
        u = self.state / "units"
        u.mkdir(exist_ok=True)
        if state is not None:
            (u / f"{name}.state").write_text(state + "\n")
        if result is not None:
            (u / f"{name}.Result").write_text(result + "\n")
        if enabled is not None:
            (u / f"{name}.enabled").write_text(enabled + "\n")

    def unit_state(self, name: str) -> str:
        p = self.state / "units" / f"{name}.state"
        return p.read_text().strip() if p.exists() else "inactive"

    def on_restart(self, name: str, script: str | None) -> None:
        p = self.state / f"on_restart_{name}"
        if script is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(script, encoding="utf-8", newline="\n")

    def status(self, **kv: object) -> None:
        """`titan2 status --json`: OPEN_LEGS, NONTERMINAL, HB_AGE, TS_SHIFT, DB_OK, DB_EXISTS, NO_STATUS."""
        (self.state / "status.json").unlink(missing_ok=True)
        (self.state / "status.env").write_text("".join(f"{k}={v}\n" for k, v in kv.items()))

    def no_status(self) -> None:
        (self.state / "status.env").unlink(missing_ok=True)
        (self.state / "status.json").unlink(missing_ok=True)

    def set(self, name: str, value: str) -> None:
        (self.state / name).write_text(value)

    # -- releases --------------------------------------------------------------------------------------------------
    def upload(self, rel: str, **kw) -> str:
        """Put release-REL.tgz into incoming/ (as deploy.sh's scp would); return its sha256."""
        return build_tarball(self.root / "incoming", rel, release_files(rel, **kw))[1]

    def install(self, rel: str, *flags: str, env: dict[str, str] | None = None, **kw) -> subprocess.CompletedProcess:
        sha = self.upload(rel, **kw)
        return self.run("release.sh", "install", rel, sha, *flags, env=env)

    def unfreeze(self) -> None:
        self.sh(f'chmod -R u+w "{bpath(self.tmp)}" 2>/dev/null; true')


REL_A, REL_B, REL_C = "a1a1a1a1a1a1", "b2b2b2b2b2b2", "c3c3c3c3c3c3"

# on_restart hooks for the fake systemctl (run by `systemctl restart titan2-bot`)
BOT_READY_THEN_EXIT0 = ('echo inactive > "$FAKE_STATE/units/titan2-bot.state"\n'
                        'echo success > "$FAKE_STATE/units/titan2-bot.Result"\n')
BOT_START_FAILS = ('echo failed > "$FAKE_STATE/units/titan2-bot.state"\n'
                   'echo exit-code > "$FAKE_STATE/units/titan2-bot.Result"\nexit 1\n')


def _server_at(tmp: Path) -> Server:
    return Server(tmp=tmp, bin=tmp / "bin", state=tmp / "state", home=tmp / "home", root=tmp / "root",
                  units=tmp / "units", var=tmp / "var", etc=tmp / "etc")


def _env_for(srv: Server) -> dict[str, str]:
    path_sep = ";" if os.name == "nt" else ":"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TITAN2_", "FAKE_"))}
    env.update({
        "PATH": str(srv.bin) + path_sep + os.environ.get("PATH", ""),
        "BASH_ENV": bpath(srv.tmp / "bashenv"),
        "MSYS": "winsymlinks:sys",
        "HOME": bpath(srv.home),
        "TMPDIR": bpath(srv.tmp / "tmpdir"),
        "FAKE_STATE": bpath(srv.state),
        "FAKE_LOG": bpath(srv.state / "calls.log"),
        "FAKE_BIN": bpath(srv.bin),
        "REAL_PYTHON": bpath(sys.executable),
        "PYTHON": bpath(sys.executable),
        "TITAN2_ROOT": bpath(srv.root),
        "TITAN2_UNIT_DIR": bpath(srv.units),
        "TITAN2_VAR": bpath(srv.var),
        "TITAN2_ETC": bpath(srv.etc),
        "TITAN2_OPS_NOW": SUNDAY,
        "TITAN2_READY_TIMEOUT_S": "2",
        "TITAN2_POLL_S": "0.1",
        "TITAN2_DASH_SETTLE_S": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def make_server(tmp: Path) -> Server:
    """An empty fake server: bootstrap's directories, no release yet, the bot stopped, a flat fresh status."""
    srv = _server_at(tmp)
    write_fakes(srv.bin)
    for d in (srv.state / "units", srv.home, srv.root / "incoming", srv.root / "releases", srv.root / "venvs",
              srv.root / "agentic_trader", srv.root / "v1_frozen_20261009", srv.units, srv.var / "db",
              srv.var / "backups", srv.etc, tmp / "tmpdir"):
        d.mkdir(parents=True, exist_ok=True)
    # things a deploy must never touch
    (srv.root / "agentic_trader" / "KEEP").write_text("the WorkingDirectory holds only the titan2 link\n")
    (srv.root / "v1_frozen_20261009" / "autonomous_trader.py").write_text("# v1\n")
    (srv.units / "titan-bot.service").write_text("[Service]\nExecStart=/bin/false\n")
    (srv.etc / "titan2.env").write_text("ZERODHA_API_KEY=k\n")
    (srv.var / "backups" / "KEEP").write_text("state\n")
    srv.env = _env_for(srv)
    srv.status(DB_EXISTS=0)
    srv.clear_calls()
    return srv


def clone_server(template: Server, tmp: Path) -> Server:
    """A copy of a prepared server (relative links only, so the copy is self-contained)."""
    r = subprocess.run([BASH, "-c", f'cp -a "{bpath(template.tmp)}/." "{bpath(tmp)}/"'], env=template.env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    srv = _server_at(tmp)
    srv.env = _env_for(srv)
    srv.clear_calls()
    (srv.state / "alerts.log").unlink(missing_ok=True)
    return srv
