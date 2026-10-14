"""Self-check phases (DETAIL §13.7, normative): morning (08:40 oneshot, Telegram summary), prestart (in the bot,
after prep), deploy (before the release switch, §13.6 step 6) and cutover (Stage 1, §13.11).

CHECKS is the §13.7 matrix, row for row: the phases a row runs in and what its failure does (Effect, the matrix's
"Effect of a prestart FAIL" column). In the in-bot prestart phase the runtime applies Report.halts() (each the
row's entry halt; halts never stop exits), Report.alert_commands() and Report.demotions(). Report.fatal marks a
failed exit-78 row (stray modules / imports / patched Kite, DB integrity); the runtime makes those checks itself
before READY (§9.5 steps 2 and 4: assert_clean_cwd() and the store's integrity_check), where exiting 78 is safe.
The CLI phases only report: exit 2 when any row FAILs, else 0 (WARNs pass), with a JSON copy written to
reports/selfcheck-*.json. No check writes trading state or a cache; the deploy phase migrates a throwaway copy.

Runtime use: startup step 2 calls assert_clean_cwd() (SelfcheckError -> exit 78). After prep, off the core thread:
run_phase(PRESTART, Context(PRESTART, env=os.environ, kite=<KiteData>, instruments=<InstrumentStore, or a callable
raising the prep error>, ban_list=<BanList>, bars=<(have, total)>, settings=<LoadResult>, calendar=<calendar>)),
then the core raises its halts, dispatches its alert commands and demotes its demotions. The CLI
`titan2 selfcheck --phase P [--db-copy PATH]` is cli(P, db_copy=PATH).

Fail closed: a row that cannot verify its condition (a tool missing, a read failing, any exception) fails.

The laptop phase (the Windows laptop profile, PAPER only: `titan2 laptop`, `titan2 laptop check`) runs LAPTOP_CHECKS
instead of the matrix: Python and its packages, the env file, the token, the clock against internet time (entries
stop beyond 1 s, the reading's own error counted), the calendar, the instruments dump, disk, the DB, settings, AC
power (WARN only) and LIVE off. They replace the systemd,
users, v1 and egress-IP rows. Every laptop FAIL is an entry halt (Report.halts()), never an exit, so Report.fatal is
always False there. Its env file and var dir default to the laptop profile's (TITAN2_ENV_FILE, else
%APPDATA%\\titan2\\titan2.env; TITAN2_VAR, else %LOCALAPPDATA%\\titan2\\var; the Linux paths elsewhere).

Every outside resource is injectable: Context takes each one as a value or a zero-argument callable (the in-bot
prestart passes its own KiteData, InstrumentStore, ban list and bar coverage), and System wraps the host probes.

Ported from agentic_trader/pre_market_check.py:47-85, 116-142, 169-186 @b4694f3 — changes: a matrix of named checks
with per-phase effects replaces print-and-count; the token check also requires today's session file and the right
user; disk is an absolute 2 GB floor on the state disk and low memory WARNs at 300 MB; the DB is opened read-only
(deploy migrates a copy); dotenv, Dhan, ML-model, OI and network-latency checks are dropped.
"""

from __future__ import annotations

import importlib.machinery
import ipaddress
import json
import logging
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from titan2 import alerts, settings as settings_mod, store as store_mod, token as token_mod
from titan2.clock import RealClock, SessionCalendar, iso, to_ist
from titan2.core.events import Alert
from titan2.model import TERMINAL_STRUCT, Account, HaltClears

log = logging.getLogger(__name__)

MORNING, PRESTART, DEPLOY, CUTOVER = "morning", "prestart", "deploy", "cutover"
PHASES = (MORNING, PRESTART, DEPLOY, CUTOVER)    # the §13.7 matrix's phases
LAPTOP = "laptop"                                # the laptop profile's phase: its own rows (LAPTOP_CHECKS)
ALL_PHASES = PHASES + (LAPTOP,)
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

ENV_FILE = token_mod.ENV_FILE                    # /etc/titan2/titan2.env  root:titan 0640
DASH_ENV_FILE = Path("/etc/titan2/dash.env")     # root:titandash 0640 (DETAIL §13.1)
ENV_OWNER, BOT_GROUP, DASH_USER, DASH_GROUP = "root", "titan", "titandash", "titandash"
FORBIDDEN_BOT_ENV = ("ZERODHA_ACCESS_TOKEN", "ZERODHA_REQUEST_TOKEN", "TRADING_MODE")   # §13.4: deliberately absent
V1_UNITS = ("titan-bot.service", "titan-dashboard.service")
V1_MARKER = "agentic_trader"
CRON_FILES = (Path("/etc/crontab"),)
CRON_DIRS = (Path("/etc/cron.d"), Path("/var/spool/cron/crontabs"))
CALENDAR_AHEAD_DAYS = 30
GB, MB = 1024 ** 3, 1024 ** 2
MIN_DISK_FREE = 2 * GB
MIN_MEM_AVAILABLE = 300 * MB
PREV_BARS_MIN = 20                               # opening_drive needs the previous session's last 20 HIST bars
PREV_BARS_MIN_FRACTION = 0.90
RATES_MAX_AGE_DAYS = 180
EGRESS_IP_URL = "https://checkip.amazonaws.com"  # AWS's what-is-my-IP service: the address the broker sees
EOD_DONE_KIND, EOD_DONE_PHASE = "PHASE", "DONE"  # the engine's event when the EOD timeline completes (15:40)
CMD_TIMEOUT_S = 15.0
FRESH_TIMEOUT_S = 180.0
DETAIL_MAX = 600
HALT_REASON_MAX = 300
FRESH_OK = "TITAN2_FRESH_OK"
# The laptop phase
LAPTOP_IMPORTS = ("kiteconnect", "pyotp", "requests", "flask")
MIN_PYTHON = (3, 10)
LAPTOP_ENV_KEYS = ("ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_USER_ID")   # the bot and the manual token flow
LOGIN_ENV_KEYS = ("ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET")                     # the auto TOTP login
TELEGRAM_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
# The laptop clock row: entries stop (HALT CLOCK_UNSYNCED) when this clock can be more than this far off internet
# time, the reading's own error counted (EC2's ntp row asks chrony through timedatectl instead).
MAX_CLOCK_OFFSET_S = 1.0
IMPORTS_DONE, PY_TAG, NO_IMPORT_TAG = "TITAN2_IMPORTS_DONE", "TITAN2_PYTHON ", "TITAN2_NO_IMPORT "
# A fresh interpreter (the bot's own, from its working directory): its version, then each package the laptop day needs.
IMPORT_CHECK = "\n".join((
    "import importlib, sys",
    f"print({PY_TAG!r} + '.'.join(str(n) for n in sys.version_info[:3]))",
    f"for name in {LAPTOP_IMPORTS!r}:",
    "    try:",
    "        importlib.import_module(name)",
    "    except Exception as exc:",
    "        text = ' '.join(str(exc).split())[:150].encode('ascii', 'replace').decode('ascii')",
    f"        print({NO_IMPORT_TAG!r} + name + ': ' + type(exc).__name__ + (': ' + text if text else ''))",
    f"print({IMPORTS_DONE!r})",
))
SKIP_IMPORT = ("titan2.__main__", "titan2.dash.app")   # the CLI entry, and the dashboard app (needs dash.env)
# A fresh interpreter, run from the bot's WorkingDirectory: imports every titan2 module (catching a broken or
# shadowed release), then checks that nothing patched KiteConnect's order path at import time.
FRESH_CHECK = "\n".join((
    "import importlib, pkgutil, titan2",
    "for m in pkgutil.walk_packages(titan2.__path__, 'titan2.'):",
    f"    if m.name not in {SKIP_IMPORT!r}:",
    "        importlib.import_module(m.name)",
    "from titan2.broker.kite import assert_kite_unpatched",
    "assert_kite_unpatched()",
    f"print({FRESH_OK!r})",
))


