"""The laptop self-check phase (titan2/selfcheck.py LAPTOP_CHECKS): the Windows laptop profile's rows (Python and its
packages, the env file, the token, the clock against internet time, the calendar, the instruments dump, disk, the
DB, settings, AC power, LIVE off), their effects (every FAIL an entry halt, never an exit) and the laptop paths.

Offline and deterministic: titan2.laptop (ctypes, NTP, HTTP) is a scripted fake (or the real module with those calls
replaced), the host a fake System and Kite a fake profile(); no real env file, session or secret is ever read.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import titan2
from titan2 import selfcheck, settings as settings_mod, store as store_mod, token
from titan2.clock import IST
from titan2.core import risk
from titan2.core.events import SelfcheckResult
from titan2.laptop import ClockOffset          # the real reading type (the fake module below hands it out)
from titan2.model import HaltClears
from titan2.net import Probe

GB = 1024 ** 3

# Fake credentials: distinctive strings so a leak anywhere is easy to spot.
API_KEY, USER = "kiteapikey01", "AB1234"
API_SECRET, PASSWORD, TOTP_SECRET = "ApiSecretValue-77f1", "Password-Hunter2", "TOTPSECRETBASE32XYZ"
TG_TOKEN, ACCESS_TOKEN = "123456:TelegramTokenValue-q9", "AcTok9876543210fedcbaXYZ"
SECRETS = (API_SECRET, PASSWORD, TOTP_SECRET, TG_TOKEN, ACCESS_TOKEN)
NOW = datetime(2026, 10, 12, 8, 50, tzinfo=IST)             # a Monday, a trading day
LATE = datetime(2026, 12, 14, 8, 50, tzinfo=IST)            # the calendar ends 2026-12-31: < 30 days ahead
ROWS = ["python", "env_file", "token", "clock", "network", "calendar", "instruments", "disk", "db", "settings",
        "power", "live_disabled"]
# Copied independently of the code: row -> (status of a failure, the entry halt it raises, alert severity).
EXPECTED = {
    "python":        ("FAIL", ("INVARIANT", "GLOBAL", HaltClears.ACK), "CRITICAL"),
    "env_file":      ("FAIL", ("TOKEN", "GLOBAL", HaltClears.AUTO), "CRITICAL"),
    "token":         ("FAIL", ("TOKEN", "GLOBAL", HaltClears.AUTO), "CRITICAL"),
    "clock":         ("FAIL", ("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO), "WARN"),
    "network":       ("WARN", None, "WARN"),
    "calendar":      ("WARN", None, "WARN"),
    "instruments":   ("FAIL", ("NO_INSTRUMENTS", "GLOBAL", HaltClears.AUTO), "CRITICAL"),
    "disk":          ("FAIL", ("INVARIANT", "GLOBAL", HaltClears.ACK), "CRITICAL"),
    "db":            ("FAIL", ("INVARIANT", "GLOBAL", HaltClears.ACK), "CRITICAL"),
    "settings":      ("FAIL", ("SETTINGS_INVALID", "GLOBAL", HaltClears.AUTO), "CRITICAL"),
    "power":         ("WARN", None, "WARN"),
    "live_disabled": ("FAIL", ("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE", HaltClears.NEXT_SESSION), "CRITICAL"),
}
ENV_FILE_VALUES = {"ZERODHA_API_KEY": API_KEY, "ZERODHA_API_SECRET": API_SECRET, "ZERODHA_USER_ID": USER,
                   "ZERODHA_PASSWORD": PASSWORD, "ZERODHA_TOTP_SECRET": TOTP_SECRET, "TELEGRAM_BOT_TOKEN": TG_TOKEN,
                   "TELEGRAM_CHAT_ID": "4242"}
LAPTOP_ENV = {"TITAN2_PROFILE": "laptop", "ZERODHA_API_KEY": API_KEY, "ZERODHA_USER_ID": USER}  # no secret in it


class FakeLaptop:
    """The titan2.laptop surface the rows use: is_laptop(env), clock_offset_s() (seconds to add to this clock for
    internet time, a bare number or a ClockOffset carrying its error; None when no source answered), power_status()
    ({"on_ac", "battery_pct"}), network_probe() (a titan2.net.Probe) and LIVE_REFUSED_REASONS.
    No ctypes and no network: each answer is scripted (an exception instance is raised instead)."""

    LIVE_REFUSED_REASONS = ("SEBI requires a registered static IP for API orders, which a laptop does not have",
                            "a laptop has no backstop if it sleeps, loses Wi-Fi or reboots")

    def __init__(self) -> None:
        self.offset: object = 0.12
        self.power: object = {"on_ac": True, "battery_pct": 100}
        self.net: object = Probe("api.kite.trade", 8, 8, 0, 23.0, 31.0)
        self.envs: list = []

    def is_laptop(self, env) -> bool:
        self.envs.append(env)
        return str(env.get("TITAN2_PROFILE", "")).strip().lower() == "laptop"

    def clock_offset_s(self):
        if isinstance(self.offset, BaseException):
            raise self.offset
        return self.offset

    def power_status(self):
        if isinstance(self.power, BaseException):
            raise self.power
        return self.power

    def network_probe(self):
        if isinstance(self.net, BaseException):
            raise self.net
        return self.net


@pytest.fixture
def lap(monkeypatch) -> FakeLaptop:
    fake = FakeLaptop()
    monkeypatch.setitem(sys.modules, "titan2.laptop", fake)
    monkeypatch.setattr(titan2, "laptop", fake, raising=False)
    return fake


def imports_out(version: str = "3.12.7", missing: tuple[str, ...] = ()) -> str:
    """What IMPORT_CHECK prints."""
    lines = [selfcheck.PY_TAG + version] + [selfcheck.NO_IMPORT_TAG + m for m in missing] + [selfcheck.IMPORTS_DONE]
    return "\n".join(lines) + "\n"


class LaptopSystem:
    """The host as the laptop phase sees it: one fresh interpreter (the import check) and the disk. No systemctl,
    timedatectl, crontab, users or egress IP: any other probe is an AttributeError, which fails its row."""

    def __init__(self, out: str | None = None, rc: int = 0, free: int = 50 * GB) -> None:
        self.out = imports_out() if out is None else out
        self.rc, self.free = rc, free
        self.calls: list[tuple] = []

    def run(self, cmd, timeout, cwd=None):
        self.calls.append((tuple(cmd), timeout, cwd))
        assert list(cmd[1:]) == ["-c", selfcheck.IMPORT_CHECK], cmd
        return self.rc, self.out

    def disk_free(self, path):
        return self.free


class FakeKite:
    def profile(self):
        return {"user_id": USER, "user_name": "Test User"}


class FakeInstruments:
    def __repr__(self) -> str:
        return "InstrumentStore(session=2026-10-12, contracts=41250, stocks=180, indices=2)"


def _raise(exc: BaseException):
    def provider():
        raise exc
    return provider


def write_env(path: Path, drop: tuple[str, ...] = (), extra: dict | None = None, encoding: str = "utf-8") -> Path:
    values = {k: v for k, v in ENV_FILE_VALUES.items() if k not in drop}
    values.update(extra or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# titan2 laptop env\n" + "".join(f"{k}={v}\n" for k, v in values.items()), encoding=encoding)
    return path


def make_db(var: Path, user_version: int | None = None) -> Path:
    path = var / "db" / store_mod.DB_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    store_mod.Store(path).close()
    if user_version is not None:
        con = sqlite3.connect(path)
        con.execute(f"PRAGMA user_version={int(user_version)}")
        con.commit()
        con.close()
    return path


def laptop_kw(base: Path, **over) -> dict:
    """A healthy laptop under base/: every row passes."""
    var = Path(over.pop("var_dir", base / "var"))
    if not (var / "db" / store_mod.DB_FILENAME).exists():
        make_db(var)
    kw = dict(env=dict(LAPTOP_ENV), env_file=write_env(base / "appdata" / "titan2" / "titan2.env"), now=NOW,
              var_dir=var, app_dir=base, system=LaptopSystem(), python="python3",
              session=token.new_session(ACCESS_TOKEN, USER, NOW.replace(hour=8, minute=30)), kite=FakeKite(),
              instruments=FakeInstruments())
    kw.update(over)
    return kw


def run(base: Path, **over) -> selfcheck.Report:
    return selfcheck.run_phase(selfcheck.LAPTOP, selfcheck.Context(selfcheck.LAPTOP, **laptop_kw(base, **over)))


# ---------------------------------------------------------------------------------------------------------------
# The table and the phase as a whole
# ---------------------------------------------------------------------------------------------------------------
def test_laptop_table_rows_and_effects():
    assert [c.name for c in selfcheck.LAPTOP_CHECKS] == ROWS
    assert selfcheck.PHASES == ("morning", "prestart", "deploy", "cutover")        # the matrix phases are unchanged
    assert selfcheck.ALL_PHASES == selfcheck.PHASES + ("laptop",)
    assert [c.name for c in selfcheck.CHECKS if selfcheck.LAPTOP in c.phases] == []
    matrix = {c.name: c for c in selfcheck.CHECKS}
    for c in selfcheck.LAPTOP_CHECKS:
        status, halt, alert = EXPECTED[c.name]
        e = c.effect
        assert c.phases == frozenset({selfcheck.LAPTOP}), c.name
        assert (e.status, e.halt, e.alert, e.exit_code) == (status, halt, alert, None), c.name    # never an exit
        assert e.note, c.name
        if halt is not None:                    # a halt the core can raise as it is: same kind, scope, clear rule
            assert risk.make_halt(halt[0], halt[1], "x").clears == halt[2], c.name
        if c.name in matrix:                    # token, calendar, instruments, db, settings: the matrix's checks
            assert (c.fn, c.title) == (matrix[c.name].fn, matrix[c.name].title), c.name
    assert matrix["db"].effect.exit_code == 78                     # elsewhere the DB row still exits 78


def test_laptop_healthy_every_row_passes(tmp_path, lap):
    kw = laptop_kw(tmp_path)
    report = selfcheck.run_phase(selfcheck.LAPTOP, selfcheck.Context(selfcheck.LAPTOP, **kw))
    assert [r.name for r in report.results] == ROWS
    assert {r.name: r.status for r in report.results if r.status != "PASS"} == {}, report.summary(True)
    assert report.ok and report.exit_code == 0 and not report.fatal
    assert report.halts() == [] and report.alert_commands() == [] and report.demotions() == []
    assert report.severity() == "INFO"
    json.dumps(report.to_json())
    # the only host probe is the import check, in the bot's interpreter, from its working directory
    assert [(c[0][0], c[2]) for c in kw["system"].calls] == [("python3", tmp_path)]
    assert kw["system"].calls[0][1] >= 60
    assert lap.envs and all(e["TITAN2_PROFILE"] == "laptop" for e in lap.envs)   # judged on the session's env
    detail = {r.name: r.detail for r in report.results}
    assert detail["python"] == "Python 3.12.7; kiteconnect, pyotp, requests, flask import"
    assert detail["clock"] == "this clock is 0.120 s behind internet time"
    assert detail["disk"] == f"disk 50.0 GB free at {kw['var_dir']}"
    assert detail["power"] == "on AC power (battery 100%)"
    assert detail["network"] == "IPv4 to api.kite.trade: 8/8 connects, median 23 ms (worst 31 ms)"
    assert detail["token"].startswith(f"user {USER}; session ")
    assert "refuses LIVE (SEBI requires a registered static IP" in detail["live_disabled"]


def test_laptop_failures_halt_entries_and_never_exit(tmp_path, lap):
    lap.offset, lap.power = 1.5, {"on_ac": False, "battery_pct": 41}   # beyond 1 s: entries stop
    lap.net = Probe("api.kite.trade", 8, 5, 1, 120.0, 1300.0, "TimeoutError: timed out")
    var = tmp_path / "var"
    make_db(var, user_version=99)                                  # a schema newer than this release
    (var / "config").mkdir(parents=True)
    (var / "config" / "settings.json").write_text('{"MAX_LOTS": 5}', encoding="utf-8")       # looser than LIMITS
    stale = token.new_session(ACCESS_TOKEN, USER, LATE.replace(hour=8, minute=30) - timedelta(days=1))
    system = LaptopSystem(imports_out("3.9.18", ("flask: ModuleNotFoundError: No module named 'flask'",)), free=GB)
    kw = laptop_kw(tmp_path, now=LATE, var_dir=var, env={"ZERODHA_API_KEY": API_KEY, "ZERODHA_USER_ID": USER},
                   env_file=tmp_path / "nowhere" / "titan2.env", system=system, session=stale,
                   instruments=_raise(RuntimeError("InstrumentsError: NIFTY lists CE/PE for 1 expiry")))
    report = selfcheck.run_phase(selfcheck.LAPTOP, selfcheck.Context(selfcheck.LAPTOP, **kw))
    assert [r.name for r in report.results] == ROWS
    for r in report.results:
        assert r.status == EXPECTED[r.name][0], (r.name, r.detail)
        assert r.effect is not None and r.effect.note
    assert not report.ok and report.exit_code == 2
    assert not report.fatal                                        # no laptop row exits: the DB row halts instead
    halts = {(h.kind, h.scope, h.clears) for h in report.halts()}
    assert halts == {v[1] for v in EXPECTED.values() if v[1] is not None}
    assert len(report.halts()) == len(halts)                       # one halt per (kind, scope), reasons joined
    unfit = next(h for h in report.halts() if h.kind == "INVARIANT")
    assert unfit.reason.startswith("selfcheck laptop: python: Python 3.9.18 is older than 3.10")
    assert "disk: " in unfit.reason and len(unfit.reason) <= selfcheck.HALT_REASON_MAX
    tok = next(h for h in report.halts() if h.kind == "TOKEN")                # env_file and token share it
    assert tok.reason.startswith("selfcheck laptop: env_file: ") and len(tok.reason) <= selfcheck.HALT_REASON_MAX
    assert {(a.code, a.severity) for a in report.alert_commands()} == {
        (f"SELFCHECK_{n.upper()}", EXPECTED[n][2]) for n in ROWS}
    assert report.severity() == "CRITICAL"
    # the core takes them as they are: each one it can raise, the report travelling in a SelfcheckResult
    for h in report.halts():
        assert risk.make_halt(h.kind, h.scope, h.reason).clears == h.clears, h
    done = SelfcheckResult(selfcheck.LAPTOP, report.ok, report)
    assert done.report.halts() == report.halts() and not done.ok and done.report.demotions() == []
    json.dumps(report.to_json())


# ---------------------------------------------------------------------------------------------------------------
# Row by row
# ---------------------------------------------------------------------------------------------------------------
def test_laptop_python_row(tmp_path, lap):
    def row(out: str, rc: int = 0) -> selfcheck.CheckResult:
        return run(tmp_path, system=LaptopSystem(out, rc)).result("python")

    r = row(imports_out("3.10.0"))
    assert r.status == "PASS" and r.detail.startswith("Python 3.10.0;")
    r = row(imports_out(missing=("pyotp: ModuleNotFoundError: No module named 'pyotp'",)))
    assert r.status == "FAIL" and "cannot import pyotp: ModuleNotFoundError: No module named 'pyotp'" in r.detail
    assert r.detail.startswith("python3 cannot import")             # names the interpreter: often the wrong venv
    assert "ops\\windows\\setup.ps1" in r.detail and "python -m pip install -r requirements.lock" in r.detail
    assert r.effect.halt == ("INVARIANT", "GLOBAL", HaltClears.ACK)
    r = row(imports_out("3.9.18"))
    assert r.status == "FAIL" and r.detail == "Python 3.9.18 is older than 3.10"
    r = row(imports_out("garbled"))
    assert r.status == "FAIL" and "older than 3.10" in r.detail               # an unreadable version fails closed
    r = row("Traceback (most recent call last):\nSyntaxError: invalid syntax\n", rc=1)
    assert r.status == "FAIL"
    assert r.detail == "python3 could not run the import check (rc 1): SyntaxError: invalid syntax"
    r = row(imports_out().replace(selfcheck.IMPORTS_DONE, ""))               # the interpreter died part-way
    assert r.status == "FAIL" and "could not run the import check (rc 0)" in r.detail
    assert row("python3: timed out after 180 s", rc=124).status == "FAIL"


def test_laptop_import_check_in_a_real_interpreter(tmp_path, monkeypatch, lap):
    stand = tmp_path / "stand"            # stand-ins for the four packages: offline, quick, and the same everywhere
    for name in selfcheck.LAPTOP_IMPORTS:
        (stand / name).mkdir(parents=True)
        (stand / name / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(stand))
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    host = selfcheck.System()
    version = ".".join(str(n) for n in sys.version_info[:3])
    rc, out = host.run([sys.executable, "-c", selfcheck.IMPORT_CHECK], 120, cwd=stand)
    lines = out.splitlines()
    assert rc == 0 and selfcheck.PY_TAG + version in lines and selfcheck.IMPORTS_DONE in lines, out
    assert not [ln for ln in lines if ln.startswith(selfcheck.NO_IMPORT_TAG)], out
    r = run(tmp_path / "ok", system=host, python=sys.executable, app_dir=stand).result("python")
    assert r.status == "PASS" and r.detail == f"Python {version}; kiteconnect, pyotp, requests, flask import"
    # a package that fails to import is named, with its error on one ASCII line (a Windows pipe is not UTF-8)
    (stand / "flask" / "__init__.py").write_text("raise ImportError('broken flask\\nsecond line \\u0928')\n",
                                                 encoding="utf-8")
    rc, out = host.run([sys.executable, "-c", selfcheck.IMPORT_CHECK], 120, cwd=stand)
    assert rc == 0 and selfcheck.NO_IMPORT_TAG + "flask: ImportError: broken flask second line ?" in out.splitlines()
    r = run(tmp_path / "bad", system=host, python=sys.executable, app_dir=stand).result("python")
    assert r.status == "FAIL" and "cannot import flask: ImportError: broken flask second line ?" in r.detail


def test_laptop_env_file_row(tmp_path, lap):
    def row(path: Path) -> selfcheck.CheckResult:
        return run(tmp_path, env_file=path).result("env_file")

    good = write_env(tmp_path / "e1" / "titan2.env")
    r = row(good)
    assert r.status == "PASS" and r.detail == (f"{good} sets ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID, "
                                                "ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET, TELEGRAM_BOT_TOKEN, "
                                                "TELEGRAM_CHAT_ID")
    # no login keys: still fine (the token step falls back to the manual request-token flow); Telegram is optional
    r = row(write_env(tmp_path / "e2" / "titan2.env",
                      drop=("ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET", "TELEGRAM_BOT_TOKEN")))
    assert r.status == "PASS" and "manual request-token flow" in r.detail and "no Telegram alerts" in r.detail
    # a key the bot or the token exchange needs is missing, or empty
    r = row(write_env(tmp_path / "e3" / "titan2.env", drop=("ZERODHA_API_SECRET",), extra={"ZERODHA_USER_ID": ""}))
    assert r.status == "FAIL" and "lacks ZERODHA_API_SECRET, ZERODHA_USER_ID" in r.detail
    assert r.effect.halt == ("TOKEN", "GLOBAL", HaltClears.AUTO)
    missing = tmp_path / "e4" / "titan2.env"
    r = row(missing)
    assert r.status == "FAIL" and r.detail.startswith(f"{missing} is missing: create it") and \
        "ZERODHA_TOTP_SECRET" in r.detail and "TITAN2_ENV_FILE" in r.detail
    # PowerShell 5's default UTF-16 is unreadable for the token step too; Notepad's UTF-8 with a BOM is fine
    r = row(write_env(tmp_path / "e5" / "titan2.env", encoding="utf-16"))
    assert r.status == "FAIL" and "is not UTF-8 text" in r.detail
    assert row(write_env(tmp_path / "e6" / "titan2.env", encoding="utf-8-sig")).status == "PASS"
    # v1 leftovers are named as ignored, never shown
    r = row(write_env(tmp_path / "e7" / "titan2.env", extra={"TRADING_MODE": "LIVE",
                                                             "ZERODHA_ACCESS_TOKEN": "OldTokValue123"}))
    assert r.status == "PASS" and "TRADING_MODE, ZERODHA_ACCESS_TOKEN ignored" in r.detail
    assert "OldTokValue123" not in r.detail and "=LIVE" not in r.detail
    folder = tmp_path / "e8" / "titan2.env"                         # a folder where the file should be
    folder.mkdir(parents=True)
    r = row(folder)
    assert r.status == "FAIL" and r.detail.startswith(f"cannot read {folder}: ")


def test_laptop_token_row(tmp_path, lap):
    r = run(tmp_path, session=token.new_session(ACCESS_TOKEN, USER, NOW - timedelta(days=1))).result("token")
    assert r.status == "FAIL" and "not today after 06:00 IST" in r.detail
    assert r.effect.halt == ("TOKEN", "GLOBAL", HaltClears.AUTO)
    # the session file under the laptop var dir (as on Linux): missing, then written by the token step
    var = tmp_path / "v2"
    r = run(tmp_path, var_dir=var, session=None).result("token")
    assert r.status == "FAIL" and "no session file at" in r.detail
    token.write_session(token.new_session(ACCESS_TOKEN, USER, NOW.replace(hour=8, minute=30)), token.session_path(var))
    assert run(tmp_path, var_dir=var, session=None).result("token").status == "PASS"
    wrong = types.SimpleNamespace(profile=lambda: {"user_id": "ZZ9999"})
    assert run(tmp_path, kite=wrong).result("token").status == "FAIL"


def test_laptop_clock_row(tmp_path, lap):
    """The fixed interface: entries stop when |offset| > 1 s. No wider laptop tier, no WARN-only drift row."""
    def row(offset: object) -> selfcheck.CheckResult:
        lap.offset = offset
        return run(tmp_path).result("clock")

    r = row(0.3)                                       # positive: this clock is behind internet time
    assert r.status == "PASS" and r.detail == "this clock is 0.300 s behind internet time"
    r = row(-1.0)                                      # the 1 s limit itself passes
    assert r.status == "PASS" and r.detail == "this clock is 1.000 s ahead of internet time"
    assert row(0).status == "PASS"
    r = row(1.25)                                      # this laptop on 2026-10-11: entries stay off until it syncs
    assert r.status == "FAIL" and r.detail.startswith("this clock is 1.250 s behind internet time: more than 1 s off, "
                                                      "so entries stay off. titan2 corrects its own clock as soon as")
    assert "Sync now" in r.detail and "admin rights needed" in r.detail      # the Windows fix stays as the other way
    assert "docs/LAPTOP.md section 5" in r.detail and r.effect.halt == ("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO)
    for off in (1.01, -1.25, 3.0, 4.99, -5.0, 6.0):     # the review's 3 s: a FAIL (no 5 s tier any more)
        r = row(off)
        assert r.status == "FAIL" and "more than 1 s off, so entries stay off" in r.detail, off
        assert "continues" not in r.detail, off
    assert not [c for c in selfcheck.LAPTOP_CHECKS if c.name == "clock_drift"]
    assert not hasattr(selfcheck, "LAPTOP_MAX_CLOCK_OFFSET_S") and selfcheck.MAX_CLOCK_OFFSET_S == 1.0
    # no time source answered (NTP and the HTTP Date both unreachable): unknown blocks entries too
    r = row(None)
    assert r.status == "FAIL" and r.detail.startswith("clock offset unknown: no internet time source answered")
    # the probe failing outright, or a nonsense answer: fail closed
    r = row(OSError("NTP and HTTP Date both failed"))
    assert r.status == "FAIL" and "NTP and HTTP Date both failed" in r.detail
    for nonsense in (float("nan"), float("inf"), True, "0.1", (0.1, "time.windows.com"),
                     ClockOffset(0.1, float("nan")), ClockOffset(0.1, -0.2)):
        r = row(nonsense)
        assert r.status == "FAIL" and r.detail.startswith("could not check: ValueError: clock_offset_s() returned")


def test_laptop_clock_row_counts_the_readings_own_error(tmp_path, lap):
    """The 1 s rule judges the worst case: the reading plus its own error. The whole-second HTTP Date (NTP blocked)
    is only good to 0.5 s plus half its round trip, so it passes a clock only when even that cannot exceed 1 s."""
    def row(offset: float, error: float, source: str = "the Date header of https://api.kite.trade"):
        lap.offset = ClockOffset(offset, error, source)
        return run(tmp_path).result("clock")

    r = row(0.4, 0.55)                                 # at most 0.95 s off
    assert r.status == "PASS" and r.detail == ("this clock is 0.400 s behind internet time (+-0.550 s, the Date header "
                                               "of https://api.kite.trade)")
    r = row(0.6, 0.55)                                 # perhaps 1.15 s off: not proven within 1 s
    assert r.status == "FAIL" and r.detail.startswith(
        "this clock is 0.600 s behind internet time (+-0.550 s, the Date header of https://api.kite.trade): possibly "
        "more than 1 s off, so entries stay off")
    assert r.effect.halt == ("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO)
    r = row(-1.4, 0.55)
    assert r.status == "FAIL" and ": more than 1 s off" in r.detail
    assert row(-0.95, 0.03, "NTP time.windows.com").status == "PASS"   # NTP: half its round trip counts too
    r = row(-0.98, 0.03, "NTP time.windows.com")
    assert r.status == "FAIL" and "(+-0.030 s, NTP time.windows.com): possibly more than 1 s off" in r.detail


def test_laptop_clock_probes_once_per_run(tmp_path, lap, monkeypatch):
    calls = []
    real = lap.clock_offset_s
    monkeypatch.setattr(lap, "clock_offset_s", lambda: calls.append(1) or real())
    lap.offset = 0.25
    report = run(tmp_path)
    assert len(calls) == 1 and report.result("clock").status == "PASS"     # one network probe per run


def test_laptop_disk_calendar_instruments_rows(tmp_path, lap):
    two = 2 * GB
    assert run(tmp_path, system=LaptopSystem(free=two)).result("disk").status == "PASS"    # the floor itself passes
    r = run(tmp_path, system=LaptopSystem(free=two - 1)).result("disk")
    assert r.status == "FAIL" and r.detail.endswith("(need >= 2 GB)")
    assert r.effect.halt == ("INVARIANT", "GLOBAL", HaltClears.ACK)
    # the calendar only warns (beyond its coverage nothing trades anyway)
    r = run(tmp_path, now=LATE, session=token.new_session(ACCESS_TOKEN, USER, LATE.replace(hour=8))).result("calendar")
    assert r.status == "WARN" and r.effect.halt is None and "not through 2027-01-13" in r.detail
    r = run(tmp_path, instruments=_raise(RuntimeError("InstrumentsError: no NIFTY rows"))).result("instruments")
    assert r.status == "FAIL" and "no NIFTY rows" in r.detail and r.effect.halt[0] == "NO_INSTRUMENTS"


def test_laptop_db_and_settings_rows(tmp_path, lap):
    # the DB: absent (first start) does not apply; newer or damaged halts entries, and never exits
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    report = selfcheck.run_phase(selfcheck.LAPTOP, selfcheck.Context(selfcheck.LAPTOP, **dict(
        laptop_kw(tmp_path), var_dir=fresh)))
    assert report.result("db").status == "SKIP" and "creates it at its first start" in report.result("db").detail
    assert run(tmp_path).result("db").detail.startswith("integrity ok; schema ")
    newer = tmp_path / "newer"
    make_db(newer, user_version=99)
    report = run(tmp_path, var_dir=newer)
    r = report.result("db")
    assert r.status == "FAIL" and "schema 99 is newer" in r.detail and r.effect.exit_code is None
    assert not report.fatal and [h.kind for h in report.halts()] == ["INVARIANT"]
    damaged = tmp_path / "damaged"
    (damaged / "db").mkdir(parents=True)
    (damaged / "db" / store_mod.DB_FILENAME).write_bytes(b"this is not an SQLite database" * 100)
    report = run(tmp_path, var_dir=damaged)
    assert report.result("db").status == "FAIL" and not report.fatal
    # settings: a file looser than LIMITS halts entries (SETTINGS_INVALID)
    bad = tmp_path / "badcfg"
    make_db(bad)
    (bad / "config").mkdir()
    (bad / "config" / "settings.json").write_text('{"MAX_LOTS": 5}', encoding="utf-8")
    r = run(tmp_path, var_dir=bad).result("settings")
    assert r.status == "FAIL" and "INVALID" in r.detail and r.effect.halt[0] == "SETTINGS_INVALID"
    assert run(tmp_path).result("settings").status == "PASS"


@pytest.mark.parametrize("reading, status, text", [
    ({"on_ac": True, "battery_pct": 100}, "PASS", "on AC power (battery 100%)"),
    ({"on_ac": True, "battery_pct": None}, "PASS", "on AC power"),                       # a desktop: no battery
    ({"on_ac": False, "battery_pct": 41}, "WARN", "on battery (battery 41%): plug in; a laptop that sleeps"),
    ({"on_ac": None, "battery_pct": None}, "WARN", "power source unknown: plug in"),    # not Windows / call failed
    ({"on_ac": 1, "battery_pct": 90}, "WARN", "power source unknown (battery 90%)"),    # only True counts as AC
    (OSError("GetSystemPowerStatus failed"), "WARN", "could not check: OSError: GetSystemPowerStatus failed"),
])
def test_laptop_power_row_only_warns(tmp_path, lap, reading, status, text):
    lap.power = reading
    report = run(tmp_path)
    r = report.result("power")
    assert r.status == status and r.detail.startswith(text), r.detail
    if status == "WARN":
        assert r.effect.halt is None and r.effect.alert == "WARN"
        assert report.ok and report.halts() == []                  # a battery never blocks entries


@pytest.mark.parametrize("probe, status, text", [
    (Probe("api.kite.trade", 8, 8, 0, 23.0, 31.0), "PASS", "IPv4 to api.kite.trade: 8/8 connects, median 23 ms"),
    (Probe("api.kite.trade", 8, 6, 1, 40.0, 1100.0, "TimeoutError: timed out"), "WARN",
     "IPv4 to api.kite.trade: 6/8 connects, median 40 ms (worst 1100 ms): 3 packet losses. Expect Kite timeouts"),
    (Probe("api.kite.trade", 8, 8, 0, 400.0, 600.0), "WARN", "IPv4 to api.kite.trade: 8/8 connects, median 400 ms "
     "(worst 600 ms): slow line"),
    (Probe("api.kite.trade", 8, 0, 0, None, None, "cannot resolve api.kite.trade: [Errno 11001] getaddrinfo failed"),
     "WARN", "IPv4 to api.kite.trade: 0/8 connects: cannot resolve api.kite.trade"),
    (OSError("no route"), "WARN", "could not check: OSError: no route"),
])
def test_laptop_network_row_only_warns(tmp_path, lap, probe, status, text):
    lap.net = probe
    report = run(tmp_path)
    r = report.result("network")
    assert r.status == status and r.detail.startswith(text), r.detail
    if status == "WARN":
        assert r.effect.halt is None and r.effect.alert == "WARN"
        assert report.ok and report.halts() == []                  # a lossy line costs refusals, never entries


def test_laptop_live_disabled_row(tmp_path, lap, monkeypatch):
    r = run(tmp_path).result("live_disabled")
    assert r.status == "PASS" and r.detail.endswith("LIVE-mode strategies run as PAPER")
    assert "a laptop has no backstop if it sleeps, loses Wi-Fi or reboots" in r.detail
    r = run(tmp_path, env=dict(LAPTOP_ENV, ALLOW_LIVE="1")).result("live_disabled")
    assert r.status == "PASS" and r.detail.endswith("ALLOW_LIVE=1 is ignored")
    # outside the laptop profile nothing here enforces PAPER: LIVE is disabled today instead (LIVE entries only)
    report = run(tmp_path, env=dict(LAPTOP_ENV, TITAN2_PROFILE=""))
    r = report.result("live_disabled")
    assert r.status == "FAIL" and "not the laptop profile" in r.detail
    assert [(h.kind, h.scope, h.clears) for h in report.halts()] == [
        ("LIVE_DISABLED_TODAY", "ACCOUNT:LIVE", HaltClears.NEXT_SESSION)]
    # titan2/laptop.py missing or broken: the rows that need it fail closed (power only warns)
    monkeypatch.setitem(sys.modules, "titan2.laptop", None)
    monkeypatch.delattr(titan2, "laptop", raising=False)
    report = run(tmp_path)
    assert {n: report.result(n).status for n in ("clock", "power", "live_disabled")} == {
        "clock": "FAIL", "power": "WARN", "live_disabled": "FAIL"}
    assert report.result("clock").detail.startswith("could not check: ")


def test_laptop_rows_with_the_real_laptop_module(tmp_path, monkeypatch):
    """The contract with titan2/laptop.py: the calls the rows make still fit its functions, and its answers (a
    ClockOffset or None; a PowerStatus) read as the fakes do. Its network and ctypes calls are replaced, its is_laptop
    is real."""
    real = importlib.import_module("titan2.laptop")
    inspect.signature(real.is_laptop).bind({})
    inspect.signature(real.clock_offset_s).bind()
    inspect.signature(real.power_status).bind()
    monkeypatch.setattr(real, "clock_offset_s", lambda timeout_s=2.0: real.ClockOffset(-0.4, 0.012,
                                                                                       "NTP time.windows.com"))
    monkeypatch.setattr(real, "power_status", lambda kernel32=None: real.PowerStatus(on_ac=False, battery_pct=41))
    monkeypatch.setattr(titan2, "laptop", real)
    report = run(tmp_path)
    assert report.result("clock").detail == ("this clock is 0.400 s ahead of internet time (+-0.012 s, NTP "
                                             "time.windows.com)")
    assert report.result("power").status == "WARN"
    assert report.result("power").detail.startswith("on battery (battery 41%)")
    live = report.result("live_disabled")                            # TITAN2_PROFILE=laptop in the env given
    assert live.status == "PASS" and all(reason in live.detail for reason in real.LIVE_REFUSED_REASONS)
    assert report.ok and report.halts() == []
    report = run(tmp_path, env=dict(LAPTOP_ENV, TITAN2_PROFILE="other"))
    assert report.result("live_disabled").status == "FAIL"


# ---------------------------------------------------------------------------------------------------------------
# Secrets, paths and the CLI
# ---------------------------------------------------------------------------------------------------------------
def test_laptop_never_prints_secrets(tmp_path, lap, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(selfcheck.alerts, "send_now", lambda *a, **k: pytest.fail("the laptop check sends nothing"))
    other_password = "OtherPassword-5150"                          # the env given overrides the file's password
    secrets = SECRETS + (other_password,)
    every = " ".join(secrets)
    # the secrets sit in the env file and the env given, and are put where a careless row could echo them
    lap.offset = OSError(f"NTP failed: {every}")
    lap.power = RuntimeError(f"power: {PASSWORD} {TOTP_SECRET}")
    env_file = write_env(tmp_path / "secret" / "titan2.env", extra={"TRADING_MODE": PASSWORD})
    system = LaptopSystem(imports_out(missing=(f"flask: ImportError: cannot load {TOTP_SECRET} {API_SECRET}",)))
    kw = laptop_kw(tmp_path, env_file=env_file, system=system, env=dict(LAPTOP_ENV, ZERODHA_PASSWORD=other_password),
                   instruments=_raise(RuntimeError(f"download failed for {ACCESS_TOKEN} / {TG_TOKEN}")),
                   kite=_raise(RuntimeError(f"TokenException: {ACCESS_TOKEN} rejected")))
    out: list[str] = []
    assert selfcheck.cli(selfcheck.LAPTOP, out=out.append, **kw) == 2
    report = selfcheck.run_phase(selfcheck.LAPTOP, selfcheck.Context(selfcheck.LAPTOP, **kw))
    texts = {
        "stdout": "\n".join(out),
        "report file": next((kw["var_dir"] / "reports").glob("selfcheck-*-laptop.json")).read_text(encoding="utf-8"),
        "json": json.dumps(report.to_json()),
        "halts": " ".join(h.reason for h in report.halts()),
        "alerts": " ".join(a.text for a in report.alert_commands()),
        "logs": caplog.text,
    }
    for where, text in texts.items():
        for secret in secrets:
            assert secret not in text, f"{where} leaks a secret"
    for name in ("python", "clock", "power", "token"):
        assert "***" in report.result(name).detail, name
    assert "TRADING_MODE ignored" in report.result("env_file").detail


def test_laptop_paths(tmp_path, monkeypatch, lap):
    for key in ("TITAN2_ENV_FILE", "TITAN2_VAR", *ENV_FILE_VALUES):
        monkeypatch.delenv(key, raising=False)                     # the real process env plays no part
    roaming, local = tmp_path / "Roaming", tmp_path / "Local"      # never the real %APPDATA% or %LOCALAPPDATA%
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    if os.name == "nt":       # the laptop's own folders: %APPDATA%\titan2\titan2.env, %LOCALAPPDATA%\titan2\var
        ctx = selfcheck.Context(selfcheck.LAPTOP, env={}, now=NOW)
        var = local / "titan2" / "var"
        assert (ctx.env_file, ctx.var_dir) == (roaming / "titan2" / "titan2.env", var)
        assert ctx.session_path == var / "session" / token.SESSION_FILENAME          # under var, as on Linux
        assert ctx.db_path == var / "db" / store_mod.DB_FILENAME
    # the other phases keep their Linux paths
    other = selfcheck.Context(selfcheck.MORNING, env={}, now=NOW)
    assert (other.env_file, other.var_dir) == (selfcheck.ENV_FILE, Path(settings_mod.DEFAULT_VAR_DIR))
    # TITAN2_ENV_FILE and TITAN2_VAR win, from the env given or the process; explicit arguments win over both
    named = {"TITAN2_ENV_FILE": str(tmp_path / "a.env"), "TITAN2_VAR": str(tmp_path / "va")}
    ctx = selfcheck.Context(selfcheck.LAPTOP, env=named, now=NOW)
    assert (ctx.env_file, ctx.var_dir) == (tmp_path / "a.env", tmp_path / "va")
    assert ctx.session_path == tmp_path / "va" / "session" / token.SESSION_FILENAME
    ctx = selfcheck.Context(selfcheck.LAPTOP, env=named, now=NOW, env_file=tmp_path / "b.env", var_dir=tmp_path / "vb")
    assert (ctx.env_file, ctx.var_dir) == (tmp_path / "b.env", tmp_path / "vb")
    monkeypatch.setenv("TITAN2_VAR", str(tmp_path / "vp"))
    ctx = selfcheck.Context(selfcheck.LAPTOP, env={"TITAN2_ENV_FILE": named["TITAN2_ENV_FILE"]}, now=NOW)
    assert ctx.var_dir == tmp_path / "vp"
    monkeypatch.delenv("TITAN2_VAR")
    env_file = write_env(tmp_path / "c.env")
    # the env is the env file overridden by the env given, as token.load_env does: a CLI env built without the
    # laptop file still sees its Kite keys
    ctx = selfcheck.Context(selfcheck.LAPTOP, env={"ZERODHA_USER_ID": "ZZ9999"}, env_file=env_file, now=NOW)
    assert ctx.env["ZERODHA_API_KEY"] == API_KEY and ctx.env["ZERODHA_USER_ID"] == "ZZ9999"
    # no env given: the env file overridden by the process environment
    monkeypatch.setenv("TITAN2_ENV_FILE", str(env_file))
    monkeypatch.setenv("ZERODHA_USER_ID", "YY8888")
    ctx = selfcheck.Context(selfcheck.LAPTOP, now=NOW)
    assert ctx.env_file == env_file
    assert ctx.env["ZERODHA_API_KEY"] == API_KEY and ctx.env["ZERODHA_USER_ID"] == "YY8888"
    # a UTF-16 env file does not stop the check from starting; its row says what is wrong
    write_env(env_file, encoding="utf-16")
    ctx = selfcheck.Context(selfcheck.LAPTOP, now=NOW)
    ok, detail = selfcheck._check_env_file(ctx)
    assert not ok and "is not UTF-8 text" in detail


def test_laptop_cli(tmp_path, lap, monkeypatch):
    monkeypatch.setattr(selfcheck.alerts, "send_now", lambda *a, **k: pytest.fail("the laptop check sends nothing"))
    out: list[str] = []
    kw = laptop_kw(tmp_path)
    assert selfcheck.cli(selfcheck.LAPTOP, out=out.append, **kw) == 0
    lines = out[0].splitlines()
    assert lines[0] == "titan2 self-check laptop 2026-10-12 08:50: PASS (0 fail, 0 warn, 12 pass, 0 skip)"
    assert [ln.split(":")[0] for ln in lines[1:]] == [f"PASS {n}" for n in ROWS]          # every row is printed
    saved = json.loads(next((kw["var_dir"] / "reports").glob("selfcheck-*-laptop.json")).read_text(encoding="utf-8"))
    assert (saved["phase"], saved["ok"], saved["fatal"], saved["exit_code"]) == ("laptop", True, False, 0)
    lap.power = {"on_ac": False, "battery_pct": 30}
    assert selfcheck.cli(selfcheck.LAPTOP, out=out.append, **laptop_kw(tmp_path / "w")) == 0     # a WARN passes
    assert "WARN power: on battery (battery 30%)" in out[-1]
    lap.power = {"on_ac": True, "battery_pct": 100}
    lap.offset = 1.25                                                          # beyond 1 s: entries stay off
    assert selfcheck.cli(selfcheck.LAPTOP, out=out.append, **laptop_kw(tmp_path / "d")) == 2
    assert "FAIL clock: this clock is 1.250 s behind internet time: more than 1 s off" in out[-1]
    assert "clock_drift" not in out[-1]
    assert selfcheck.cli("nightly", out=out.append) == 2 and "laptop" in out[-1]
    with pytest.raises(ValueError, match="laptop"):
        selfcheck.run_phase("nightly")
    with pytest.raises(ValueError):
        selfcheck.run_phase(selfcheck.LAPTOP, selfcheck.Context(selfcheck.MORNING, **laptop_kw(tmp_path / "m")))