class SelfcheckError(Exception):
    """A startup check failed (assert_clean_cwd); the runtime exits 78."""


# ---------------------------------------------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Effect:
    """What a failure of one row does."""

    status: str                                          # FAIL or WARN: how a failure is reported in every phase
    note: str                                            # the matrix's text
    halt: tuple[str, str, HaltClears] | None = None      # (kind, scope, clears) raised by the prestart phase
    alert: str | None = None                             # severity of the alert sent for a failure
    exit_code: int | None = None                         # 78: fatal at startup (§9.5)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    title: str
    phases: frozenset[str]
    effect: Effect
    fn: Callable[[Context], tuple]                       # -> (ok: bool | None, detail[, data]); None = SKIP


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    title: str
    status: str                                          # PASS | WARN | FAIL | SKIP
    detail: str
    effect: Effect | None = None                         # set when status is WARN or FAIL
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HaltSpec:
    kind: str
    scope: str
    clears: HaltClears
    reason: str


def _ph(*phases: str) -> frozenset[str]:
    return frozenset(phases)


def _halt(kind: str, scope: str = "GLOBAL", clears: HaltClears = HaltClears.AUTO) -> tuple[str, str, HaltClears]:
    return kind, scope, clears


# ---------------------------------------------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------------------------------------------
class System:
    """Host probes (Linux). Tests pass a fake with the same methods."""

    def run(self, cmd: Sequence[str], timeout: float, cwd: Path | None = None) -> tuple[int, str]:
        """(returncode, stdout + stderr); 127 = not found, 124 = timed out. Never raises."""
        try:
            p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, cwd=cwd,
                               stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return 127, f"{cmd[0]}: not found"
        except subprocess.TimeoutExpired:
            return 124, f"{cmd[0]}: timed out after {timeout:.0f} s"
        except OSError as exc:
            return 126, f"{cmd[0]}: {exc}"
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def read_text(self, path: Path) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")

    def list_dir(self, path: Path) -> list[Path]:
        return sorted(Path(path).iterdir())

    def stat(self, path: Path) -> os.stat_result:
        return os.stat(path)

    def user_name(self, uid: int) -> str:
        import pwd                                       # POSIX only: elsewhere ownership cannot be verified
        return pwd.getpwuid(uid).pw_name

    def group_name(self, gid: int) -> str:
        import grp
        return grp.getgrgid(gid).gr_name

    def current_user(self) -> str:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name

    def user_groups(self, user: str) -> set[str]:
        """The user's primary and supplementary group names. KeyError when there is no such user."""
        import grp
        import pwd
        primary = pwd.getpwnam(user).pw_gid
        return {grp.getgrgid(primary).gr_name} | {g.gr_name for g in grp.getgrall() if user in g.gr_mem}

    def disk_free(self, path: Path) -> int:
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(p).free

    def mem_available(self) -> int | None:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None

    def egress_ip(self) -> str:
        import requests                                  # lazy
        resp = requests.get(EGRESS_IP_URL, timeout=5)
        resp.raise_for_status()
        return resp.text.strip()


# ---------------------------------------------------------------------------------------------------------------
# What the checks read
# ---------------------------------------------------------------------------------------------------------------
def _env_file_secrets(path: Path) -> set[str]:
    """The secret values in an env file, for scrub(); none when it cannot be read (the env_file row says why)."""
    try:
        values = token_mod.read_env_file(path)
    except (OSError, ValueError):
        return set()
    return {str(values.get(k, "")) for k in token_mod.SECRET_ENV_KEYS}


class Context:
    """Inputs for one run. Resources (settings, calendar, session, kite, instruments, ban_list, bars, telegram,
    healthcheck) are each a value or a zero-argument callable; None means the production source. Each is resolved
    once per run; a failure to resolve fails every row that needs it. In the laptop phase env_file and var_dir default
    to the laptop profile's (token.env_file, settings.default_var_dir: TITAN2_ENV_FILE / TITAN2_VAR from the env given
    or the process, else the Windows folders), and env is that env file overridden by the env given (default: the
    process environment), as token.load_env does."""

    def __init__(self, phase: str, *, env: Mapping[str, str] | None = None, now: datetime | None = None,
                 var_dir: str | Path | None = None, app_dir: str | Path | None = None, system: Any = None,
                 python: str | None = None, db_copy: str | Path | None = None, env_file: str | Path | None = None,
                 dash_env_file: str | Path = DASH_ENV_FILE, settings: Any = None, calendar: Any = None,
                 session: Any = None, kite: Any = None, instruments: Any = None, ban_list: Any = None,
                 bars: Any = None, telegram: Any = None, healthcheck: Any = None) -> None:
        if phase not in ALL_PHASES:
            raise ValueError(f"unknown self-check phase {phase!r}; one of {', '.join(ALL_PHASES)}")
        self.phase = phase
        laptop = phase == LAPTOP
        if env_file is None:
            env_file = token_mod.env_file({**os.environ, **(env or {})}) if laptop else ENV_FILE
        if laptop:
            env = token_mod.load_env(env_file, environ=env)
        elif env is None:
            env = token_mod.load_env()
        self.env: Mapping[str, str] = env
        self.now = to_ist(now) if now is not None else RealClock().now_wall()
        default_var = (settings_mod.default_var_dir({**os.environ, **self.env}) if laptop else
                       self.env.get("TITAN2_VAR") or os.environ.get("TITAN2_VAR") or settings_mod.DEFAULT_VAR_DIR)
        self.var_dir = Path(var_dir if var_dir is not None else default_var)
        self.app_dir = Path(app_dir) if app_dir is not None else Path.cwd()
        self.system = system if system is not None else System()
        self.python = python or sys.executable
        self.db_copy = Path(db_copy) if db_copy is not None else None
        self.env_file, self.dash_env_file = Path(env_file), Path(dash_env_file)
        self.db_path = self.var_dir / "db" / store_mod.DB_FILENAME
        self.session_path = token_mod.session_path(self.var_dir)
        self._sources: dict[str, Any] = {
            "settings": settings if settings is not None else
            (lambda: settings_mod.load(self.var_dir / "config" / "settings.json")),
            "calendar": calendar if calendar is not None else SessionCalendar.load,
            "session": session if session is not None else (lambda: token_mod.read_session(self.session_path)),
            "kite": kite if kite is not None else self._default_kite,
            "instruments": instruments if instruments is not None else self._default_instruments,
            "ban_list": ban_list if ban_list is not None else self._default_ban_list,
            "bars": bars if bars is not None else self._default_bars,
            "telegram": telegram if telegram is not None else (lambda: alerts.Telegram.from_env(self.env)),
            "healthcheck": healthcheck if healthcheck is not None else (lambda: alerts.Healthcheck.from_env(self.env)),
        }
        self._cache: dict[str, tuple[bool, Any]] = {}
        self._store: store_mod.Store | None = None
        # scrub() also removes the laptop env file's secrets, from the first row on, whatever env was given
        self._secrets: set[str] = _env_file_secrets(self.env_file) if laptop else set()

    @property
    def today(self) -> date:
        return self.now.date()

    def _get(self, name: str) -> Any:
        if name not in self._cache:
            src = self._sources[name]
            try:
                self._cache[name] = (True, src() if callable(src) else src)
            except Exception as exc:
                self._cache[name] = (False, exc)
        ok, value = self._cache[name]
        if not ok:
            raise value
        return value

    def settings(self) -> settings_mod.LoadResult:
        return self._get("settings")

    def calendar(self) -> SessionCalendar:
        return self._get("calendar")

    def session(self) -> token_mod.Session:
        return self._get("session")

    def kite(self) -> Any:
        return self._get("kite")

    def instruments(self) -> Any:
        return self._get("instruments")

    def ban_list(self) -> Any:
        return self._get("ban_list")

    def bars(self) -> tuple[int, int]:
        return self._get("bars")

    def telegram(self) -> alerts.Telegram | None:
        return self._get("telegram")

    def healthcheck(self) -> alerts.Healthcheck | None:
        return self._get("healthcheck")

    def store(self) -> store_mod.Store:
        """titan2.db opened read-only (closed by close())."""
        if self._store is None:
            self._store = store_mod.open_readonly(self.db_path)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def scrub(self, text: str) -> str:
        """Detail text with every secret value removed."""
        secrets = [str(self.env.get(k, "")) for k in token_mod.SECRET_ENV_KEYS] + sorted(self._secrets)
        ok, sess = self._cache.get("session", (False, None))
        if ok and isinstance(sess, token_mod.Session):
            secrets.append(sess.access_token)
        for s in sorted(set(secrets), key=len, reverse=True):     # longest first: no secret is left half-replaced
            if len(s) >= 4:
                text = text.replace(s, "***")
        return text

    # -- production sources -------------------------------------------------------------------------------------
    def _default_kite(self) -> Any:
        from titan2.broker.kite import KiteData          # lazy: kiteconnect imports twisted

        api_key = str(self.env.get("ZERODHA_API_KEY", "")).strip()
        if not api_key:
            raise SelfcheckError("ZERODHA_API_KEY is not set")
        return KiteData(token_mod.kite_client(api_key, self.session().access_token))

    def _default_instruments(self) -> Any:
        """Today's cached dump, else a fresh download validated in memory (only the bot's prep writes the cache)."""
        from titan2 import instruments as ins

        s = self.settings().settings
        opts = {"min_td_index": s.MIN_TD_TO_EXPIRY_INDEX, "min_td_stock": s.MIN_TD_TO_EXPIRY_STOCK}
        found = ins.InstrumentStore.from_cache(self.var_dir / "cache", self.today, self.calendar(), **opts)
        if found is not None:
            return found
        kite = self.kite()
        return ins.InstrumentStore.from_rows(kite.instruments("NFO"), kite.instruments("NSE"), self.today,
                                             self.calendar(), **opts)

    def _default_ban_list(self) -> Any:
        """The override or today's cache; else fetch and parse without caching it (prep caches)."""
        from titan2 import instruments as ins

        found = ins.load_ban_list(self.today, cache_dir=self.var_dir / "cache", config_dir=self.var_dir / "config",
                                  fetch=None)
        if found.ok or found.source != ins.MISSING:
            return found
        try:
            text = ins.fetch_ban_list_text(ins.ban_list_url(self.env))
            symbols = ins.parse_ban_list(text, self.today)
        except Exception as exc:
            return ins.BanList(self.today, None, ins.MISSING, f"fetch failed: {exc}")
        return ins.BanList(self.today, symbols, ins.NSE, f"{len(symbols)} banned (fetched; prep caches it)")

    def _default_bars(self) -> tuple[int, int]:
        """(stocks with >= PREV_BARS_MIN previous-session bars, universe size). Before prep (morning) yesterday's
        LIVE bars count too; in prestart only HIST bars, which are what the strategies read."""
        s = self.settings().settings
        universe = sorted(self.instruments().universe((), s.STOCK_ALLOWLIST, s.STOCK_DENYLIST))
        prev = self.calendar().prev_trading_day(self.today)
        if prev is None or not universe:
            return 0, len(universe)
        sources = ("HIST",) if self.phase == PRESTART else ("HIST", "LIVE")
        rows = self.store().query(
            "SELECT symbol, COUNT(DISTINCT start_ts) FROM bars_5m WHERE substr(start_ts, 1, 10) = ? AND valid = 1 "
            f"AND source IN ({','.join('?' * len(sources))}) GROUP BY symbol", [prev.isoformat(), *sources])
        counts = {r[0]: r[1] for r in rows}
        return sum(1 for sym in universe if counts.get(sym, 0) >= PREV_BARS_MIN), len(universe)


# ---------------------------------------------------------------------------------------------------------------
# Startup helper (runtime §9.5 step 2) and small utilities
# ---------------------------------------------------------------------------------------------------------------
# Imported by name whatever else is on sys.path: the release itself and the site module's startup hooks.
_ALWAYS_IMPORTED = frozenset({"titan2", "sitecustomize", "usercustomize"})


def _resolved(entry: str) -> Path | None:
    try:
        return Path(entry or os.curdir).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def installed_import_dirs(skip: str | Path | None = None) -> list[str]:
    """The sys.path entries that hold installed code (the standard library and the venv's site-packages: everything
    under the interpreter's prefixes), without `skip`. The bot's own working directory is never one of them."""
    roots = {r for r in (_resolved(x) for x in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix})
             if r is not None}
    skipped = _resolved(str(skip)) if skip is not None else None
    out = []
    for entry in sys.path:
        p = _resolved(entry) if isinstance(entry, str) else None
        if p is not None and p != skipped and any(p == r or r in p.parents for r in roots):
            out.append(entry)
    return out


def shadows_an_import(name: str, installed: Sequence[str]) -> bool:
    """True when a module `name` in the bot's working directory would replace one the bot can import: `python -m
    titan2` searches that directory first (§9.5), so a name the standard library, the venv (`installed`, see
    installed_import_dirs) or titan2 also provides is loaded from there instead. A failed lookup counts as shadowing
    (fail closed)."""
    if name in _ALWAYS_IMPORTED or name in sys.stdlib_module_names or name in sys.builtin_module_names:
        return True
    try:
        return importlib.machinery.PathFinder.find_spec(name, list(installed)) is not None
    except Exception as exc:                             # a broken path entry or package: cannot tell
        log.warning("cannot tell whether %r shadows an import (%s): it counts as shadowing", name, exc)
        return True


def _module_name(name: str, suffixes: Sequence[str]) -> str | None:
    """'json.py' -> 'json', 'x.cpython-312-x86_64-linux-gnu.so' -> 'x'; None for a file no import can load."""
    for suffix in sorted(suffixes, key=len, reverse=True):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return None


def cwd_problems(path: str | Path) -> list[str]:
    """Why `python -m titan2` started in path could import something other than the release: a module file or a
    package next to the titan2 symlink whose name the bot can import from elsewhere (v1's tree shadowing the
    release, §9.5). Anything else (an operator's _research.py, data, notes) is never imported by the bot and is left
    alone: refusing it would make every restart exit 78, which systemd never restarts, while legs are open."""
    p = Path(path)
    suffixes = tuple(importlib.machinery.all_suffixes())
    try:
        entries = sorted(p.iterdir(), key=lambda e: e.name)
    except OSError as exc:
        return [f"cannot list {p}: {type(exc).__name__}"]
    installed = installed_import_dirs(skip=p)
    out = []
    for e in entries:
        if e.name == "titan2":
            continue
        if e.is_file():
            stem = _module_name(e.name, suffixes)
            if stem is not None and shadows_an_import(stem, installed):
                out.append(f"module file {e.name}")
        elif (e.is_dir() and any((e / f"__init__{s}").exists() for s in suffixes)
              and shadows_an_import(e.name, installed)):
            out.append(f"package {e.name}/")
    return out


def assert_clean_cwd(path: str | Path | None = None) -> None:
    """Raise SelfcheckError (the runtime exits 78) if the working directory could shadow the release."""
    problems = cwd_problems(path if path is not None else Path.cwd())
    if problems:
        shown = ", ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else "")
        raise SelfcheckError(f"working directory is not clean: {shown}")


def _first_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[0] if lines else ""


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _is_trading_day(cal: SessionCalendar, text: str) -> bool:
    try:
        return cal.is_trading_day(date.fromisoformat(text))
    except ValueError:
        return False


def _file_rules(system: Any, path: Path, *, owner: str, group: str | None, allowed: int) -> list[str]:
    """Problems with a secret file's owner, group and mode (no permission bit outside `allowed`)."""
    try:
        st = system.stat(path)
    except FileNotFoundError:
        return [f"{path} is missing"]
    except OSError as exc:
        return [f"cannot stat {path}: {type(exc).__name__}"]
    out = []
    mode = stat.S_IMODE(st.st_mode)
    if mode & ~allowed:
        out.append(f"{path} has mode {mode:04o}; allowed at most {allowed:04o}")
    user = system.user_name(st.st_uid)
    if user != owner:
        out.append(f"{path} is owned by {user}, not {owner}")
    if group is not None:
        grp_name = system.group_name(st.st_gid)
        if grp_name != group:
            out.append(f"{path} has group {grp_name}, not {group}")
    return out


def _cron_lines(ctx: Context) -> tuple[list[str], list[str]]:
    """(active cron lines, unreadable sources). User crontabs live in a root-only spool: v1's (user ubuntu) is never
    readable here, and inside the bot's sandbox (NoNewPrivileges) `crontab -l` cannot read even titan's own. Those are
    noted, not failed: Stage 0 removes v1's crontab, and the cwd row catches v1's code under agentic_trader/."""
    lines: list[str] = []
    unreadable: list[str] = []
    rc, out = ctx.system.run(["crontab", "-l"], CMD_TIMEOUT_S)
    if rc == 0:
        lines += out.splitlines()
    elif "no crontab" not in out.lower():
        unreadable.append(f"crontab -l (rc {rc})")
    sources = list(CRON_FILES)
    for d in CRON_DIRS:
        try:
            sources += ctx.system.list_dir(d)
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(str(d))
    for src in sources:
        try:
            lines += ctx.system.read_text(src).splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(str(src))
    active = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    return active, unreadable


# ---------------------------------------------------------------------------------------------------------------
# The rows (each returns (ok, detail[, data]); ok None = not applicable)
# ---------------------------------------------------------------------------------------------------------------
def _check_token(ctx: Context) -> tuple:
    sess = ctx.session()
    if not sess.is_fresh(ctx.now):
        return False, f"session {sess.fingerprint} was issued {iso(sess.issued_at)}, not today after 06:00 IST"
    expected = str(ctx.env.get("ZERODHA_USER_ID", "")).strip()
    if not expected:
        return False, "ZERODHA_USER_ID is not set"
    got = str(ctx.kite().profile().get("user_id") or "").strip()
    if got.upper() != expected.upper() or sess.user_id.upper() != expected.upper():
        return False, f"profile() user {got!r} / session user {sess.user_id!r} is not ZERODHA_USER_ID {expected!r}"
    return True, f"user {got}; session {sess.fingerprint} issued {iso(sess.issued_at)}"


def _check_ntp(ctx: Context) -> tuple:
    rc, out = ctx.system.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], CMD_TIMEOUT_S)
    value = _first_line(out)
    if rc == 0 and value == "yes":
        return True, "NTPSynchronized=yes"
    return False, f"NTPSynchronized={value or '?'} (timedatectl rc {rc})"


def _check_calendar(ctx: Context) -> tuple:
    cal = ctx.calendar()
    span = f"{cal.coverage_start}..{cal.coverage_end}"
    if cal.covers_ahead(ctx.today, CALENDAR_AHEAD_DAYS):
        return True, f"sessions file covers {span}"
    horizon = ctx.today + timedelta(days=CALENDAR_AHEAD_DAYS)
    return False, (f"sessions file covers {span}, not through {horizon}: dates beyond it are not trading days; "
                   "add NSE's next holiday circular to titan2/data/nse_sessions.json")


def _check_instruments(ctx: Context) -> tuple:
    return True, repr(ctx.instruments())


def _check_ban_list(ctx: Context) -> tuple:
    bl = ctx.ban_list()
    if bl.ok:
        return True, f"{bl.source}: {bl.detail}"
    return False, f"{bl.source}: {bl.detail}; every stock is ineligible today (G08)"


def _check_prev_bars(ctx: Context) -> tuple:
    have, total = ctx.bars()
    if total <= 0:
        return False, "the stock universe is empty"
    frac = have / total
    detail = f"{have}/{total} universe stocks ({frac:.0%}) have >= {PREV_BARS_MIN} previous-session bars"
    ok = frac >= PREV_BARS_MIN_FRACTION
    return ok, detail if ok else detail + "; the others are ineligible today"


def _check_settings(ctx: Context) -> tuple:
    res = ctx.settings()
    return bool(res.valid), res.summary()


def _check_db(ctx: Context) -> tuple:
    latest = store_mod.latest_schema_version()
    if ctx.phase == DEPLOY:
        return _migration_dry_run(ctx, latest)
    if not ctx.db_path.exists():
        return None, f"no database at {ctx.db_path} yet (the bot creates it at its first start)"
    with closing(store_mod.open_readonly(ctx.db_path)) as st:
        problems, version = st.integrity_check(), st.user_version
    if problems:
        return False, f"integrity_check: {'; '.join(problems[:3])}"
    if version > latest:
        return False, f"schema {version} is newer than this release's {latest}: deploy that release or restore a backup"
    if version < latest:
        if ctx.phase == PRESTART:
            return False, f"schema {version}, but the running release needs {latest}"
        return True, f"integrity ok; schema {version} migrates to {latest} at the next bot start"
    return True, f"integrity ok; schema {version}"


def _migration_dry_run(ctx: Context, latest: int) -> tuple:
    """Migrate a throwaway copy of --db-copy (the operator's copy is never modified), or an empty DB when the live
    one does not exist yet, then integrity-check it."""
    src = ctx.db_copy
    if src is not None and not src.is_file():
        return False, f"--db-copy {src} is not a file"
    what = f"a copy of {src}" if src is not None else "an empty database"
    with tempfile.TemporaryDirectory(prefix="titan2-selfcheck-") as tmp:
        target = Path(tmp) / store_mod.DB_FILENAME
        if src is not None:                              # bytes only: SQLite never opens the operator's file
            shutil.copyfile(src, target)
            wal = Path(f"{src}-wal")
            if wal.is_file():
                shutil.copyfile(wal, Path(f"{target}-wal"))
        st = store_mod.Store(target, backup_dir=Path(tmp) / "backups")
        try:
            problems, version, migrated = st.integrity_check(), st.user_version, list(st.migrated)
        finally:
            st.close()
    if problems:
        return False, f"after migrating {what}: integrity_check: {'; '.join(problems[:3])}"
    if version != latest:
        return False, f"after migrating {what}: schema {version}, expected {latest}"
    return True, f"migrations {migrated or 'none pending'} applied to {what}; integrity ok; schema {version}"


def _check_resources(ctx: Context) -> tuple:
    free = ctx.system.disk_free(ctx.var_dir)
    mem = ctx.system.mem_available()
    mem_text = "unknown" if mem is None else f"{mem / MB:.0f} MB"
    detail = f"disk {free / GB:.1f} GB free at {ctx.var_dir}; memory {mem_text} available"
    ok = free >= MIN_DISK_FREE and mem is not None and mem >= MIN_MEM_AVAILABLE
    return ok, detail if ok else detail + " (need >= 2 GB disk and >= 300 MB memory)"


def _check_v1_disabled(ctx: Context) -> tuple:
    problems = []
    for unit in V1_UNITS:
        rc, out = ctx.system.run(["systemctl", "is-enabled", unit], CMD_TIMEOUT_S)
        state = _first_line(out)
        if rc == 127:
            problems.append(f"cannot run systemctl ({state})")
            break
        missing = "no such file" in out.lower() or "not-found" in out.lower() or "not found" in out.lower()
        if state not in ("masked", "masked-runtime") and not missing:
            problems.append(f"{unit} is-enabled={state or '?'} (must be masked)")
        rc, out = ctx.system.run(["systemctl", "is-active", unit], CMD_TIMEOUT_S)
        state = _first_line(out)
        if state in ("active", "activating", "reloading", "deactivating", "refreshing"):
            problems.append(f"{unit} is {state}")
    lines, unreadable = _cron_lines(ctx)
    hits = [ln.strip() for ln in lines if V1_MARKER in ln]
    if hits:
        problems.append(f"{len(hits)} cron line(s) mention {V1_MARKER}: {hits[0][:120]}")
    note = f" (not readable here: {', '.join(unreadable)})" if unreadable else ""
    return (not problems), ("; ".join(problems) or "v1 units masked and inactive; no v1 cron line") + note


def _check_code_clean(ctx: Context) -> tuple:
    problems = cwd_problems(ctx.app_dir)
    rc, out = ctx.system.run([ctx.python, "-c", FRESH_CHECK], FRESH_TIMEOUT_S, cwd=ctx.app_dir)
    if rc != 0 or FRESH_OK not in out:
        problems.append(f"fresh interpreter failed (rc {rc}): {_last_line(out)}")
    return (not problems), ("; ".join(problems) or
                            f"{ctx.app_dir} has no module that shadows an import; a fresh interpreter imports every "
                            "titan2 module and KiteConnect is unpatched")


def _check_env_files(ctx: Context) -> tuple:
    s = ctx.system
    problems = _file_rules(s, ctx.env_file, owner=ENV_OWNER, group=BOT_GROUP, allowed=0o640)
    notes = []
    try:
        bad = sorted(k for k in token_mod.parse_env(s.read_text(ctx.env_file)) if k in FORBIDDEN_BOT_ENV)
        if bad:
            problems.append(f"{ctx.env_file} must not hold {', '.join(bad)} (the token lives in the session file, "
                            "modes in the DB)")
    except OSError as exc:
        problems.append(f"cannot read {ctx.env_file}: {type(exc).__name__}")
    try:
        if BOT_GROUP in s.user_groups(DASH_USER):
            problems.append(f"user {DASH_USER} is in group {BOT_GROUP}, so the dashboard can read {ctx.env_file}")
    except KeyError:
        problems.append(f"no user {DASH_USER}: the dashboard's isolation cannot be verified")
    problems += _file_rules(s, ctx.dash_env_file, owner=ENV_OWNER, group=DASH_GROUP, allowed=0o640)
    try:
        leaked = sorted(k for k in token_mod.parse_env(s.read_text(ctx.dash_env_file)) if k.startswith("ZERODHA_"))
        if leaked:
            problems.append(f"{ctx.dash_env_file} holds {', '.join(leaked)}")
    except PermissionError:
        notes.append(f"{ctx.dash_env_file} content is not readable here (expected; its permissions were checked)")
    except FileNotFoundError:
        pass                                             # already reported as missing
    except OSError as exc:
        problems.append(f"cannot read {ctx.dash_env_file}: {type(exc).__name__}")
    try:
        s.stat(ctx.session_path)
        session_exists = True
    except FileNotFoundError:
        session_exists = False
    if session_exists:
        me = s.current_user()
        problems += _file_rules(s, ctx.session_path, owner=me, group=None, allowed=0o600)
        problems += _file_rules(s, ctx.session_path.parent, owner=me, group=None, allowed=0o700)
    detail = "; ".join(problems) or "env files and the session file have the right owners and modes; no ZERODHA_* " \
                                    "key in dash.env"
    return (not problems), detail + (f" ({'; '.join(notes)})" if notes else "")


def _check_broker_flat(ctx: Context) -> tuple:
    kite = ctx.kite()
    positions = [p for p in kite.positions(Account.LIVE) if p.exchange == "NFO" and p.net_qty != 0]
    orders = [o for o in kite.orders_today(Account.LIVE) if o.status == "WORKING"]
    problems = []
    if positions:
        problems.append("NFO positions: " + ", ".join(f"{p.tradingsymbol} {p.product} {p.net_qty:+d}"
                                                     for p in positions[:10]))
    if orders:
        problems.append("open orders: " + ", ".join(f"{o.tradingsymbol} {o.side} {o.qty} ({o.raw_status})"
                                                   for o in orders[:10]))
    return (not problems), "; ".join(problems) or "no NFO positions and no open orders"


def _check_egress_ip(ctx: Context) -> tuple:
    want = str(ctx.env.get("REGISTERED_EGRESS_IP", "")).strip()
    if not want:
        if str(ctx.env.get("ALLOW_LIVE", "")).strip() == "1":
            return False, ("ALLOW_LIVE=1 but REGISTERED_EGRESS_IP is not set: a registered static IP is required "
                           "for LIVE")
        return None, "REGISTERED_EGRESS_IP not set (LIVE is off)"
    got = ctx.system.egress_ip()
    try:
        same = ipaddress.ip_address(got) == ipaddress.ip_address(want)
    except ValueError:
        return False, f"cannot compare egress {got!r} with REGISTERED_EGRESS_IP {want!r}"
    return same, f"egress IP {got}" + ("" if same else f" is not REGISTERED_EGRESS_IP {want}: LIVE is off today")


def _check_live_ready(ctx: Context) -> tuple:
    if not ctx.db_path.exists():
        return None, "no database yet: no LIVE strategy"
    st = ctx.store()
    live = [m for m in st.strategy_modes() if "LIVE" in (m.get("mode"), m.get("pending_mode"))]
    if not live:
        return None, "no LIVE strategy"
    demote = []
    for m in live:
        trial = st.trial(m["strategy"], m["version"])
        if trial is None or trial.get("state") != "CERTIFIED":
            demote.append(m["strategy"])
    floor = ctx.settings().settings.LIVE_MIN_MARGIN_RS
    available = ctx.kite().margins(Account.LIVE).available_rs
    low = available < floor
    problems = []
    if low:
        problems.append(f"LIVE margin available Rs {available:,.0f} < LIVE_MIN_MARGIN_RS Rs {floor:,.0f}")
    if demote:
        problems.append(f"no CERTIFIED trial for the current version (demote): {', '.join(demote)}")
    detail = "; ".join(problems) or (f"LIVE strategies {', '.join(m['strategy'] for m in live)} certified; "
                                     f"margin Rs {available:,.0f}")
    return (not problems), detail, {"demote": demote, "halt": low}


def _check_rates(ctx: Context) -> tuple:
    from titan2 import charges                           # lazy: only this row needs the rate table

    newest = max(charges.RATESETS, key=lambda r: r.effective_from)
    basis = max(newest.effective_from, charges.RATES_VERIFIED_ON)
    age = (ctx.today - basis).days
    detail = f"newest RateSet {newest.version}; rates last confirmed {basis} ({age} days ago)"
    ok = age < RATES_MAX_AGE_DAYS
    return ok, detail if ok else detail + ": recheck zerodha.com/charges against a contract note"


def _check_prev_session(ctx: Context) -> tuple:
    if not ctx.db_path.exists():
        return None, "no database yet"
    st = ctx.store()
    today = ctx.today.isoformat()
    terminal = sorted(s.value for s in TERMINAL_STRUCT)
    open_rows = st.query(f"SELECT session, COUNT(*) FROM structures WHERE session < ? AND state NOT IN "
                         f"({','.join('?' * len(terminal))}) GROUP BY session ORDER BY session", [today, *terminal])
    problems = []
    if open_rows:
        problems.append("not flat: " + ", ".join(f"{r[1]} open structure(s) from {r[0]}" for r in open_rows))
    cal = ctx.calendar()
    sessions = [r[0] for r in st.query("SELECT DISTINCT session FROM events WHERE session < ? "
                                       "ORDER BY session DESC LIMIT 30", [today])]
    last = next((s for s in sessions if _is_trading_day(cal, s)), None)
    if last is None:
        return (not problems), "; ".join(problems) or "no earlier session in the database"
    done = st.scalar("SELECT COUNT(*) FROM events WHERE session = ? AND kind = ? AND "
                     "json_extract(payload, '$.phase') = ?", [last, EOD_DONE_KIND, EOD_DONE_PHASE])
    if not done:
        problems.append(f"session {last} did not finish its EOD timeline (no {EOD_DONE_KIND} {EOD_DONE_PHASE} event)")
    prev = cal.prev_trading_day(ctx.today)
    note = "" if prev is None or prev.isoformat() == last else f" (the bot did not run on {prev})"
    return (not problems), ("; ".join(problems) or f"session {last} ended flat with its EOD timeline done") + note


def _check_notify(ctx: Context) -> tuple:
    problems = []
    tg = ctx.telegram()
    if tg is None:
        problems.append("Telegram is not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
    else:
        try:
            tg.send(f"[titan2] self-check {ctx.phase} {ctx.now:%Y-%m-%d %H:%M}: Telegram test")
        except alerts.AlertError as exc:
            problems.append(str(exc))
    hc = ctx.healthcheck()
    if hc is None:
        problems.append("HEALTHCHECK_URL is not set (no external dead-man)")
    else:
        try:
            hc.ping("log", f"titan2 self-check {ctx.phase}")
        except alerts.AlertError as exc:
            problems.append(str(exc))
    return (not problems), "; ".join(problems) or "Telegram test sent; healthchecks reachable"


# -- the laptop phase's own rows ----------------------------------------------------------------------------------
def _check_python(ctx: Context) -> tuple:
    rc, out = ctx.system.run([ctx.python, "-c", IMPORT_CHECK], FRESH_TIMEOUT_S, cwd=ctx.app_dir)
    lines = [ln.strip() for ln in out.splitlines()]
    version = next((ln[len(PY_TAG):] for ln in lines if ln.startswith(PY_TAG)), "")
    missing = [ln[len(NO_IMPORT_TAG):] for ln in lines if ln.startswith(NO_IMPORT_TAG)]
    if rc != 0 or IMPORTS_DONE not in lines or not version:
        return False, f"{ctx.python} could not run the import check (rc {rc}): {_last_line(out)}"
    problems = []
    try:
        old = tuple(int(n) for n in version.split(".")[:2]) < MIN_PYTHON
    except ValueError:
        old = True
    if old:
        problems.append(f"Python {version} is older than {'.'.join(str(n) for n in MIN_PYTHON)}")
    if missing:
        problems.append(f"{ctx.python} cannot import {'; '.join(missing)} (run titan2 from the venv that "
                        "ops\\windows\\setup.ps1 builds, as ops\\windows\\run_today.ps1 does; or python -m pip install "
                        "-r requirements.lock)")
    return (not problems), "; ".join(problems) or f"Python {version}; {', '.join(LAPTOP_IMPORTS)} import"


def _check_env_file(ctx: Context) -> tuple:
    """Key names only: no value from the file ever reaches a detail (and scrub() removes its secrets anyway)."""
    path = ctx.env_file
    try:
        values = token_mod.read_env_file(path)           # what the token step and the bot read
    except FileNotFoundError:
        return False, (f"{path} is missing: create it with KEY=value lines for "
                       f"{', '.join(LAPTOP_ENV_KEYS + LOGIN_ENV_KEYS)} (TITAN2_ENV_FILE names another file)")
    except ValueError:
        return False, f"{path} is not UTF-8 text: save it as UTF-8 (PowerShell 5's > and Out-File write UTF-16)"
    except OSError as exc:
        return False, f"cannot read {path}: {type(exc).__name__}"
    have = {k for k, v in values.items() if v.strip()}
    notes = []
    if not all(k in have for k in LOGIN_ENV_KEYS):
        notes.append(f"without {' and '.join(LOGIN_ENV_KEYS)} the token step uses the manual request-token flow")
    if not all(k in have for k in TELEGRAM_ENV_KEYS):
        notes.append(f"no Telegram alerts without {' and '.join(TELEGRAM_ENV_KEYS)}")
    ignored = sorted(k for k in have if k in FORBIDDEN_BOT_ENV)
    if ignored:
        notes.append(f"{', '.join(ignored)} ignored (the token lives in the session file, modes in the DB)")
    tail = f" ({'; '.join(notes)})" if notes else ""
    missing = [k for k in LAPTOP_ENV_KEYS if k not in have]
    if missing:
        return False, f"{path} lacks {', '.join(missing)}{tail}"
    found = [k for k in LAPTOP_ENV_KEYS + LOGIN_ENV_KEYS + TELEGRAM_ENV_KEYS if k in have]
    return True, f"{path} sets {', '.join(found)}{tail}"


def _clock_reading(ctx: Context) -> tuple[float, float, str] | None:
    """laptop.clock_offset_s(): (seconds to add to this clock for internet time, how far that reading itself can be
    off, its source), or None when no source answered. A nonsense answer raises ValueError (could not check)."""
    from titan2 import laptop                            # lazy: the laptop profile's host probes

    offset = laptop.clock_offset_s()
    if offset is None:
        return None
    error = getattr(offset, "error_s", 0.0)              # a laptop.ClockOffset; a bare number counts as exact
    for x in (offset, error):
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError(f"clock_offset_s() returned {offset!r} (error {error!r})")
    if error < 0:
        raise ValueError(f"clock_offset_s() returned {offset!r} with a negative error {error!r}")
    return float(offset), float(error), str(getattr(offset, "source", "") or "")


def _check_clock(ctx: Context) -> tuple:
    """Fails closed: entries stop when the clock titan2 uses can be more than MAX_CLOCK_OFFSET_S off internet time,
    the reading's own error (half the NTP round trip, or the whole-second HTTP Date's 0.5 s and half its round trip)
    counted. That clock is the system clock, or, once titan2.clock.CORRECTION holds an offset (the laptop measures
    it at start and every 5 minutes), the system clock corrected by it: then the row judges what remains."""
    from titan2 import clock as clock_mod

    reading = _clock_reading(ctx)
    if reading is None:
        return False, ("clock offset unknown: no internet time source answered (NTP, then the HTTP Date of "
                       "api.kite.trade), so entries stay off")
    offset, error, source = reading
    applied = clock_mod.CORRECTION.offset_s
    side = 'behind' if offset > 0 else 'ahead of'
    if applied is None:
        detail = f"this clock is {abs(offset):.3f} s {side} internet time"
        if error:
            detail += f" (+-{error:.3f} s, {source or 'the reading'})"
        left = offset
    else:
        left = offset - applied                          # what the correction leaves
        detail = (f"titan2's clock is {abs(left):.3f} s {'behind' if left > 0 else 'ahead of'} internet time"
                  + (f" (+-{error:.3f} s, {source or 'the reading'})" if error else "")
                  + f"; it corrects the Windows clock, {abs(offset):.3f} s {side} internet time")
    if abs(left) + error <= MAX_CLOCK_OFFSET_S:
        return True, detail
    beyond = "more than" if abs(left) > MAX_CLOCK_OFFSET_S else "possibly more than"
    if applied is not None:
        return False, (f"{detail}: {beyond} {MAX_CLOCK_OFFSET_S:g} s off even so, so entries stay off. The Windows "
                       "clock moved since titan2 measured it; titan2 measures again within 5 minutes: then stop "
                       "(Ctrl+C once) and start again")
    return False, (f"{detail}: {beyond} {MAX_CLOCK_OFFSET_S:g} s off, so entries stay off. titan2 corrects its own "
                   "clock as soon as an internet time source answers (unless TITAN2_CLOCK_CORRECT=0): check the "
                   "internet connection, then stop (Ctrl+C once) and start again. Or sync Windows: Settings > Time & "
                   "language > Date & time > Sync now (if Windows does not really sync, the one-time w32tm fix in "
                   "docs/LAPTOP.md section 5, admin rights needed)")


def _check_disk(ctx: Context) -> tuple:
    free = ctx.system.disk_free(ctx.var_dir)
    detail = f"disk {free / GB:.1f} GB free at {ctx.var_dir}"
    ok = free >= MIN_DISK_FREE
    return ok, detail if ok else detail + f" (need >= {MIN_DISK_FREE // GB} GB)"


def _check_power(ctx: Context) -> tuple:
    from titan2 import laptop

    status = laptop.power_status()                       # {"on_ac": True | False | None, "battery_pct": 0-100 | None}
    on_ac, pct = status.get("on_ac"), status.get("battery_pct")
    charge = f" (battery {pct}%)" if pct is not None else ""
    if on_ac is True:
        return True, f"on AC power{charge}"
    what = "on battery" if on_ac is False else "power source unknown"
    return False, f"{what}{charge}: plug in; a laptop that sleeps or shuts down stops managing its positions"


def _check_network(ctx: Context) -> tuple:
    """WARN only: a lossy or slow route to Kite costs refused entries (fetch timeouts), never a wrong trade."""
    from titan2 import laptop

    p = laptop.network_probe()                           # titan2.net.Probe: IPv4 TCP connects to api.kite.trade
    detail = f"IPv4 to {p.host}: {p.ok}/{p.tries} connects"
    if p.median_ms is not None:
        detail += f", median {p.median_ms:.0f} ms (worst {p.worst_ms:.0f} ms)"
    if p.clean:
        return True, detail
    why = p.error if p.ok == 0 and p.error else (f"{p.tries - p.ok + p.slow} packet losses" if p.ok < p.tries or p.slow
                                                  else "slow line")
    return False, (f"{detail}: {why}. Expect Kite timeouts, i.e. signals refused as QUOTE:FETCH_FAILED: restart the "
                   "router or use another network (a phone hotspot) before 09:15")


def _check_live_off(ctx: Context) -> tuple:
    from titan2 import laptop

    if not laptop.is_laptop(ctx.env):
        return False, "not the laptop profile (TITAN2_PROFILE=laptop), so LIVE is disabled today instead"
    why = "; ".join(laptop.LIVE_REFUSED_REASONS)
    ignored = "; ALLOW_LIVE=1 is ignored" if str(ctx.env.get("ALLOW_LIVE", "")).strip() == "1" else ""
    return True, f"the laptop profile refuses LIVE ({why}): LIVE-mode strategies run as PAPER{ignored}"


_ALL = frozenset(PHASES)
_A = alerts
# DETAIL §13.7, in table order. The broker row is not run in prestart: recon covers it there ("(recon)").
CHECKS: tuple[Check, ...] = (
    Check("token", "token valid (profile()), user_id matches", _ph(MORNING, PRESTART, CUTOVER),
          Effect(FAIL, "HALT(TOKEN)", _halt("TOKEN"), _A.CRITICAL), _check_token),
    Check("ntp", "NTP synced", _ph(MORNING, PRESTART, CUTOVER),
          Effect(FAIL, "HALT(CLOCK_UNSYNCED)", _halt("CLOCK_UNSYNCED"), _A.WARN), _check_ntp),
    Check("calendar", "calendar covers today and the next 30 days", _ALL,
          Effect(WARN, "WARN (beyond coverage = no trading)", alert=_A.WARN), _check_calendar),
    Check("instruments", "today's instruments dump is sane", _ph(MORNING, PRESTART, CUTOVER),
          Effect(FAIL, "HALT(NO_INSTRUMENTS)", _halt("NO_INSTRUMENTS"), _A.CRITICAL), _check_instruments),
    Check("ban_list", "today's ban list loaded", _ph(MORNING, PRESTART),
          Effect(WARN, "stocks ineligible (G08)", alert=_A.WARN), _check_ban_list),
    Check("prev_bars", "previous-session bars for >= 90% of the universe", _ph(MORNING, PRESTART),
          Effect(WARN, "WARN (missing symbols ineligible)", alert=_A.WARN), _check_prev_bars),
    Check("settings", "settings within LIMITS", _ALL,
          Effect(FAIL, "HALT(SETTINGS_INVALID)", _halt("SETTINGS_INVALID"), _A.CRITICAL), _check_settings),
    Check("db", "DB integrity_check; schema version", _ALL,
          Effect(FAIL, "exit 78", alert=_A.CRITICAL, exit_code=78), _check_db),
    Check("resources", "disk >= 2 GB; memory >= 300 MB", _ALL,
          Effect(WARN, "WARN", alert=_A.WARN), _check_resources),
    Check("v1_disabled", "v1 units masked and inactive; no crontab line mentioning agentic_trader", _ALL,
          Effect(FAIL, "HALT(V1_ACTIVE, GLOBAL, AUTO) + CRITICAL", _halt("V1_ACTIVE"), _A.CRITICAL),
          _check_v1_disabled),
    # morning too: a shadowing file dropped into the WorkingDirectory is found at 08:40, not by the restart it breaks
    Check("code_clean", "cwd clean (no module in agentic_trader/ that shadows an import); KiteConnect unpatched in "
          "a fresh interpreter", _ph(MORNING, PRESTART, DEPLOY, CUTOVER),
          Effect(FAIL, "exit 78", alert=_A.CRITICAL, exit_code=78), _check_code_clean),
    Check("env_files", "dash.env has no ZERODHA_*; env file permissions correct", _ALL,
          Effect(FAIL, "CRITICAL", alert=_A.CRITICAL), _check_env_files),
    Check("broker_flat", "broker: no non-bot NFO positions; no open orders", _ph(MORNING, CUTOVER),
          Effect(FAIL, "HALT(UNKNOWN_POSITION)", _halt("UNKNOWN_POSITION", "ACCOUNT:LIVE", HaltClears.ACK),
                 _A.CRITICAL), _check_broker_flat),
    Check("egress_ip", "egress IP == REGISTERED_EGRESS_IP (if set)", _ph(MORNING, PRESTART, CUTOVER),
          Effect(FAIL, "LIVE_DISABLED_TODAY", _halt("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE", HaltClears.NEXT_SESSION),
                 _A.CRITICAL), _check_egress_ip),
    Check("live_ready", "LIVE: margins >= LIVE_MIN_MARGIN_RS; LIVE strategies hold CERTIFIED trials",
          _ph(MORNING, PRESTART),
          Effect(FAIL, "LIVE_DISABLED_TODAY / demote",
                 _halt("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE", HaltClears.NEXT_SESSION), _A.CRITICAL),
          _check_live_ready),
    Check("rates", "newest RateSet younger than 180 days", _ph(MORNING, DEPLOY),
          Effect(WARN, "WARN", alert=_A.WARN), _check_rates),
    Check("prev_session", "previous session closed clean (flat, EOD done)", _ph(MORNING, PRESTART),
          Effect(FAIL, "CRITICAL", alert=_A.CRITICAL), _check_prev_session),
    Check("notify", "Telegram test and healthchecks ping", _ph(MORNING, CUTOVER),
          Effect(WARN, "WARN", alert=_A.WARN), _check_notify),
)


_LAPTOP = _ph(LAPTOP)


def _laptop_row(name: str, **changes: Any) -> Check:
    """A §13.7 row run in the laptop phase, as it is or with `changes`."""
    return replace(next(c for c in CHECKS if c.name == name), phases=_LAPTOP, **changes)


# A laptop FAIL halts entries and never exits: the host rows and the (elsewhere exit-78) DB row raise HALT(INVARIANT),
# which an operator clears with `titan2 ack-halt` once fixed.
_UNFIT = Effect(FAIL, "HALT(INVARIANT)", _halt("INVARIANT", clears=HaltClears.ACK), _A.CRITICAL)
LAPTOP_CHECKS: tuple[Check, ...] = (
    Check("python", "Python >= 3.10; kiteconnect, pyotp, requests and flask importable", _LAPTOP, _UNFIT,
          _check_python),
    Check("env_file", "env file present with the Kite keys (values never shown)", _LAPTOP,
          Effect(FAIL, "HALT(TOKEN)", _halt("TOKEN"), _A.CRITICAL), _check_env_file),
    _laptop_row("token"),
    Check("clock", "titan2's clock within 1 s of internet time, the reading's error counted (entries stop beyond)",
          _LAPTOP,
          Effect(FAIL, "HALT(CLOCK_UNSYNCED)", _halt("CLOCK_UNSYNCED"), _A.WARN), _check_clock),
    Check("network", "IPv4 route to api.kite.trade without packet loss (WARN only)", _LAPTOP,
          Effect(WARN, "WARN", alert=_A.WARN), _check_network),
    _laptop_row("calendar"),
    _laptop_row("instruments"),
    Check("disk", "disk >= 2 GB free", _LAPTOP, _UNFIT, _check_disk),
    _laptop_row("db", effect=_UNFIT),
    _laptop_row("settings"),
    Check("power", "on AC power (WARN only)", _LAPTOP, Effect(WARN, "WARN", alert=_A.WARN), _check_power),
    Check("live_disabled", "LIVE disabled: the laptop profile is PAPER only", _LAPTOP,
          Effect(FAIL, "LIVE_DISABLED_TODAY", _halt("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE", HaltClears.NEXT_SESSION),
                 _A.CRITICAL), _check_live_off),
)


# ---------------------------------------------------------------------------------------------------------------
# Running a phase
# ---------------------------------------------------------------------------------------------------------------
_SEVERITY_RANK = {alerts.INFO: 0, alerts.WARN: 1, alerts.CRITICAL: 2}


@dataclass(frozen=True, slots=True)
class Report:
    phase: str
    started: datetime
    results: tuple[CheckResult, ...]

    def result(self, name: str) -> CheckResult | None:
        return next((r for r in self.results if r.name == name), None)

    def with_status(self, *statuses: str) -> list[CheckResult]:
        return [r for r in self.results if r.status in statuses]

    @property
    def ok(self) -> bool:
        return not self.with_status(FAIL)

    @property
    def fatal(self) -> bool:
        """A failed exit-78 row: unsafe code on the path, or a damaged DB."""
        return any(r.status == FAIL and r.effect is not None and r.effect.exit_code == 78 for r in self.results)

    @property
    def exit_code(self) -> int:
        """CLI exit code: 0 ok (WARNs allowed), 2 any FAIL."""
        return 0 if self.ok else 2

    def halts(self) -> list[HaltSpec]:
        """The entry halts a prestart run raises: one per (kind, scope), as the halts table allows only one active
        row per pair; rows sharing a halt (egress IP and LIVE readiness) have their reasons joined."""
        merged: dict[tuple[str, str], tuple[HaltClears, list[str]]] = {}
        for r in self.results:
            if r.effect is not None and r.effect.halt is not None and r.data.get("halt", True):
                kind, scope, clears = r.effect.halt
                merged.setdefault((kind, scope), (clears, []))[1].append(f"{r.name}: {r.detail}")
        return [HaltSpec(kind, scope, clears, f"selfcheck {self.phase}: {'; '.join(reasons)}"[:HALT_REASON_MAX])
                for (kind, scope), (clears, reasons) in merged.items()]

    def alert_commands(self) -> list[Alert]:
        """One events.Alert per failed row, for the runtime to dispatch to the alerts thread."""
        return [Alert(r.effect.alert, f"SELFCHECK_{r.name.upper()}",
                      f"self-check {self.phase} {r.status} {r.title}: {r.detail}", f"selfcheck:{r.name}")
                for r in self.results if r.effect is not None and r.effect.alert]

    def demotions(self) -> list[str]:
        """Strategies to demote to PAPER (LIVE without a CERTIFIED trial for their current version)."""
        return [s for r in self.results if r.effect is not None for s in r.data.get("demote", ())]

    def severity(self) -> str:
        sev = alerts.INFO
        for r in self.results:
            if r.effect is not None and r.effect.alert and _SEVERITY_RANK[r.effect.alert] > _SEVERITY_RANK[sev]:
                sev = r.effect.alert
        return sev

    def summary(self, verbose: bool = False) -> str:
        n = {s: len(self.with_status(s)) for s in (PASS, WARN, FAIL, SKIP)}
        head = (f"titan2 self-check {self.phase} {self.started:%Y-%m-%d %H:%M}: {'PASS' if self.ok else 'FAIL'} "
                f"({n[FAIL]} fail, {n[WARN]} warn, {n[PASS]} pass, {n[SKIP]} skip)")
        shown = self.results if verbose else self.with_status(FAIL, WARN)
        return "\n".join([head] + [f"{r.status} {r.name}: {r.detail}" for r in shown])

    def to_json(self) -> dict[str, Any]:
        return {"phase": self.phase, "started": iso(self.started), "ok": self.ok, "fatal": self.fatal,
                "exit_code": self.exit_code, "demote": self.demotions(),
                "halts": [{"kind": h.kind, "scope": h.scope, "clears": h.clears.value, "reason": h.reason}
                          for h in self.halts()],
                "results": [{"name": r.name, "title": r.title, "status": r.status, "detail": r.detail,
                             "effect": r.effect.note if r.effect else None, "data": dict(r.data)}
                            for r in self.results]}


def _run_one(check: Check, ctx: Context) -> CheckResult:
    try:
        out = check.fn(ctx)
        ok, detail = out[0], str(out[1])
        data = dict(out[2]) if len(out) > 2 else {}
    except Exception as exc:                             # fail closed: unverifiable means failed
        ok, detail, data = False, f"could not check: {type(exc).__name__}: {exc}", {}
    status = SKIP if ok is None else PASS if ok else check.effect.status
    result = CheckResult(check.name, check.title, status, ctx.scrub(detail)[:DETAIL_MAX],
                         check.effect if status in (FAIL, WARN) else None, data)
    level = logging.ERROR if status == FAIL else logging.WARNING if status == WARN else logging.INFO
    log.log(level, "selfcheck %s %s %s: %s", ctx.phase, status, check.name, result.detail)
    return result


def run_phase(phase: str, ctx: Context | None = None) -> Report:
    """Run every row that belongs to the phase (the matrix's, or LAPTOP_CHECKS), in table order."""
    if phase not in ALL_PHASES:
        raise ValueError(f"unknown self-check phase {phase!r}; one of {', '.join(ALL_PHASES)}")
    ctx = ctx if ctx is not None else Context(phase)
    if ctx.phase != phase:
        raise ValueError(f"context was built for phase {ctx.phase!r}, not {phase!r}")
    try:
        results = tuple(_run_one(c, ctx) for c in CHECKS + LAPTOP_CHECKS if phase in c.phases)
    finally:
        ctx.close()
    return Report(phase, ctx.now, results)


def _write_report(directory: Path, report: Report) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"selfcheck-{report.started:%Y%m%d-%H%M%S}-{report.phase}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def cli(phase: str, *, db_copy: str | Path | None = None, out: Callable[[str], Any] = print, **context: Any) -> int:
    """`titan2 selfcheck --phase P [--db-copy PATH]` (and `titan2 laptop check`, phase LAPTOP): print every row,
    write reports/selfcheck-*.json, send the morning summary to Telegram. Returns the exit code (0 ok, 2 any FAIL
    or a bad phase)."""
    if phase not in ALL_PHASES:
        out(f"unknown phase {phase!r}; one of {', '.join(ALL_PHASES)}")
        return 2
    ctx = Context(phase, db_copy=db_copy, **context)
    report = run_phase(phase, ctx)
    out(report.summary(verbose=True))
    try:
        log.info("selfcheck report written to %s", _write_report(ctx.var_dir / "reports", report))
    except OSError as exc:
        log.warning("selfcheck report not written: %s", exc)
    if phase == MORNING:
        alerts.send_now(report.severity(), "SELFCHECK_MORNING", report.summary(), env=ctx.env)
    return report.exit_code
