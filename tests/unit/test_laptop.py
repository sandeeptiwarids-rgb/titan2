"""Laptop mode (titan2/laptop.py and its seams in runtime, feed, token, settings, clock and the CLI): the profile, the
Windows paths, PAPER only, keep-awake and power (ctypes faked), the clock probe (network faked), the two-stage Ctrl+C,
the day runner's sequence (bot faked) and one short in-process session on the real BotProcess with test fakes.

Offline and deterministic except the in-process session, which runs the real threads on a scaled clock. No broker,
no real env file, session or secret is touched; every Win32 call goes to FakeKernel32.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import titan2.__main__ as cli
from titan2 import laptop, runtime, selfcheck
from titan2 import settings as settings_mod
from titan2 import store as store_mod
from titan2 import token as token_mod
from titan2.clock import Clock, FakeClock, RealClock, SessionCalendar, ShiftedClock, ist_datetime
from titan2.core import operator, positions
from titan2.core.events import Cancel, Place, SelfcheckResult
from titan2.market.feed import Feed, SignalQueue
from titan2.market.quotes import QuoteCache
from titan2.net import Probe
from titan2.model import (
    EXPIRY_RULE_NEAREST, Account, Direction, Halt, HaltClears, Intent, OrderRequest, Policy, Recheck, Role, Side,
    StrikePref, StructureType, make_tag,
)
from titan2.store import Store

S = date(2026, 10, 12)                                    # a Monday session
SAT = date(2026, 10, 10)
CAL = SessionCalendar.from_dict({"years": {"2026": {"holidays": ["2026-10-02", "2026-10-20"]}},
                                 "regular_hours": [{"from": "2026-01-01", "open": "09:15", "close": "15:40"}]})
ON, OFF = laptop.ES_CONTINUOUS | laptop.ES_SYSTEM_REQUIRED, laptop.ES_CONTINUOUS
SECRETS = {"ZERODHA_API_SECRET": "ApiSecret-9f2", "ZERODHA_PASSWORD": "Pw-hunter2",
           "ZERODHA_TOTP_SECRET": "TOTPBASE32X"}


@pytest.fixture(autouse=True)
def _fresh_settings():
    settings_mod._reset_load_once()
    yield
    settings_mod._reset_load_once()


# ---------------------------------------------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------------------------------------------
class FakeKernel32:
    """The kernel32 calls laptop.py makes, recorded; GetSystemPowerStatus fills the caller's structure."""

    def __init__(self, *, ac: int = 1, flag: int = 0, pct: int = 87, power_ok: int = 1, state_ok: bool = True):
        self.states: list[int] = []
        self.handlers: list[tuple[Any, bool]] = []

        def set_state(flags: int) -> int:
            self.states.append(flags)
            return 0x80000000 if state_ok else 0

        def get_power(ref: Any) -> int:
            st = ref._obj                                  # ctypes.byref(structure)._obj is the structure
            st.ACLineStatus, st.BatteryFlag, st.BatteryLifePercent = ac, flag, pct
            return power_ok

        def set_handler(routine: Any, add: bool) -> int:
            self.handlers.append((routine, bool(add)))
            return 1

        self.SetThreadExecutionState = set_state
        self.GetSystemPowerStatus = get_power
        self.SetConsoleCtrlHandler = set_handler
        self.console_mode: int | None = 0x01F7               # what a classic console reports (QuickEdit on)
        self.modes_set: list[tuple[int, int]] = []

        def get_std_handle(which: int) -> int:
            assert which == laptop.STD_INPUT_HANDLE
            return 77

        def get_console_mode(handle: int, ref: Any) -> int:
            if self.console_mode is None:
                return 0                                   # not a console: the input is redirected
            ref._obj.value = self.console_mode
            return 1

        def set_console_mode(handle: int, mode: int) -> int:
            self.modes_set.append((handle, mode))
            return 1

        self.GetStdHandle = get_std_handle
        self.GetConsoleMode = get_console_mode
        self.SetConsoleMode = set_console_mode


class FakeBot:
    """A BotProcess as the Session sees it: only its StopRequest."""

    def __init__(self) -> None:
        self.stop = runtime.StopRequest()


def lap_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {"TITAN2_VAR": str(tmp_path / "var"), "TITAN2_LOG_DIR": str(tmp_path / "logs"),
           "TITAN2_ENV_FILE": str(tmp_path / "titan2.env")}
    env.update(extra)
    return env


def _clean_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "titan2").mkdir(parents=True, exist_ok=True)
    return app


def _no_kite(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("no Kite client may be built here")


class RecordingAlerter:
    """The alerts.Alerter surface BotProcess uses, recording instead of sending."""

    healthcheck = None

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def start(self) -> None:
        pass

    def stop(self, timeout: float | None = None) -> None:
        pass

    def submit(self, severity: str, code: str, text: str, dedup_key: str | None = None) -> bool:
        self.sent.append((severity, code, text, dedup_key))
        return True

    def dispatch(self, cmd: Any) -> bool:
        return self.submit(cmd.severity, cmd.code, cmd.text, cmd.dedup_key)

    def fail(self, text: str) -> None:
        self.sent.append(("FAIL", "", text, None))

    def ping_now(self) -> None:
        pass

    def ping_ready(self) -> None:
        pass

    def unacked_critical(self) -> int:
        return 0


def _events(var: Path, kind: str | None = None) -> list[dict]:
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        return st.events(kind=kind)
    finally:
        st.close()


# ---------------------------------------------------------------------------------------------------------------
# Profile and paths
# ---------------------------------------------------------------------------------------------------------------
def test_profile_detection(monkeypatch):
    assert laptop.is_laptop({"TITAN2_PROFILE": "laptop"}) and laptop.is_laptop({"TITAN2_PROFILE": " Laptop "})
    assert not laptop.is_laptop({}) and not laptop.is_laptop({"TITAN2_PROFILE": "server"})
    monkeypatch.setenv("TITAN2_PROFILE", "laptop")
    assert laptop.is_laptop()                              # the process environment by default
    monkeypatch.delenv("TITAN2_PROFILE")
    assert not laptop.is_laptop()
    assert runtime.BotProcess(env={"TITAN2_PROFILE": "laptop", "TITAN2_VAR": "x"}).laptop
    bot = runtime.BotProcess(env={"TITAN2_VAR": "x"}, laptop=True)
    assert bot.laptop and bot.env["TITAN2_PROFILE"] == "laptop"     # what the laptop self-check rows read
    assert not runtime.BotProcess(env={"TITAN2_VAR": "x"}).laptop


def test_windows_paths_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(laptop, "WINDOWS", True)
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    p = laptop.windows_paths(env)
    assert (p.base, p.var, p.logs) == (local / "titan2", local / "titan2" / "var", local / "titan2" / "logs")
    assert p.env_file == roaming / "titan2" / "titan2.env"
    assert (laptop.default_var_dir(env), laptop.default_log_dir(env), laptop.default_env_file(env)) == \
        (p.var, p.logs, p.env_file)
    over = dict(env, TITAN2_VAR=str(tmp_path / "v"), TITAN2_LOG_DIR=str(tmp_path / "l"),
                TITAN2_ENV_FILE=str(tmp_path / "e.env"))
    assert (laptop.default_var_dir(over), laptop.default_log_dir(over), laptop.default_env_file(over)) == \
        (tmp_path / "v", tmp_path / "l", tmp_path / "e.env")
    # every default follows: the CLI, settings, the token unit and the bot
    assert cli.var_dir(env) == settings_mod.default_var_dir(env) == p.var
    assert runtime.BotProcess(env=env).var == p.var
    assert token_mod.env_file(env) == p.env_file
    for k in ("TITAN2_VAR", "TITAN2_ENV_FILE", "TITAN2_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    assert token_mod.var_dir() == p.var and token_mod.session_path() == p.var / "session" / "kite_session.json"
    p.env_file.parent.mkdir(parents=True)
    p.env_file.write_text("ZERODHA_API_KEY=fromfile\nZERODHA_USER_ID=AB1234\n", encoding="utf-8")
    merged = token_mod.load_env(environ={"APPDATA": str(roaming), "ZERODHA_USER_ID": "ZZ9999"})
    assert merged["ZERODHA_API_KEY"] == "fromfile" and merged["ZERODHA_USER_ID"] == "ZZ9999"   # the environment wins
    # settings.default_path() moves only in the laptop profile (outside it, it stays the EC2 path)
    assert settings_mod.default_path().as_posix() == "/var/lib/titan2/config/settings.json"
    monkeypatch.setenv("TITAN2_PROFILE", "laptop")
    assert settings_mod.default_path() == p.var / "config" / "settings.json"


def test_linux_defaults_unchanged(monkeypatch):
    monkeypatch.setattr(laptop, "WINDOWS", False)
    env = {"LOCALAPPDATA": "C:/x", "APPDATA": "C:/y", "TITAN2_PROFILE": "laptop"}
    assert laptop.default_var_dir(env).as_posix() == "/var/lib/titan2"
    assert laptop.default_log_dir(env).as_posix() == "/var/log/titan2"
    assert laptop.default_env_file(env).as_posix() == "/etc/titan2/titan2.env"
    assert settings_mod.default_var_dir(env).as_posix() == cli.var_dir({}).as_posix() == "/var/lib/titan2"
    assert runtime.BotProcess(env=env).var.as_posix() == "/var/lib/titan2"
    assert cli.var_dir({"TITAN2_VAR": "/srv/titan2"}).as_posix() == "/srv/titan2"
    assert token_mod.env_file({}) == token_mod.ENV_FILE
    assert token_mod.env_file({"TITAN2_ENV_FILE": "/opt/t.env"}).as_posix() == "/opt/t.env"    # as ops/backup.sh
    monkeypatch.delenv("TITAN2_VAR", raising=False)
    assert token_mod.var_dir().as_posix() == "/var/lib/titan2"


def test_load_env_survives_a_utf16_file(tmp_path, caplog):
    """Windows PowerShell 5's `>` writes UTF-16: the file is ignored with a warning (the self-check's env_file row
    says what to fix), never a crash of the CLI or the laptop runner."""
    p = tmp_path / "titan2.env"
    p.write_text("ZERODHA_API_KEY=k\n", encoding="utf-16")
    with caplog.at_level(logging.WARNING, logger="titan2.token"):
        assert token_mod.load_env(p, environ={"A": "1"}) == {"A": "1"}
    assert "not UTF-8" in caplog.text
    with pytest.raises(ValueError):
        token_mod.read_env_file(p)                          # what the self-check's row reads


def test_shifted_clock():
    base = FakeClock(ist_datetime(S, "09:00"))
    c = ShiftedClock(base, 3)
    assert c.today() == S + timedelta(days=3) and c.now_wall().time() == base.now_wall().time()
    assert c.mono() == base.mono()
    base.advance(5.0)
    assert (c.now_wall() - base.now_wall()) == timedelta(days=3) and c.mono() == base.mono()
    with pytest.raises(TypeError):
        ShiftedClock(base, 1.5)


def test_prune_logs(tmp_path):
    old, new, other = tmp_path / "laptop-2026-08-01.log", tmp_path / "dash-2026-10-11.log", tmp_path / "bot.log"
    for p in (old, new, other):
        p.write_text("x", encoding="utf-8")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    assert laptop.prune_logs(tmp_path) == 1
    assert not old.exists() and new.exists() and other.exists()      # only the laptop's own dated files


# ---------------------------------------------------------------------------------------------------------------
# PAPER only
# ---------------------------------------------------------------------------------------------------------------
def test_live_refused_in_the_laptop_profile(tmp_path, monkeypatch, caplog):
    """ALLOW_LIVE=1 is ignored in the laptop profile: allow_live False (the LIVE lane refuses entries, the engine's
    I14 too), and each LIVE-mode strategy runs as PAPER for the session, with a WARNING and a LIVE_REFUSED event.
    Elsewhere ALLOW_LIVE=1 still means key 1."""
    assert runtime.BotProcess(env={"TITAN2_VAR": str(tmp_path), "ALLOW_LIVE": "1"}).allow_live is True
    bot = runtime.BotProcess(env={"TITAN2_VAR": str(tmp_path), "ALLOW_LIVE": "1"}, laptop=True)
    assert bot.allow_live is False and bot.allow_live_env is True
    var = tmp_path / "var"
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        now = ist_datetime(SAT, "09:00")
        with st.tx():
            st.put_strategy_mode("opening_drive", version="1.x", mode="LIVE", changed_wall=now, changed_by="test")
            st.put_strategy_mode("price_spike_up", version="1.x", mode="PAPER", pending_mode="LIVE",
                                 changed_wall=now, changed_by="test")
        st.close()
    monkeypatch.setattr(runtime.alerts.Alerter, "from_env", lambda *a, **k: RecordingAlerter())
    with store_mod.core_thread(), caplog.at_level(logging.WARNING, logger="titan2.runtime"):
        code = runtime.run_bot(env={"TITAN2_VAR": str(var), "ALLOW_LIVE": "1"}, laptop=True,
                               clock=FakeClock(ist_datetime(SAT, "10:00")), calendar=CAL,
                               notifier=runtime.Notifier(send=lambda p: None), kite_factory=_no_kite,
                               ticker_factory=_no_kite, cwd=_clean_app(tmp_path), check_kite=lambda **_k: None,
                               install_signals=False)
    assert code == runtime.EXIT_OK
    boot = _events(var, "BOOT")[-1]["payload"]
    assert boot["allow_live"] is False and boot["profile"] == "laptop"
    (ev,) = _events(var, "LIVE_REFUSED")
    assert ev["severity"] == "WARN" and ev["payload"]["live_strategies"] == ["opening_drive", "price_spike_up"]
    assert ev["payload"]["allow_live_env"] is True and ev["payload"]["reasons"] == list(laptop.LIVE_REFUSED_REASONS)
    assert "registered static IP" in ev["payload"]["reasons"][0]
    assert "opening_drive is in LIVE mode; it runs as PAPER this session" in caplog.text
    assert "ALLOW_LIVE=1 is ignored" in caplog.text
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        assert [m["mode"] for m in st.strategy_modes()] == ["LIVE", "PAPER"]      # the modes are kept for EC2
    finally:
        st.close()


def test_live_refused_for_price_spike_down_in_the_laptop_profile(tmp_path, monkeypatch, caplog):
    """price_spike_down (2026-10-12, a PAPER experiment against brief E1) gets the laptop's PAPER-only treatment like
    every strategy: in LIVE mode it is named in the LIVE_REFUSED event and the WARNING, runs as PAPER for the session,
    and its mode in titan2.db is kept."""
    var = tmp_path / "var"
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        with st.tx():
            st.put_strategy_mode("price_spike_down", version="1.x", mode="LIVE",
                                 changed_wall=ist_datetime(SAT, "09:00"), changed_by="test")
        st.close()
    monkeypatch.setattr(runtime.alerts.Alerter, "from_env", lambda *a, **k: RecordingAlerter())
    with store_mod.core_thread(), caplog.at_level(logging.WARNING, logger="titan2.runtime"):
        code = runtime.run_bot(env={"TITAN2_VAR": str(var)}, laptop=True, clock=FakeClock(ist_datetime(SAT, "10:00")),
                               calendar=CAL, notifier=runtime.Notifier(send=lambda p: None), kite_factory=_no_kite,
                               ticker_factory=_no_kite, cwd=_clean_app(tmp_path), check_kite=lambda **_k: None,
                               install_signals=False)
    assert code == runtime.EXIT_OK
    assert _events(var, "BOOT")[-1]["payload"]["allow_live"] is False
    (ev,) = _events(var, "LIVE_REFUSED")
    assert ev["severity"] == "WARN" and ev["payload"]["live_strategies"] == ["price_spike_down"]
    assert "strategy price_spike_down is in LIVE mode; it runs as PAPER this session" in caplog.text
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        assert st.strategy_mode("price_spike_down")["mode"] == "LIVE"            # kept for EC2
    finally:
        st.close()


def test_selfcheck_runner_maps_prestart_to_laptop(monkeypatch):
    ran, posted = [], []
    monkeypatch.setattr(runtime.selfcheck, "run_phase", lambda phase, ctx: ran.append((phase, ctx)) or
                        SimpleNamespace(ok=True))
    r = runtime.SelfcheckRunner(lambda phase: f"ctx:{phase}", posted.append, phases={"prestart": "laptop"})
    r.run("prestart")
    r._thread.join(5)
    assert ran == [("laptop", "ctx:laptop")]
    assert isinstance(posted[0], SelfcheckResult) and posted[0].phase == "laptop" and posted[0].ok
    plain = runtime.SelfcheckRunner(lambda phase: phase, posted.append)
    plain.run("prestart")
    plain._thread.join(5)
    assert ran[-1][0] == "prestart"                          # the server keeps its prestart phase


# ---------------------------------------------------------------------------------------------------------------
# Keep-awake, power, the console close handler
# ---------------------------------------------------------------------------------------------------------------
def test_keep_awake_set_and_clear(monkeypatch):
    k = FakeKernel32()
    awake = laptop.KeepAwake(kernel32=k)
    assert awake.on() and awake.active
    awake.off()
    awake.off()                                              # once only
    assert k.states == [ON, OFF] and not awake.active
    failing = FakeKernel32(state_ok=False)
    awake = laptop.KeepAwake(kernel32=failing)
    assert not awake.on()
    awake.off()
    assert failing.states == [ON]                            # nothing to clear when it never took
    monkeypatch.setattr(laptop, "WINDOWS", False)
    assert laptop.KeepAwake().on() is False                  # a no-op elsewhere


def test_power_status(monkeypatch):
    st = laptop.power_status(FakeKernel32(ac=1, pct=87))
    assert st == {"on_ac": True, "battery_pct": 87} and isinstance(st, dict) and st.on_ac is True
    st = laptop.power_status(FakeKernel32(ac=0, pct=42))
    assert (st.on_ac, st.battery_pct) == (False, 42) and laptop.power_text(st) == "ON BATTERY, battery 42%"
    assert laptop.power_status(FakeKernel32(ac=255, flag=255, pct=255)) == {"on_ac": None, "battery_pct": None}
    assert laptop.power_status(FakeKernel32(ac=1, flag=128, pct=255)) == {"on_ac": True, "battery_pct": None}
    assert laptop.power_status(FakeKernel32(power_ok=0)) == {"on_ac": None, "battery_pct": None}
    broken = SimpleNamespace(GetSystemPowerStatus=lambda ref: 1 / 0)
    assert laptop.power_status(broken) == {"on_ac": None, "battery_pct": None}
    assert laptop.power_text({"on_ac": True, "battery_pct": None}) == "on AC power"
    monkeypatch.setattr(laptop, "WINDOWS", False)
    assert laptop.power_status() == {"on_ac": None, "battery_pct": None}


def test_quick_edit_off_and_back(monkeypatch):
    """A click in a classic console window would pause every console write (and the thread doing it) until Esc:
    QuickEdit goes off for the run, and the previous mode comes back."""
    k = FakeKernel32()
    restore = laptop.quick_edit_off(kernel32=k)
    ((handle, mode),) = k.modes_set
    assert handle == 77 and not mode & laptop.ENABLE_QUICK_EDIT_MODE and mode & laptop.ENABLE_EXTENDED_FLAGS
    assert mode & ~(laptop.ENABLE_QUICK_EDIT_MODE | laptop.ENABLE_EXTENDED_FLAGS) == 0x01F7 & ~0xC0   # rest kept
    restore()
    assert k.modes_set[-1] == (77, 0x01F7)
    k.console_mode = None
    assert laptop.quick_edit_off(kernel32=k) is None                  # redirected input: nothing to change
    monkeypatch.setattr(laptop, "WINDOWS", False)
    assert laptop.quick_edit_off() is None


def test_console_log_never_blocks_the_caller():
    """The core thread logs; a console that stops reading must not stop it: the queue takes the record at once and
    the listener writes it when the console reads again."""
    release, wrote = threading.Event(), []

    class StuckConsole:
        def write(self, text: str) -> None:
            release.wait(10)
            wrote.append(text)

        def flush(self) -> None:
            pass

    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)
    listener = laptop.console_log(StuckConsole())
    try:
        t0 = time.monotonic()
        logging.getLogger("titan2.core.engine").warning("exit order sent")
        logging.getLogger("titan2.core.engine").warning("verified flat")
        assert time.monotonic() - t0 < 1.0 and wrote == []
        printed: list[str] = []
        laptop.logged(printed.append)("token: OK")          # the runner's lines: printed, and logged off-console
        assert printed == ["token: OK"]
        release.set()
    finally:
        listener.stop()                                      # drains the queue
        for h in [h for h in root.handlers if getattr(h, runtime._LOG_MARK, False)]:
            root.removeHandler(h)
        root.setLevel(level)
    text = "".join(wrote)
    assert "WARNING [" in text and "titan2.core.engine: exit order sent" in text and "verified flat" in text
    assert "token: OK" not in text                           # never twice on the console


def test_logged_lines_reach_the_log_file(tmp_path):
    root = logging.getLogger()
    level = root.level
    try:
        path = runtime.setup_logging(tmp_path, filename="laptop-x.log", console=False)
        laptop.logged(lambda s: None)("self-check: FAIL rows block new entries")
        for h in root.handlers:
            h.flush()
        assert "titan2.laptop: self-check: FAIL rows block new entries" in path.read_text(encoding="utf-8")
    finally:
        for h in [h for h in root.handlers if getattr(h, runtime._LOG_MARK, False)]:
            root.removeHandler(h)
            h.close()
        root.setLevel(level)


def test_stop_reactor_only_when_running(monkeypatch):
    calls: list = []
    fake = SimpleNamespace(running=True, stop=lambda: None, callFromThread=lambda fn: calls.append(fn))
    monkeypatch.setitem(sys.modules, "twisted.internet.reactor", fake)
    laptop.stop_reactor()
    assert calls == [fake.stop]
    fake.running = False
    laptop.stop_reactor()
    monkeypatch.delitem(sys.modules, "twisted.internet.reactor")
    laptop.stop_reactor()
    assert calls == [fake.stop]


def test_console_routine_handles_close_only():
    seen: list[int] = []
    assert laptop.console_routine(laptop.CTRL_CLOSE_EVENT, seen.append) == 1
    assert laptop.console_routine(laptop.CTRL_SHUTDOWN_EVENT, seen.append) == 1
    assert laptop.console_routine(0, seen.append) == 0       # CTRL_C_EVENT: the C runtime raises SIGINT
    assert laptop.console_routine(1, seen.append) == 0       # CTRL_BREAK_EVENT: SIGBREAK
    assert laptop.console_routine(laptop.CTRL_LOGOFF_EVENT, lambda t: 1 / 0) == 1      # an error is logged
    assert seen == [laptop.CTRL_CLOSE_EVENT, laptop.CTRL_SHUTDOWN_EVENT]


def test_ctrl_c_turned_back_on(monkeypatch):
    """A process started by one that ignores Ctrl+C (CREATE_NEW_PROCESS_GROUP, a scheduler) inherits that, and would
    never see the first press: `titan2 laptop` turns Ctrl+C back on for itself."""
    k = FakeKernel32()
    assert laptop.ctrl_c_on(kernel32=k) is True and k.handlers == [(None, False)]
    monkeypatch.setattr(laptop, "WINDOWS", False)
    assert laptop.ctrl_c_on() is False                       # a no-op elsewhere


@pytest.mark.skipif(os.name != "nt", reason="ctypes.WINFUNCTYPE exists on Windows only")
def test_install_console_handler_registers_and_removes():
    k, seen = FakeKernel32(), []
    remove = laptop.install_console_handler(seen.append, kernel32=k)
    ((routine, added),) = k.handlers
    assert added and routine(laptop.CTRL_CLOSE_EVENT) == 1 and routine(0) == 0 and seen == [2]
    remove()
    assert k.handlers[-1] == (routine, False)


def test_console_close_flattens_while_time_allows():
    """A console close is the first press; Windows ends the process ~5 s later, so the clean stop follows when the
    session is not done by then (or at once when Ctrl+C was already pressed)."""
    ctl, session, done = laptop.CtrlC(), laptop.Session(), threading.Event()
    bot = FakeBot()
    session.bind(bot)
    done.set()
    laptop.on_console_close(ctl, session, done, flatten_s=0.01, stop_s=0.01)
    assert ctl.stage == 1 and bot.stop.reason is None        # it finished in time
    ctl, session, bot = laptop.CtrlC(), laptop.Session(), FakeBot()
    session.bind(bot)
    laptop.on_console_close(ctl, session, threading.Event(), flatten_s=0.01, stop_s=0.01)
    assert ctl.stage == 1 and bot.stop.reason == "CONSOLE_CLOSE" and session.reason == "CONSOLE_CLOSE"
    ctl, session, bot = laptop.CtrlC(), laptop.Session(), FakeBot()
    session.bind(bot)
    ctl.press("SIGINT")
    t0 = time.monotonic()
    laptop.on_console_close(ctl, session, threading.Event(), flatten_s=5.0, stop_s=0.01)
    assert ctl.stage == 2 and bot.stop.reason == "CONSOLE_CLOSE" and time.monotonic() - t0 < 2.0


# ---------------------------------------------------------------------------------------------------------------
# The two-stage Ctrl+C
# ---------------------------------------------------------------------------------------------------------------
def test_ctrl_c_state_machine_and_session():
    ctl = laptop.CtrlC()
    assert ctl.stage == 0
    assert ctl.press("SIGINT") == 1 and ctl.press("SIGBREAK") == 2 and ctl.press("SIGINT") == 2
    assert ctl.presses == 3 and ctl.sources == ["SIGINT", "SIGBREAK", "SIGINT"]
    session, first, later = laptop.Session(), FakeBot(), FakeBot()
    session.bind(first)
    session.request_stop("CTRL_C")
    session.request_stop("CTRL_C_TWICE")                     # the first reason is kept
    assert session.reason == "CTRL_C" and first.stop.reason == "CTRL_C"
    session.bind(None)
    session.bind(later)                                      # a restart after a stop request stops at once
    assert later.stop.reason == "CTRL_C"


def test_signal_handlers_two_stages_then_restored():
    """SIGINT (and SIGBREAK on Windows) count presses: the first only flags (the supervisor flattens), the second
    requests the clean stop at once; SIGTERM is a clean stop. The previous handlers come back afterwards."""
    sigs = [s for s in (signal.SIGINT, getattr(signal, "SIGBREAK", None), signal.SIGTERM) if s is not None]
    before = {s: signal.getsignal(s) for s in sigs}
    ctl, session, done = laptop.CtrlC(), laptop.Session(), threading.Event()
    bot = FakeBot()
    session.bind(bot)
    k = FakeKernel32() if os.name == "nt" else None
    restore = laptop.install_handlers(ctl, session, done, kernel32=k)
    try:
        press = signal.getsignal(signal.SIGINT)
        press(signal.SIGINT, None)
        assert ctl.stage == 1 and bot.stop.reason is None
        second = getattr(signal, "SIGBREAK", signal.SIGINT)
        signal.getsignal(second)(second, None)
        assert ctl.stage == 2 and bot.stop.reason == "CTRL_C_TWICE"
        other = laptop.Session()
        restore_term = laptop.install_handlers(laptop.CtrlC(), other, done, kernel32=k)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert other.reason == "SIGTERM"
        restore_term()
    finally:
        restore()
    assert {s: signal.getsignal(s) for s in sigs} == before
    if k is not None:
        assert [added for _, added in k.handlers] == [True, True, False, False]   # console handler in and out


def test_real_sigint_reaches_the_state_machine():
    ctl, session, done = laptop.CtrlC(), laptop.Session(), threading.Event()
    restore = laptop.install_handlers(ctl, session, done, kernel32=FakeKernel32() if os.name == "nt" else None)
    try:
        t = threading.Thread(target=signal.raise_signal, args=(signal.SIGINT,))
        t.start()
        t.join(5)
        deadline = time.monotonic() + 5
        while ctl.stage == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        restore()
    assert ctl.sources == ["SIGINT"] and session.reason is None


# ---------------------------------------------------------------------------------------------------------------
# The supervisor: the first press's flatten, the second press, power, the dashboard
# ---------------------------------------------------------------------------------------------------------------
NOW = ist_datetime(S, "11:00")


def _db(var: Path, *, open_structure: bool = True, pause: bool = False) -> None:
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db", backup_dir=var / "backups")
        if open_structure:
            intent = Intent("lap0open0001", "opening_drive", "1.x", "NSE:RELIANCE", Direction.UP,
                            StructureType.LONG_CE, StrikePref.ITM1, EXPIRY_RULE_NEAREST, 2900.0,
                            Recheck(2880.0, 2930.0), 10.0, 5.0, NOW, {})
            with st.tx():
                st.insert_intent(intent, S)
            positions.reserve(st, intent, book="PAPER:opening_drive", account=Account.PAPER, policy=Policy.FULL,
                              now_mono=5.0, now_wall=NOW)
        if pause:
            with st.tx():
                st.add_halt("GLOBAL", "PAUSE", "operator PAUSE", "RESUME", NOW, session=S, operator=True)
        st.close()


def _ack(var: Path, cmd_id: int, kind: str, status: str, result: str) -> None:
    with store_mod.core_thread():
        st = Store(var / "db" / "titan2.db")
        with st.tx():
            st.put_command_ack("CLI", cmd_id, kind, status, NOW, result=result,
                               done_wall=None if status == "ACCEPTED" else NOW)
        st.close()


def _sent(var: Path) -> list:
    return operator.read_new(var / "db" / cli.CLI_DB, "CLI", 0)


def _supervisor(var: Path, **kw: Any) -> tuple[laptop.Supervisor, laptop.CtrlC, FakeBot, list[str], list[float]]:
    ctl, session, bot, out, mono = laptop.CtrlC(), laptop.Session(), FakeBot(), [], [100.0]
    session.bind(bot)
    kw.setdefault("power", lambda: laptop.PowerStatus(on_ac=True, battery_pct=90))
    sup = laptop.Supervisor(ctl=ctl, session=session, var=var, clock=FakeClock(NOW), out=out.append, cli_mod=cli,
                            mono=lambda: mono[0], **kw)
    return sup, ctl, bot, out, mono


def test_first_press_flattens_through_the_exit_engine_then_stops_without_resume(tmp_path):
    """The FLATTEN halt keeps entries off from the first press to the stop: on DONE the stop is requested at once and
    no RESUME is ever sent (the engine clears that halt itself once stopping, scenario test_laptop_paper_only)."""
    var = tmp_path / "var"
    _db(var)
    sup, ctl, bot, out, mono = _supervisor(var)
    sup.step()
    assert sup.flatten_id is None and not (var / "db" / cli.CLI_DB).exists()      # nothing pressed, nothing sent
    ctl.press("SIGINT")
    sup.step()
    (row,) = _sent(var)
    assert (row.kind, row.confirm_text, row.requested_by) == ("FLATTEN_ALL", "FLATTEN", laptop.REQUESTED_BY)
    assert sup.flatten_id == row.cmd_id and bot.stop.reason is None and "Ctrl+C again" in out[0]
    sup.step()
    assert out[-1].startswith(f"flatten #{row.cmd_id} QUEUED")
    _ack(var, row.cmd_id, "FLATTEN_ALL", "ACCEPTED", "flattening every bot leg")
    sup.step()
    assert out[-1].startswith(f"flatten #{row.cmd_id} ACCEPTED: 0 open leg(s)")
    n = len(out)
    sup.step()
    assert len(out) == n                                     # unchanged progress is not repeated...
    mono[0] += laptop.PROGRESS_EVERY_S
    sup.step()
    assert len(out) == n + 1                                 # ...until PROGRESS_EVERY_S
    assert bot.stop.reason is None                           # not flat yet: no stop
    _ack(var, row.cmd_id, "FLATTEN_ALL", "DONE", "verified flat")
    sup.step()
    assert out[-1].startswith("verified flat (verified flat): stopping cleanly") and bot.stop.reason == "CTRL_C"
    for _ in range(3):
        sup.step()
    assert [r.kind for r in _sent(var)] == ["FLATTEN_ALL"] and sup.flatten_id is None    # never a RESUME
    assert not hasattr(sup, "resume_id") and not hasattr(laptop, "RESUME_WAIT_S")
    ctl.press("SIGINT")                                      # the second press, while the clean stop runs
    sup.step()
    assert "second Ctrl+C: stopping now" in out[-1] and bot.stop.reason == "CTRL_C"
    ctl.press("SIGINT")
    sup.step()
    assert "already stopping" in out[-1]


def test_first_press_with_nothing_open_still_sends_flatten_all(tmp_path):
    """No shortcut: with nothing open the FLATTEN_ALL still goes out, so its halt stops entries before the stop (an
    intent handled between the press and the Shutdown would otherwise open a position that the stop leaves open)."""
    var = tmp_path / "var"
    _db(var, open_structure=False)
    sup, ctl, bot, out, _ = _supervisor(var)
    ctl.press("SIGINT")
    sup.step()
    (row,) = _sent(var)
    assert (row.kind, row.requested_by) == ("FLATTEN_ALL", laptop.REQUESTED_BY) and bot.stop.reason is None
    _ack(var, row.cmd_id, "FLATTEN_ALL", "DONE", "already flat (verified flat)")
    sup.step()
    assert bot.stop.reason == "CTRL_C" and "verified flat (already flat (verified flat))" in out[-1]
    sup, ctl, bot, out, _ = _supervisor(tmp_path / "nodb")            # no titan2.db yet: the bot reads it at start
    ctl.press("SIGINT")
    sup.step()
    assert [r.kind for r in _sent(tmp_path / "nodb")] == ["FLATTEN_ALL"] and bot.stop.reason is None


def test_first_press_that_cannot_queue_its_flatten_stops_cleanly(tmp_path):
    var = tmp_path / "var"
    _db(var)
    sup, ctl, bot, out, _ = _supervisor(var)
    (var / "db" / cli.CLI_DB).mkdir(parents=True)                    # a folder where the command file should be
    ctl.press("SIGINT")
    sup.step()
    assert bot.stop.reason == "CTRL_C" and out[-1].startswith("could not queue FLATTEN_ALL")
    assert sup.flatten_id is None


def test_operator_pause_survives_the_ctrl_c_flatten(tmp_path):
    var = tmp_path / "var"
    _db(var, pause=True)
    sup, ctl, bot, out, _ = _supervisor(var)
    ctl.press("SIGINT")
    sup.step()
    _ack(var, sup.flatten_id, "FLATTEN_ALL", "DONE", "verified flat")
    sup.step()
    assert [r.kind for r in _sent(var)] == ["FLATTEN_ALL"]           # no RESUME: it would clear the PAUSE too
    assert bot.stop.reason == "CTRL_C" and "stopping cleanly" in out[-1]


def test_second_press_stops_now(tmp_path):
    var = tmp_path / "var"
    _db(var)
    sup, ctl, bot, out, _ = _supervisor(var)
    ctl.press("SIGINT")
    sup.step()
    _ack(var, sup.flatten_id, "FLATTEN_ALL", "ACCEPTED", "flattening")
    ctl.press("SIGINT")
    sup.step()
    assert bot.stop.reason == "CTRL_C_TWICE" and sup.flatten_id is None
    assert any("recovers and manages them" in line for line in out) and "FLATTEN halt stays" in out[-1]
    # two presses before the supervisor's next pass: no flatten is started at all
    var2 = tmp_path / "var2"
    _db(var2)
    sup, ctl, bot, out, _ = _supervisor(var2)
    ctl.press("SIGINT")
    ctl.press("SIGINT")
    sup.step()
    assert bot.stop.reason == "CTRL_C_TWICE" and not (var2 / "db" / cli.CLI_DB).exists()


def test_flatten_failed_then_done_and_rejected(tmp_path):
    var = tmp_path / "var"
    _db(var)
    sup, ctl, bot, out, mono = _supervisor(var)
    ctl.press("SIGINT")
    sup.step()
    _ack(var, sup.flatten_id, "FLATTEN_ALL", "FAILED", "not flat after 60 s: 1 structure(s) not closed")
    sup.step()
    sup.step()
    assert sum("the exit engine keeps trying" in line for line in out) == 1 and bot.stop.reason is None
    _ack(var, sup.flatten_id, "FLATTEN_ALL", "DONE", "verified flat")             # DONE later, as operator.py says
    sup.step()
    assert bot.stop.reason == "CTRL_C" and "verified flat" in out[-1] and sup.flatten_id is None
    var2 = tmp_path / "var2"
    _db(var2)
    sup, ctl, bot, out, _ = _supervisor(var2)
    ctl.press("SIGINT")
    sup.step()
    _ack(var2, sup.flatten_id, "FLATTEN_ALL", "REJECTED", "something odd")
    sup.step()
    assert bot.stop.reason == "CTRL_C" and "FLATTEN_ALL REJECTED" in out[-1]


def test_the_ctrl_c_flatten_is_a_stops_own():
    """core.operator's two rules for the runner's FLATTEN_ALL (EngineContext.stop_flatten_by, set by
    BotProcess(laptop=True)): it expires after EXPIRE_AFTER_S like a non-operator command, so it never runs at a later
    start; and is_stop_flatten() knows its halt by the reason start_flatten() writes, never an operator's FLATTEN or a
    PAUSE. Without a stop requester (EC2) nothing changes."""
    stop_by = f"CLI:{laptop.REQUESTED_BY}"

    def row(by: str, age_s: float) -> operator.CommandRow:
        return operator.CommandRow("CLI", 7, NOW - timedelta(seconds=age_s), by, "FLATTEN_ALL", {}, "FLATTEN", "n7")

    assert operator._problem(row(laptop.REQUESTED_BY, operator.EXPIRE_AFTER_S - 1), NOW, stop_by) is None
    assert operator._problem(row(laptop.REQUESTED_BY, operator.EXPIRE_AFTER_S + 1), NOW, stop_by) == "EXPIRED"
    assert operator._problem(row("opsuser", 86400), NOW, stop_by) is None           # an operator's waits for the bot
    assert operator._problem(row(laptop.REQUESTED_BY, 86400), NOW, None) is None   # EC2: no stop requester
    laptop_eng = SimpleNamespace(ctx=SimpleNamespace(stop_flatten_by=stop_by))
    ec2_eng = SimpleNamespace(ctx=SimpleNamespace(stop_flatten_by=None))

    def halt(kind: str, reason: str) -> Halt:
        return Halt(1, "GLOBAL", kind, reason, True, HaltClears.RESUME, NOW, S)

    ours = halt("FLATTEN", f"operator FLATTEN_ALL ({stop_by})")
    assert operator.is_stop_flatten(laptop_eng, ours) and not operator.is_stop_flatten(ec2_eng, ours)
    assert not operator.is_stop_flatten(laptop_eng, halt("FLATTEN", "operator FLATTEN_ALL (CLI:opsuser)"))
    assert not operator.is_stop_flatten(laptop_eng, halt("FLATTEN", "operator FLATTEN_ALL (DASH:opsuser)"))
    assert not operator.is_stop_flatten(laptop_eng, halt("PAUSE", f"operator PAUSE ({stop_by})"))


def test_supervisor_logs_power_and_watches_the_dashboard(tmp_path, caplog):
    states = iter([laptop.PowerStatus(on_ac=False, battery_pct=55), laptop.PowerStatus(on_ac=True, battery_pct=60)])
    sup, ctl, bot, out, mono = _supervisor(tmp_path / "var", power=lambda: next(states))
    dash = SimpleNamespace(poll=lambda: None, log_path=tmp_path / "dash.log")
    sup.dash = dash
    with caplog.at_level(logging.INFO, logger="titan2.laptop"):
        sup.step()                                           # at the start: on battery, a WARNING
        sup.step()
        mono[0] += laptop.POWER_LOG_EVERY_S
        sup.step()                                           # 15 min later: logged again
    power = [r for r in caplog.records if "power:" in r.getMessage()]
    assert [(r.levelname, "ON BATTERY" in r.getMessage()) for r in power] == [("WARNING", True), ("INFO", False)]
    assert out == []
    dash.poll = lambda: 1
    sup.step()
    sup.step()
    assert out == [f"dashboard stopped (exit 1): see {tmp_path / 'dash.log'}; trading is unaffected"]


def test_supervisor_thread_runs_and_stops(tmp_path):
    var = tmp_path / "var"
    sup, ctl, bot, out, _ = _supervisor(var, poll_s=0.01)
    sup.start()
    ctl.press("SIGINT")
    deadline = time.monotonic() + 5
    while sup.flatten_id is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sup.flatten_id is not None and bot.stop.reason is None       # the FLATTEN_ALL first, the stop after DONE
    _ack(var, sup.flatten_id, "FLATTEN_ALL", "DONE", "already flat (verified flat)")
    while bot.stop.reason is None and time.monotonic() < deadline:
        time.sleep(0.01)
    sup.stop()
    assert bot.stop.reason == "CTRL_C" and sup.finished.is_set() and not sup._thread.is_alive()


# ---------------------------------------------------------------------------------------------------------------
# The clock against internet time (network faked)
# ---------------------------------------------------------------------------------------------------------------
T1 = 1_791_000_000.25                                       # an epoch second in 2026


class FakeSock:
    """A UDP socket whose reply is built from the request (reply(request) -> bytes)."""

    def __init__(self, reply: Callable[[bytes], bytes]) -> None:
        self.reply, self.sent, self.timeout, self.closed = reply, [], None, False

    def settimeout(self, t: float) -> None:
        self.timeout = t

    def sendto(self, data: bytes, addr: tuple) -> None:
        self.sent.append((data, addr))

    def recvfrom(self, n: int) -> tuple[bytes, tuple]:
        return self.reply(self.sent[-1][0]), ("203.0.113.5", 123)

    def close(self) -> None:
        self.closed = True


def ntp_reply(offset: float, *, mode: int = 4, li: int = 0, stratum: int = 2, echo: bool = True,
              size: int = 48) -> Callable[[bytes], bytes]:
    """A server whose clock is `offset` ahead: receive at T1 + 0.1 (its own clock), transmit at once."""
    def reply(request: bytes) -> bytes:
        server = T1 + 0.1 + offset
        head = bytes([(li << 6) | (4 << 3) | mode, stratum, 6, 0xEC]) + b"\0" * 20
        origin = request[40:48] if echo else b"\0" * 8
        return (head + origin + laptop._ntp_stamp(server) + laptop._ntp_stamp(server))[:size]
    return reply


def test_ntp_offset(monkeypatch):
    times = iter([T1, T1 + 0.2])                             # a 200 ms round trip, centred on the server's stamps
    sock = FakeSock(ntp_reply(1.5))
    got = laptop.ntp_offset_s("time.windows.com", 2.0, sock=sock, now=lambda: next(times))
    assert isinstance(got, laptop.ClockOffset) and got == pytest.approx(1.5, abs=1e-6)
    assert got.error_s == pytest.approx(0.1, abs=1e-6) and got.source == "NTP time.windows.com"   # half the trip
    (data, addr), = sock.sent
    assert addr == ("time.windows.com", 123) and len(data) == 48 and data[0] == 0x23 and sock.closed
    assert sock.timeout == 2.0 and struct.unpack("!II", data[40:48])[0] == int(T1) + 2208988800
    for bad, why in ((ntp_reply(0.0, size=40), "short"), (ntp_reply(0.0, mode=3), "synchronized"),
                     (ntp_reply(0.0, li=3), "synchronized"), (ntp_reply(0.0, stratum=0), "synchronized"),
                     (ntp_reply(0.0, echo=False), "does not answer")):
        times = iter([T1, T1 + 0.2])
        with pytest.raises(ValueError, match=why):
            laptop.ntp_offset_s("h", 1.0, sock=FakeSock(bad), now=lambda: next(times))


def test_http_date_offset():
    seen = []

    def head(url: str, timeout: float, allow_redirects: bool) -> Any:
        seen.append((url, timeout, allow_redirects))
        return SimpleNamespace(headers={"Date": "Mon, 12 Oct 2026 03:30:10 GMT"})

    server = datetime.fromisoformat("2026-10-12T03:30:10+00:00").timestamp()
    times = iter([server - 3.0, server - 2.8])               # this clock is ~3 s behind (the header is whole seconds)
    got = laptop.http_date_offset_s("https://api.kite.trade", 2.5, head=head, now=lambda: next(times))
    assert got == pytest.approx(3.0 + 0.5 - 0.1) and seen == [("https://api.kite.trade", 2.5, False)]
    # the server's clock read [:10, :11) while the request ran: the offset lies in [2.8, 4.0], so 3.4 +- 0.6
    assert got.error_s == pytest.approx(0.5 + 0.1) and got.source == "the Date header of https://api.kite.trade"
    with pytest.raises(ValueError, match="no Date header"):
        laptop.http_date_offset_s("u", 1.0, head=lambda *a, **k: SimpleNamespace(headers={}))
    reading = laptop.ClockOffset(-0.25, 0.04, "x")            # a reading is a float, with its error and source
    assert reading == -0.25 and abs(reading) == 0.25 and (reading.error_s, reading.source) == (0.04, "x")
    assert laptop.ClockOffset(1.0).error_s == 0.0


def test_measure_clock_falls_back_and_never_raises(monkeypatch):
    tried = []

    def ntp(host: str, timeout_s: float) -> float:
        tried.append(host)
        if host == "time.windows.com":
            raise TimeoutError("timed out")
        return 0.25

    monkeypatch.setattr(laptop, "ntp_offset_s", ntp)
    monkeypatch.setattr(laptop, "http_date_offset_s", lambda url, t: pytest.fail("NTP answered"))
    assert laptop.measure_clock(1.0) == (0.25, "NTP pool.ntp.org")
    assert tried == [h for h in laptop.NTP_HOSTS for _ in range(laptop.NTP_SAMPLES)]     # 3 samples a host
    assert laptop.clock_offset_s() == 0.25                   # the self-check calls it without arguments
    monkeypatch.setattr(laptop, "ntp_offset_s", lambda host, t: (_ for _ in ()).throw(OSError("unreachable")))
    monkeypatch.setattr(laptop, "http_date_offset_s", lambda url, t: -0.75)
    assert laptop.measure_clock(1.0) == (-0.75, f"the Date header of {laptop.TIME_URL}")
    monkeypatch.setattr(laptop, "http_date_offset_s", lambda url, t: (_ for _ in ()).throw(ValueError("no Date")))
    offset, why = laptop.measure_clock(1.0)
    assert offset is None and "no time source answered" in why and "unreachable" in why and "no Date" in why
    assert laptop.clock_offset_s(0.5) is None


# ---------------------------------------------------------------------------------------------------------------
# The token step and the self-check step
# ---------------------------------------------------------------------------------------------------------------
CREDS = {"ZERODHA_API_KEY": "kiteapikey01", "ZERODHA_USER_ID": "AB1234", **SECRETS}
TODAY_0850 = ist_datetime(S, "08:50")


class ProfileKite:
    def __init__(self, user: str = "AB1234") -> None:
        self.user = user

    def profile(self) -> dict:
        return {"user_id": self.user}


def test_ensure_token_uses_todays_session_when_it_works(tmp_path):
    var = tmp_path / "var"
    token_mod.write_session(token_mod.new_session("abcdef123456", "AB1234", ist_datetime(S, "08:30")),
                            token_mod.session_path(var))
    out: list[str] = []
    ok = laptop.ensure_token(CREDS, var, out=out.append, run=lambda **kw: pytest.fail("no login needed"),
                             kite_factory=lambda *a, **k: ProfileKite(), now=TODAY_0850)
    assert ok and out == ["token: today's Kite session works"]
    # yesterday's session does not count: a login follows
    calls = []
    ok = laptop.ensure_token(CREDS, var, out=out.append, run=lambda **kw: calls.append(kw) or 0,
                             kite_factory=lambda *a, **k: ProfileKite(), now=TODAY_0850 + timedelta(days=1))
    assert ok and calls and calls[0]["env"]["TOKEN_MODE"] == "auto" and "request_token" not in calls[0]


def test_ensure_token_auto_then_manual(tmp_path):
    var = tmp_path / "var"
    calls: list[dict] = []
    codes = iter([2, 0])
    out: list[str] = []
    asked: list[str] = []

    def run(**kw: Any) -> int:
        calls.append(kw)
        return next(codes)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "  https://127.0.0.1/?action=login&type=login&status=success&request_token=abcDEF123456  "

    assert laptop.ensure_token(CREDS, var, out=out.append, ask=ask, run=run, now=TODAY_0850)
    assert [c["env"].get("TOKEN_MODE") for c in calls] == ["auto", None] and asked == ["request_token: "]
    assert calls[0]["path"] == token_mod.session_path(var) and calls[0]["notify"] is laptop._quiet
    assert calls[1]["request_token"] == "abcDEF123456"
    assert any("kite.zerodha.com/connect/login?api_key=kiteapikey01" in line for line in out)
    text = "\n".join(out)
    assert not any(v in text for v in SECRETS.values())     # names and the API key only, never a secret


def test_ensure_token_failure_paths(tmp_path):
    var = tmp_path / "var"
    out: list[str] = []
    never = lambda **kw: pytest.fail("no Kite call expected")     # noqa: E731
    no_login = {k: v for k, v in CREDS.items() if k not in ("ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET")}
    # no password/TOTP: straight to the manual flow; Enter alone skips it
    assert not laptop.ensure_token(no_login, var, out=out.append, ask=lambda p: "", run=never, now=TODAY_0850)
    assert "needs ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET" in out[0] and "skipped" in out[-1]
    # not interactive (Task Scheduler, a redirected run): no prompt
    assert not laptop.ensure_token(no_login, var, out=out.append, ask=None, run=never, now=TODAY_0850)
    assert "--request-token" in out[-1]
    # nothing to log in with at all
    assert not laptop.ensure_token({"ZERODHA_USER_ID": "AB1234"}, var, out=out.append, ask=lambda p: "x",
                                   run=never, now=TODAY_0850)
    assert "HALT TOKEN" in out[-1] and "ZERODHA_API_KEY" in out[-1]
    # TOKEN_MODE=manual: no automatic attempt; a refused request token, and an EOF, both leave HALT(TOKEN)
    calls = []
    manual = dict(CREDS, TOKEN_MODE="manual")
    assert not laptop.ensure_token(manual, var, out=out.append, ask=lambda p: "abcDEF123456",
                                   run=lambda **kw: calls.append(kw) or 2, now=TODAY_0850)
    assert [c.get("request_token") for c in calls] == ["abcDEF123456"] and "refused" in out[-1]

    def eof(prompt: str) -> str:
        raise EOFError

    assert not laptop.ensure_token(manual, var, out=out.append, ask=eof, run=never, now=TODAY_0850)
    assert laptop.request_token_in("!!bad!!") is None and laptop.request_token_in("abc123XYZ") == "abc123XYZ"


class FakeReport:
    def __init__(self, ok: bool, halts: list) -> None:
        self.ok, self._halts = ok, halts

    def halts(self) -> list:
        return self._halts

    def summary(self, verbose: bool = False) -> str:
        return f"titan2 self-check laptop: {'PASS' if self.ok else 'FAIL'}"


def test_run_selfcheck_returns_its_halts(tmp_path):
    seen: dict = {}
    spec = selfcheck.HaltSpec("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO, "selfcheck laptop: clock: +1.2 s")

    def run_phase(phase: str, ctx: Any) -> FakeReport:
        seen.update(phase=phase, env_file=ctx.env_file, var=ctx.var_dir, now=ctx.now, env=ctx.env)
        return FakeReport(False, [spec])

    out: list[str] = []
    env = dict(CREDS, TITAN2_PROFILE="laptop")
    halts = laptop.run_selfcheck(env, tmp_path / "var", tmp_path / "t.env", now=TODAY_0850, calendar=CAL,
                                 out=out.append, run_phase=run_phase)
    assert halts == [("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO, "selfcheck laptop: clock: +1.2 s")]
    assert (seen["phase"], seen["env_file"], seen["var"], seen["now"]) == ("laptop", tmp_path / "t.env",
                                                                           tmp_path / "var", TODAY_0850)
    assert out[0].endswith("FAIL") and "blocks new entries" in out[1]

    def broken(phase: str, ctx: Any) -> Any:
        raise RuntimeError("selfcheck exploded")

    (halt,) = laptop.run_selfcheck(env, tmp_path / "var", tmp_path / "t.env", now=TODAY_0850, calendar=CAL,
                                   out=out.append, run_phase=broken)
    assert halt[:3] == ("CLOCK_UNSYNCED", "GLOBAL", "AUTO") and "did not run" in halt[3]     # fails closed
    assert "did not run" in out[-1]


# ---------------------------------------------------------------------------------------------------------------
# The day runner (the bot faked)
# ---------------------------------------------------------------------------------------------------------------
def make_factory(codes: list[int], on_run: Callable[[Any, int], Any] | None = None) -> tuple[Callable, list]:
    made: list = []

    def factory(**kw: Any) -> Any:
        bot = SimpleNamespace(stop=runtime.StopRequest(), kwargs=kw)
        n = len(made)

        def run() -> int:
            if on_run is not None:
                on_run(bot, n)
            return codes[n]

        bot.run = run
        made.append(bot)
        return bot

    return factory, made


def day(tmp_path: Path, factory: Callable, *, when: datetime = TODAY_0850, steps: list | None = None,
        token_ok: bool = True, halts: list | None = None, kernel32: FakeKernel32 | None = None,
        out: list | None = None, **kw: Any) -> int:
    steps = [] if steps is None else steps

    def token_step(env: dict, var: Path, *, out: Any, ask: Any) -> bool:
        steps.append(("token", env, var))
        return token_ok

    def selfcheck_step(env: dict, var: Path, env_file: Path, *, now: datetime, calendar: Any, out: Any) -> list:
        steps.append(("selfcheck", env_file, now))
        return list(halts or [])

    kw.setdefault("handlers", False)
    return laptop.run_day(lap_env(tmp_path), out=(out if out is not None else []).append, clock=FakeClock(when),
                          calendar=CAL, dash=False, cli_mod=cli, bot_factory=factory, token_step=token_step,
                          selfcheck_step=selfcheck_step, keep_awake=laptop.KeepAwake(kernel32=kernel32 or
                                                                                     FakeKernel32()),
                          power=lambda: laptop.PowerStatus(on_ac=True, battery_pct=99), sleep=lambda s: None, **kw)


def test_day_runner_sequence(tmp_path):
    """Trading day -> token -> laptop self-check -> the bot in PAPER (the self-check's halts at its boot, keep-awake
    on while it runs) -> the report."""
    k, steps, out = FakeKernel32(), [], []
    halt = ("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO, "selfcheck laptop: clock: +1.2 s")
    awake_during: list = []
    factory, made = make_factory([0], on_run=lambda bot, n: awake_during.append(list(k.states)))
    assert day(tmp_path, factory, steps=steps, halts=[halt], kernel32=k, out=out) == 0
    assert [s[0] for s in steps] == ["token", "selfcheck"]
    assert steps[0][2] == tmp_path / "var" and steps[1][1] == tmp_path / "titan2.env" and steps[1][2] == TODAY_0850
    (bot,) = made
    kw = bot.kwargs
    assert kw["laptop"] is True and kw["install_signals"] is False and kw["boot_halts"] == [halt]
    assert kw["env"]["TITAN2_PROFILE"] == "laptop" and kw["env"]["TITAN2_VAR"] == str(tmp_path / "var")
    assert kw["calendar"] is CAL and kw["clock"].today() == S
    assert awake_during == [[ON]] and k.states == [ON, OFF]                # on during the session, off after
    text = "\n".join(out)
    assert f"{S}: trading session 09:15-15:40" in text and "the trading day is done" in text
    assert "no env file at" in text                                        # the operator learns what to create


def test_day_runner_skips_a_non_trading_day(tmp_path):
    k, steps, out = FakeKernel32(), [], []
    factory, made = make_factory([0])
    for when in (ist_datetime(SAT, "09:00"), ist_datetime(date(2026, 10, 20), "09:00")):     # weekend, holiday
        assert day(tmp_path, factory, when=when, steps=steps, kernel32=k, out=out) == 0
    assert steps == [] and made == [] and k.states == []
    assert "no NSE trading session: nothing to do" in out[-1]
    assert day(tmp_path, factory, when=ist_datetime(date(2030, 1, 2), "09:00"), out=out) == 0
    assert "beyond the calendar" in out[-1]


def test_token_failure_blocks_entries_but_the_session_still_runs(tmp_path):
    """A failed token step does not stop the day: the bot starts (recovery, exits) and raises HALT(TOKEN) itself;
    the self-check's halts go to its boot. --skip-token skips the step."""
    steps: list = []
    factory, made = make_factory([0])
    token_halt = ("TOKEN", "GLOBAL", HaltClears.AUTO, "selfcheck laptop: token: no session")
    assert day(tmp_path, factory, steps=steps, token_ok=False, halts=[token_halt]) == 0
    assert len(made) == 1 and made[0].kwargs["boot_halts"] == [token_halt]
    steps.clear()
    out: list[str] = []
    factory, made = make_factory([0])
    assert day(tmp_path, factory, steps=steps, skip_token=True, out=out) == 0
    assert [s[0] for s in steps] == ["selfcheck"] and "--skip-token" in "\n".join(out) and len(made) == 1
    # the real token step without any credential: False, and the bot still runs
    factory, made = make_factory([0])
    out = []
    code = laptop.run_day(lap_env(tmp_path), out=out.append, clock=FakeClock(TODAY_0850), calendar=CAL, dash=False,
                          cli_mod=cli, bot_factory=factory, selfcheck_step=lambda *a, **k: [],
                          keep_awake=laptop.KeepAwake(kernel32=FakeKernel32()), handlers=False)
    assert code == 0 and len(made) == 1 and any("HALT TOKEN" in line for line in out)


def test_day_runner_restarts_like_systemd(tmp_path):
    """No systemd on the laptop: exit 75 restarts at once, exit 3 up to CRASH_RESTARTS times; only the first run
    raises the self-check's halts; 73 and 78 stop."""
    halt = ("CLOCK_UNSYNCED", "GLOBAL", "AUTO", "x")
    out: list[str] = []
    factory, made = make_factory([75, 3, 0])
    assert day(tmp_path, factory, halts=[halt], out=out) == 0
    assert len(made) == 3 and [b.kwargs["boot_halts"] for b in made] == [[halt], [], []]
    text = "\n".join(out)
    assert "controlled restart" in text and "crashed (exit 3): restart 1 of 5" in text
    factory, made = make_factory([3] * 10)
    out = []
    assert day(tmp_path, factory, out=out) == runtime.EXIT_EXCEPTION_STORM
    assert len(made) == 1 + laptop.CRASH_RESTARTS and "NOT managed" in out[-1]
    for code, words in ((runtime.EXIT_LOCKED, "another titan2 already runs"), (runtime.EXIT_FATAL, "exit 78")):
        factory, made = make_factory([code])
        out = []
        assert day(tmp_path, factory, out=out) == code and len(made) == 1 and words in "\n".join(out)


def test_a_stop_request_is_never_restarted(tmp_path):
    """Ctrl+C twice during a run that then ends with 75: no restart. The real signal handlers are installed for the
    session and restored afterwards."""
    before = signal.getsignal(signal.SIGINT)
    seen: list = []

    def press_twice(bot: Any, n: int) -> None:
        signal.raise_signal(signal.SIGINT)                    # the handlers run on this (the main) thread
        signal.raise_signal(signal.SIGINT)
        seen.append(bot.stop.reason)

    factory, made = make_factory([75, 0], on_run=press_twice)
    out: list[str] = []
    assert day(tmp_path, factory, handlers=True, out=out) == runtime.EXIT_OK      # never "restart me" after Ctrl+C
    assert len(made) == 1 and seen == ["CTRL_C_TWICE"] and signal.getsignal(signal.SIGINT) is before
    assert "titan2 stopped cleanly (CTRL_C_TWICE)" in "\n".join(out)


def test_a_second_laptop_run_stops_before_any_login(tmp_path):
    """Another titan2 holds the instance lock: stop at once (exit 73), before the token step (a second login would
    rotate the running bot's session) and before the self-check."""
    assert laptop.EXIT_LOCKED == runtime.EXIT_LOCKED
    held = runtime.InstanceLock(tmp_path / "var" / "run" / runtime.LOCK_NAME)
    assert held.acquire()
    steps: list = []
    out: list[str] = []
    factory, made = make_factory([0])
    try:
        assert day(tmp_path, factory, steps=steps, out=out) == runtime.EXIT_LOCKED
    finally:
        held.release()
    assert steps == [] and made == [] and "another titan2 already runs" in out[-1]
    assert not laptop.another_runs(tmp_path / "var")                  # free again (and the probe released it)
    assert day(tmp_path, make_factory([0])[0], steps=steps) == 0 and [s[0] for s in steps] == ["token", "selfcheck"]


def test_a_failing_session_still_cleans_up(tmp_path):
    """Whatever the bot does, the run never ends half-stopped: keep-awake cleared, the supervisor thread stopped,
    the dashboard child terminated, the previous signal handlers back."""
    before = signal.getsignal(signal.SIGINT)
    k = FakeKernel32()
    killed: list = []
    child = SimpleNamespace(poll=lambda: None, terminate=lambda: killed.append(True), wait=lambda t: 0)

    def factory(**kw: Any) -> Any:
        raise RuntimeError("cannot build the bot")

    with pytest.raises(RuntimeError, match="cannot build the bot"):
        laptop.run_day(lap_env(tmp_path), out=lambda s: None, clock=FakeClock(TODAY_0850), calendar=CAL,
                       skip_token=True, cli_mod=cli, bot_factory=factory, selfcheck_step=lambda *a, **kw: [],
                       keep_awake=laptop.KeepAwake(kernel32=k), handlers=True, popen=lambda cmd, **kw: child)
    assert k.states == [ON, OFF] and killed == [True] and signal.getsignal(signal.SIGINT) is before
    assert not [t for t in threading.enumerate() if t.name == "laptop" and t.is_alive()]


def test_ctrl_c_before_the_session_exits_130(tmp_path):
    k = FakeKernel32()
    factory, made = make_factory([0])

    def token_step(*a: Any, **kw: Any) -> bool:
        raise KeyboardInterrupt

    out: list[str] = []
    code = laptop.run_day(lap_env(tmp_path), out=out.append, clock=FakeClock(TODAY_0850), calendar=CAL, dash=False,
                          cli_mod=cli, bot_factory=factory, token_step=token_step,
                          keep_awake=laptop.KeepAwake(kernel32=k), handlers=False)
    assert code == laptop.EXIT_INTERRUPTED and made == [] and k.states == [] and "nothing was traded" in out[-1]


def test_allow_live_is_announced_as_ignored(tmp_path):
    out: list[str] = []
    factory, _ = make_factory([0])
    laptop.run_day(lap_env(tmp_path, ALLOW_LIVE="1"), out=out.append, clock=FakeClock(ist_datetime(SAT, "09:00")),
                   calendar=CAL, dash=False, cli_mod=cli, bot_factory=factory, handlers=False)
    assert any("ALLOW_LIVE=1 is ignored" in line and "static IP" in line for line in out)


# ---------------------------------------------------------------------------------------------------------------
# The dashboard child process
# ---------------------------------------------------------------------------------------------------------------
def test_start_dash_isolated_child(tmp_path):
    calls: list = []

    class Proc:
        pid = 4242

        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> Any:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            return 0

    def popen(cmd: list, **kw: Any) -> Proc:
        calls.append((cmd, kw))
        return Proc()

    env = dict(CREDS, TELEGRAM_BOT_TOKEN="tg", HEALTHCHECK_URL="https://hc", TITAN2_ENV_FILE="x.env",
               DASH_USER="me", PATH="p")
    out: list[str] = []
    proc = laptop.start_dash(env, tmp_path / "var", tmp_path / "logs", S, out=out.append, popen=popen)
    ((cmd, kw),) = calls
    assert cmd == [sys.executable, "-m", "titan2.dash.app", "serve", "--laptop", "--host", "127.0.0.1", "--port",
                   "5000"]
    child = kw["env"]
    assert not [k for k in child if k.startswith(("ZERODHA_", "TELEGRAM_"))] and "HEALTHCHECK_URL" not in child
    assert "TITAN2_ENV_FILE" not in child and child["DASH_USER"] == "me" and child["PATH"] == "p"
    assert child["TITAN2_VAR"] == str(tmp_path / "var") and child["TITAN2_PROFILE"] == "laptop"
    assert kw["cwd"] == str(laptop.APP_DIR)
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)   # Ctrl+C is the bot's alone
    assert proc.log_path == tmp_path / "logs" / f"dash-{S}.log" and "http://127.0.0.1:5000" in out[0]
    laptop.stop_dash(proc)
    assert proc.terminated

    def failing(cmd: list, **kw: Any) -> Any:
        raise OSError("no python")

    assert laptop.start_dash(env, tmp_path / "var", tmp_path / "logs", S, out=out.append, popen=failing) is None
    assert "trading is unaffected" in out[-1]
    laptop.stop_dash(None)


# ---------------------------------------------------------------------------------------------------------------
# The CLI verbs
# ---------------------------------------------------------------------------------------------------------------
def test_laptop_verb_builds_the_laptop_env(tmp_path, monkeypatch):
    """`titan2 laptop` needs no systemd and no override; it merges the laptop env file under the environment, pins
    the profile and the paths, and passes --date / --no-dash / --skip-token on."""
    got: dict = {}
    monkeypatch.setattr(laptop, "run_day", lambda env, **kw: got.update(env=env, **kw) or 0)
    env_file = tmp_path / "titan2.env"
    env_file.write_text("ZERODHA_API_KEY=fromfile\nZERODHA_USER_ID=AB1234\nZERODHA_API_SECRET=s3cr3t-value\n",
                        encoding="utf-8")
    env = lap_env(tmp_path, ZERODHA_USER_ID="ZZ9999")
    target = RealClock().today() + timedelta(days=3)
    profile_before = os.environ.get("TITAN2_PROFILE")
    assert cli.main(["laptop", "--date", target.isoformat(), "--no-dash", "--skip-token"], env=env,
                    out=lambda s: None) == 0
    e = got["env"]
    assert e["ZERODHA_API_KEY"] == "fromfile" and e["ZERODHA_USER_ID"] == "ZZ9999"         # the environment wins
    # a --date run has its own state and logs (review round 3: it ran in the real state dir)
    sim = tmp_path / "sim" / target.isoformat()
    assert e["TITAN2_PROFILE"] == "laptop" and e["TITAN2_VAR"] == str(sim / "var")
    assert e["TITAN2_LOG_DIR"] == str(sim / "logs") and e["TITAN2_ENV_FILE"] == str(env_file)
    assert got["dash"] is False and got["skip_token"] is True and got["ask"] is None and got["cli_mod"] is cli
    assert isinstance(got["clock"], ShiftedClock) and got["clock"].today() == target
    assert os.environ.get("TITAN2_PROFILE") == profile_before      # a given env never touches the process's
    got.clear()
    assert cli.main(["laptop"], env=env, out=lambda s: None) == 0
    assert got["dash"] is True and got["skip_token"] is False and isinstance(got["clock"], RealClock)
    assert got["env"]["TITAN2_VAR"] == str(tmp_path / "var") and got["env"]["TITAN2_LOG_DIR"] == str(tmp_path / "logs")
    out: list[str] = []
    assert cli.main(["laptop", "--date", "12-10-2026"], env=env, out=out.append) == cli.EXIT_FAILED
    assert out[-1].startswith("refused: --date")


def test_a_date_run_never_uses_the_real_state_dir(tmp_path, monkeypatch):
    """Review round 3 (money lens): `titan2 laptop --date D` ran in the real state dir, so a weekend rehearsal for
    Monday was session Monday in titan2.db and paper_exchange.db: its heartbeat gap flagged the real Monday IMPAIRED,
    its trade became Monday's G1 evidence and its 15:26 marks skipped the real Monday's paper square-off. A --date run
    keeps its own state and logs (sim\\<D> next to the state dir), seeded with today's Kite session and the settings;
    the real state dir is only read. TITAN2_SIM_DIR, the explicit override, may name any folder but the real one, and
    a rehearsal session is refused while a titan2 runs on the real state dir (it could log in to Kite again)."""
    got: dict = {}

    def bot_day(env, **kw):                           # the rehearsal's bot writes where it was told to
        got.update(env=env, **kw)
        db = Path(env["TITAN2_VAR"]) / "db"
        db.mkdir(parents=True, exist_ok=True)
        (db / "titan2.db").write_text("rehearsal", encoding="utf-8")
        return 0

    monkeypatch.setattr(laptop, "run_day", bot_day)
    real = tmp_path / "var"
    for sub, name, text in (("session", "kite_session.json", '{"access_token": "today"}'),
                            ("config", "settings.json", "{}"), ("db", "titan2.db", "the real day")):
        (real / sub).mkdir(parents=True, exist_ok=True)
        (real / sub / name).write_text(text, encoding="utf-8")
    before = sorted(p.relative_to(real).as_posix() for p in real.rglob("*"))
    env = lap_env(tmp_path)
    day = RealClock().today() + timedelta(days=2)
    out: list[str] = []
    assert cli.main(["laptop", "--date", day.isoformat(), "--no-dash", "--skip-token"], env=env,
                    out=out.append) == 0
    sim = tmp_path / "sim" / day.isoformat()
    assert (got["env"]["TITAN2_VAR"], got["env"]["TITAN2_LOG_DIR"]) == (str(sim / "var"), str(sim / "logs"))
    assert (sim / "var" / "session" / "kite_session.json").read_text(encoding="utf-8") == '{"access_token": "today"}'
    assert (sim / "var" / "config" / "settings.json").is_file() and (sim / "var" / "db" / "titan2.db").is_file()
    assert (real / "db" / "titan2.db").read_text(encoding="utf-8") == "the real day"
    assert sorted(p.relative_to(real).as_posix() for p in real.rglob("*")) == before    # nothing written there
    assert any(f"state {sim / 'var'}" in line and "never touched" in line for line in out), out
    # TITAN2_SIM_DIR names the simulations' folder; one that would land on the real state dir is refused
    got.clear()
    other = tmp_path / "rehearsals"
    assert cli.main(["laptop", "--date", day.isoformat()], env=dict(env, TITAN2_SIM_DIR=str(other)),
                    out=out.append) == 0
    assert got["env"]["TITAN2_VAR"] == str(other / day.isoformat() / "var")
    got.clear()
    loop = dict(env, TITAN2_VAR=str(tmp_path / "loop" / day.isoformat() / "var"), TITAN2_SIM_DIR=str(tmp_path / "loop"))
    assert cli.main(["laptop", "--date", day.isoformat()], env=loop, out=out.append) == cli.EXIT_FAILED
    assert got == {} and out[-1].startswith("refused: --date") and "real state dir" in out[-1]
    # a titan2 running on the real state dir: no rehearsal session next to it (a check is still fine)
    held = runtime.InstanceLock(real / "run" / runtime.LOCK_NAME)
    assert held.acquire()
    try:
        assert cli.main(["laptop", "--date", day.isoformat()], env=env, out=out.append) == laptop.EXIT_LOCKED
        assert got == {} and "Run it after the session" in out[-1]
    finally:
        held.release()
    # today's date is no simulation: the real dirs
    assert cli.main(["laptop", "--date", RealClock().today().isoformat()], env=env, out=out.append) == 0
    assert got["env"]["TITAN2_VAR"] == str(real) and isinstance(got["clock"], RealClock)


def test_laptop_check_verb_runs_only_the_selfcheck(tmp_path, monkeypatch):
    got: dict = {}
    monkeypatch.setattr(laptop, "run_day", lambda *a, **k: pytest.fail("no session for `laptop check`"))

    def fake_cli(phase: str, **kw: Any) -> int:
        got.update(phase=phase, **kw)
        return 2

    monkeypatch.setattr(selfcheck, "cli", fake_cli)
    out: list[str] = []
    assert cli.main(["laptop", "check"], env=lap_env(tmp_path), out=out.append) == 2
    assert got["phase"] == "laptop" and got["var_dir"] == tmp_path / "var"
    assert got["env_file"] == tmp_path / "titan2.env" and got["env"]["TITAN2_PROFILE"] == "laptop"
    assert got["out"] == out.append

    def interrupted(phase: str, **kw: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(selfcheck, "cli", interrupted)
    assert cli.main(["laptop", "check"], env=lap_env(tmp_path), out=out.append) == laptop.EXIT_INTERRUPTED


def test_dash_verb_serves_through_the_dashboard_module(monkeypatch):
    from titan2.dash import app as dash_app

    calls: list = []
    monkeypatch.setattr(dash_app, "serve", lambda host, port, laptop_flag, **kw: calls.append((host, port,
                                                                                               laptop_flag, kw)) or 0)
    monkeypatch.delenv("TITAN2_PROFILE", raising=False)
    out: list[str] = []
    assert cli.main(["dash"], out=out.append) == 0
    assert calls[-1][:3] == ("127.0.0.1", 5000, False) and calls[-1][3]["allow_public"] is False
    assert "env" not in calls[-1][3]                        # never the bot's env: serve() reads its own file
    monkeypatch.setenv("TITAN2_PROFILE", "laptop")
    assert cli.main(["dash", "--host", "0.0.0.0", "--port", "5050", "--allow-public"], out=out.append) == 0
    assert calls[-1][:3] == ("0.0.0.0", 5050, True) and calls[-1][3]["allow_public"] is True


def test_bot_verb_guard_unchanged_and_laptop_verb_needs_no_override(tmp_path, monkeypatch):
    for k in ("INVOCATION_ID", "NOTIFY_SOCKET"):
        monkeypatch.delenv(k, raising=False)
    out: list[str] = []
    assert cli.main(["bot"], env={"TITAN2_VAR": str(tmp_path)}, out=out.append) == cli.EXIT_FAILED
    assert "--outside-systemd" in out[-1]
    monkeypatch.setattr(laptop, "run_day", lambda env, **kw: 0)
    assert cli.main(["laptop"], env=lap_env(tmp_path), out=out.append) == 0


# ---------------------------------------------------------------------------------------------------------------
# What the bot session needs on Windows: logging, the reactor kept across in-process runs
# ---------------------------------------------------------------------------------------------------------------
def test_setup_logging_file_handler_and_console_flag(tmp_path):
    root = logging.getLogger()
    level = root.level
    try:
        path = runtime.setup_logging(tmp_path / "logs", filename="laptop-2026-10-12.log", console=False)
        mine = [h for h in root.handlers if getattr(h, runtime._LOG_MARK, False)]
        assert path == tmp_path / "logs" / "laptop-2026-10-12.log" and len(mine) == 1
        want = logging.handlers.WatchedFileHandler if os.name == "posix" else logging.FileHandler
        assert type(mine[0]) is want
        logging.getLogger("titan2.test").warning("hello laptop")
        mine[0].flush()
        assert "hello laptop" in path.read_text(encoding="utf-8")
        runtime.setup_logging(tmp_path / "logs")
        mine = [h for h in root.handlers if getattr(h, runtime._LOG_MARK, False)]
        assert len(mine) == 2 and any(getattr(h, "stream", None) is sys.stdout for h in mine)
    finally:
        for h in [h for h in root.handlers if getattr(h, runtime._LOG_MARK, False)]:
            root.removeHandler(h)
            h.close()
        root.setLevel(level)


class RecTicker:
    def __init__(self) -> None:
        self.calls: list = []
        self.websocket_thread = None

    def connect(self, threaded: bool = False) -> None:
        self.calls.append(("connect", threaded, threading.current_thread().name))


class RunningReactor:
    """A reactor left running by an earlier in-process run: callFromThread runs the work on its own thread."""

    running = True

    def __init__(self) -> None:
        self.calls: list = []

    def callFromThread(self, fn: Callable, *args: Any, **kwargs: Any) -> None:
        self.calls.append(fn)
        t = threading.Thread(target=fn, args=args, kwargs=kwargs, name="fake-reactor")
        t.start()
        t.join(5)


def test_feed_connects_on_a_reactor_that_is_already_running():
    """A Twisted reactor cannot run twice, so the laptop's in-process restart reuses the first run's: KiteTicker's
    connect (connectWS) must then run on the reactor thread. A reactor that is not running is untouched (Linux)."""
    ticker, reactor = RecTicker(), RunningReactor()
    clock = FakeClock(ist_datetime(S, "09:00"))
    feed = Feed(ticker, QuoteCache(mono=clock.mono), lambda ev: None, SignalQueue(), {}.get, clock=clock,
                reactor=reactor)
    feed.start()
    assert ticker.calls == [("connect", True, "fake-reactor")] and reactor.calls == [ticker.connect]
    ticker2 = RecTicker()

    class Idle:
        def callFromThread(self, fn: Callable, *a: Any, **k: Any) -> None:
            pytest.fail("nothing goes through the reactor before it runs")

    Feed(ticker2, QuoteCache(mono=clock.mono), lambda ev: None, SignalQueue(), {}.get, clock=clock,
         reactor=Idle()).start()
    assert ticker2.calls == [("connect", True, threading.current_thread().name)]


def test_runtime_shutdown_keeps_the_reactor_for_the_laptop():
    stops: list = []
    feed = SimpleNamespace(stop=lambda **kw: stops.append(kw))
    rt = runtime.Runtime(store=None, ctx=None, clock=FakeClock(ist_datetime(S, "10:00")), feed=feed)
    rt.feed_started = True
    rt.shutdown()
    rt.keep_reactor = True
    rt.shutdown()
    assert stops == [{}, {"stop_reactor": False}]


# ---------------------------------------------------------------------------------------------------------------
# One short laptop session, in-process, on the real BotProcess with test fakes
# ---------------------------------------------------------------------------------------------------------------
class ScaledClock(Clock):
    """Wall and monotonic time running `rate` times faster than real time."""

    def __init__(self, start: datetime, rate: float = 4.0) -> None:
        self._t0, self._start, self._rate = time.monotonic(), start, rate

    def mono(self) -> float:
        return 1000.0 + (time.monotonic() - self._t0) * self._rate

    def now_wall(self) -> datetime:
        return self._start + timedelta(seconds=(time.monotonic() - self._t0) * self._rate)


class TradingKite:
    """An empty, healthy LIVE account whose instruments dump is unavailable (the engine halts NO_INSTRUMENTS)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def _note(self, name: str) -> None:
        with self.lock:
            self.calls.append(name)

    def profile(self) -> dict:
        self._note("profile")
        return {"user_id": "AB1234", "user_name": "test"}

    def orders(self) -> list:
        self._note("orders")
        return []

    def positions(self) -> dict:
        self._note("positions")
        return {"net": [], "day": []}

    def trades(self) -> list:
        self._note("trades")
        return []

    def instruments(self, exchange: str) -> list:
        self._note(f"instruments:{exchange}")
        raise OSError("instruments dump unavailable in this test")

    def place_order(self, **kw: Any) -> str:                # what would reach api.kite.trade: never on the laptop
        self._note("place_order")
        return "261012000000099"

    def cancel_order(self, **kw: Any) -> str:
        self._note("cancel_order")
        return str(kw.get("order_id"))


class FakeTicker:
    def __init__(self) -> None:
        self.subscribed_tokens: dict = {}
        self.calls: list[tuple] = []
        self.websocket_thread = None

    def connect(self, threaded: bool = False) -> None:
        self.calls.append(("connect", threaded))
        self.on_connect(None, None)

    def subscribe(self, tokens: list) -> None:
        self.calls.append(("subscribe", tuple(tokens)))

    def set_mode(self, mode: str, tokens: list) -> None:
        self.calls.append(("set_mode", mode, tuple(tokens)))

    def unsubscribe(self, tokens: list) -> None:
        self.calls.append(("unsubscribe", tuple(tokens)))

    def close(self) -> None:
        self.calls.append(("close",))


class InlineReactor:
    def callFromThread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        fn(*args, **kwargs)

    def stop(self) -> None:
        pytest.fail("the laptop keeps the reactor running")


@pytest.mark.parametrize("token_ok", [True, False])
def test_laptop_session_in_process(tmp_path, monkeypatch, caplog, token_ok):
    """`titan2 laptop` end to end on the real BotProcess (lanes, paper-sim, prep, alerts, WS feed, signals, engine)
    with fake Kite: the trading-day check, the token step skipped, the self-check's halt at boot, PAPER only although
    ALLOW_LIVE=1 (the real order lane refuses a LIVE exit and a LIVE cancel: nothing reaches Kite), READY, the
    in-bot self-check as phase "laptop"; then a real SIGINT: nothing is open, yet the FLATTEN_ALL goes out (its halt
    stops entries until the stop), DONE at once, a clean stop (SHUTDOWN EXITED, exit 0) that clears that halt without
    marking the session IMPAIRED, keep-awake set and cleared, the battery warned about, the daily report printed.
    Without a session file the bot still recovers and reaches READY (exits would run), under HALT(TOKEN)."""
    var = tmp_path / "var"
    if token_ok:
        token_mod.write_session(token_mod.new_session("abcdef123456", "AB1234", ist_datetime(S, "08:30")),
                                token_mod.session_path(var))
    env = lap_env(tmp_path, ZERODHA_API_KEY="testkey", ALLOW_LIVE="1")
    rec = RecordingAlerter()
    monkeypatch.setattr(runtime.alerts.Alerter, "from_env", lambda *a, **k: rec)
    phases: list = []

    def in_bot_selfcheck(phase: str, ctx: Any) -> Any:      # the real rows probe the host and the network
        phases.append((phase, ctx.env_file, ctx.env.get("TITAN2_PROFILE")))
        raise RuntimeError("not run in this test")

    monkeypatch.setattr(runtime.selfcheck, "run_phase", in_bot_selfcheck)
    kite, ticker, seen = TradingKite(), FakeTicker(), {}
    runtimes: list[runtime.Runtime] = []
    ban_text = f"Securities in Ban For Trade Date {S:%d-%b-%Y}:\nNIL\n".upper()
    sell = OrderRequest(account=Account.LIVE, tag=make_tag(Role.EXIT, Account.LIVE, S, 1, 1), role=Role.EXIT,
                        symbol="NFO:RELIANCE26OCT2900CE", tradingsymbol="RELIANCE26OCT2900CE", exchange="NFO",
                        token=12345, side=Side.SELL, qty=500, limit_px=100.0, lot_size=500, tick=0.05)

    def watch(rt: runtime.Runtime) -> None:
        runtimes.append(rt)

        def run() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (rt.ready and rt.feed_started and phases):
                time.sleep(0.02)
            lane = rt.order_lane
            seen.update(ready=rt.ready, allow_live=rt.ctx.allow_live, keep_reactor=rt.keep_reactor,
                        entries=lane.broker.entries_allowed, orders=lane.broker.orders_allowed,
                        live_orders=rt.ctx.live_orders, stop_by=rt.ctx.stop_flatten_by)
            answers: list = []
            post = lane._post
            lane._post = lambda ev: (answers.append(ev), post(ev))
            lane.submit(Place(Account.LIVE, sell.tag, sell))                  # a LIVE exit, as recovery would send
            lane.submit(Cancel(Account.LIVE, sell.tag, "261012000000077"))    # and a LIVE cancel

            def ours() -> list:
                return [(a.outcome, a.message) for a in list(answers) if getattr(a, "order_pk", None) == sell.tag]

            while time.monotonic() < deadline and len(ours()) < 2:
                time.sleep(0.02)
            seen["lane"] = ours()
            signal.raise_signal(signal.SIGINT)              # Ctrl+C: the handler runs on the main (core) thread

        threading.Thread(target=run, name="lap-ctrl-c", daemon=True).start()

    k = FakeKernel32()
    out: list[str] = []
    halt = ("CLOCK_UNSYNCED", "GLOBAL", HaltClears.AUTO, "selfcheck laptop: clock: clock offset +1.200 s")
    before = signal.getsignal(signal.SIGINT)
    with store_mod.core_thread(), caplog.at_level(logging.WARNING):
        code = laptop.run_day(
            env, out=out.append, clock=ScaledClock(ist_datetime(S, "08:50")), calendar=CAL, dash=False,
            skip_token=True, cli_mod=cli, selfcheck_step=lambda *a, **kw: [halt],
            keep_awake=laptop.KeepAwake(kernel32=k), power=lambda: laptop.PowerStatus(on_ac=False, battery_pct=40),
            bot_kwargs=dict(notifier=runtime.Notifier(send=lambda p: None), kite_factory=lambda *_a, **_k: kite,
                            ticker_factory=lambda *_a, **_k: ticker, reactor=InlineReactor(),
                            ban_fetch=lambda: ban_text, cwd=_clean_app(tmp_path), check_kite=lambda **_k: None,
                            on_runtime=watch))
    assert code == runtime.EXIT_OK, "\n".join(out)
    refused = ("REJECTED", "LOCAL: laptop profile: no LIVE orders")
    assert seen == {"ready": True, "allow_live": False, "keep_reactor": True, "entries": False, "orders": False,
                    "live_orders": False, "stop_by": f"CLI:{laptop.REQUESTED_BY}", "lane": [refused, refused]}
    assert "place_order" not in kite.calls and "cancel_order" not in kite.calls     # nothing reached Kite
    assert phases and phases[0] == ("laptop", tmp_path / "titan2.env", "laptop")
    assert k.states == [ON, OFF] and signal.getsignal(signal.SIGINT) is before
    assert ("connect", True) in ticker.calls and ("close",) in ticker.calls
    text = "\n".join(out)
    assert "FLATTEN_ALL #1 sent" in text and "verified flat (already flat" in text and "titan2 stopped cleanly" in text
    assert f"{S}" in out[-1]                                 # the daily report comes last
    assert "ON BATTERY" in caplog.text
    events = _events(var)
    kinds = [e["kind"] for e in events]
    assert kinds[:2] == ["BOOT", "SETTINGS_LOADED"] and "LIVE_REFUSED" in kinds and "READY" in kinds
    boot = events[0]["payload"]
    assert boot["profile"] == "laptop" and boot["allow_live"] is False
    stops = [e["payload"] for e in events if e["kind"] == "SHUTDOWN"]
    assert stops[0]["reason"] == "CTRL_C" and stops[-1]["phase"] == "EXITED" and stops[-1]["exit"] == 0
    flatten = [e["payload"] for e in events if e["kind"] == "HALT" and e["payload"]["kind"] == "FLATTEN"]
    cleared = [e["payload"] for e in events if e["kind"] == "HALT_CLEARED" and e["payload"]["kind"] == "FLATTEN"]
    assert len(flatten) == 1 and [c["by"] for c in cleared] == [f"CLI:{laptop.REQUESTED_BY}"]
    assert not [e for e in events if e["kind"] == "SESSION_IMPAIRED"]      # nothing was open: G1 is not touched
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        halts = {h.kind for h in st.active_halts()}
    finally:
        st.close()
    assert "CLOCK_UNSYNCED" in halts and "FLATTEN" not in halts  # the self-check's halt, raised at boot, stays
    assert ("TOKEN" in halts) is not token_ok                   # no session: entries off, recovery and READY anyway
    assert any(a[1] == "HALT_CLOCK_UNSYNCED" for a in rec.sent)
    # the in-bot laptop self-check's result brings the phone's start card (here: it did not run, and says so)
    (rt,) = runtimes
    rt.selfcheck._thread.join(10)
    cards = [a for a in rec.sent if a[1] == "START_CARD"]
    assert len(cards) == 1 and cards[0][0] == "INFO" and cards[0][3] == "start_card"
    assert "PAPER only" in cards[0][2]
    assert "self-check did not run (RuntimeError('not run in this test'))" in cards[0][2]
    assert rt.reads_lane.active is not None                    # lane-reads-quotes keeps Kite's connection warm
    # information for the core (2026-10-14 night): the lane's measured quote calls, the alerter's counters (this
    #   stand-in keeps none) and the laptop's condor line for the 15:38 phone report
    assert rt.ctx.take_quote_calls == rt.reads_lane.take_quote_calls
    assert rt.ctx.alert_counts() == {} and rt.ctx.report_notes() == {"condor": "no condor yet"}
    names = {"signals", "prep", "lane-live-orders", "lane-reads", "lane-reads-quotes", "paper-sim", "laptop"}
    assert not [t.name for t in threading.enumerate() if t.name in names and t.is_alive()]


class FakeDashChild:
    """The dashboard child's Popen: running until terminate()."""

    def __init__(self, cmd: list[str], **kw: Any) -> None:
        self.cmd, self.kw, self.pid, self.rc = cmd, kw, 4242, None

    def poll(self) -> int | None:
        return self.rc

    def terminate(self) -> None:
        self.rc = 1

    def wait(self, timeout: float | None = None) -> int | None:
        return self.rc

    def kill(self) -> None:
        self.rc = -9


def test_laptop_day_in_process_to_the_close(tmp_path, monkeypatch, caplog):
    """A whole `titan2 laptop` day in-process, ended by the bot itself: the trading-day check, the real token step
    (today's session answers profile()), the real laptop self-check (only its probes faked: the import check, disk,
    a clock 6 s behind, AC power), the dashboard child (Popen faked), the real BotProcess with fake Kite, started at
    15:40:05 so the EOD timeline runs at once and its DONE step exits 0; then the dashboard is stopped, keep-awake
    cleared and the daily report printed. Nothing reaches the network or the broker."""
    var = tmp_path / "var"
    token_mod.write_session(token_mod.new_session("abcdef123456", "AB1234", ist_datetime(S, "08:30")),
                            token_mod.session_path(var))
    (tmp_path / "titan2.env").write_text("ZERODHA_API_KEY=testkey\nZERODHA_API_SECRET=ApiSecret-9f2\n"
                                         "ZERODHA_USER_ID=AB1234\n", encoding="utf-8")
    env = lap_env(tmp_path, ZERODHA_API_KEY="testkey", ZERODHA_USER_ID="AB1234")
    kite, ticker, rec = TradingKite(), FakeTicker(), RecordingAlerter()
    monkeypatch.setattr(runtime.alerts.Alerter, "from_env", lambda *a, **k: rec)
    monkeypatch.setattr(token_mod, "kite_client", lambda *_a, **_k: kite)          # the self-check's token row
    monkeypatch.setattr(selfcheck.System, "run", lambda self, cmd, timeout, cwd=None: (
        0, f"{selfcheck.PY_TAG}3.12.7\n{selfcheck.IMPORTS_DONE}\n"))
    monkeypatch.setattr(selfcheck.System, "disk_free", lambda self, path: 100 * selfcheck.GB)
    monkeypatch.setattr(laptop, "clock_offset_s", lambda timeout_s=2.0: 6.0)
    monkeypatch.setattr(laptop, "power_status", lambda kernel32=None: laptop.PowerStatus(on_ac=True, battery_pct=99))
    # the network row's TCP connects and the laptop-settings rows' host reads (powercfg, netsh, the registry,
    #   2026-10-14 night) are scripted too, so the run stays offline and the same on every host
    monkeypatch.setattr(laptop, "network_probe", lambda: Probe("api.kite.trade", 8, 8, 0, 20.0, 30.0))
    monkeypatch.setattr(laptop, "lid_close_action", lambda run=None: (0, 1))
    monkeypatch.setattr(laptop, "restart_pending", lambda reg=None: False)
    monkeypatch.setattr(laptop, "update_active_hours", lambda reg=None: (7, 1))
    monkeypatch.setattr(laptop, "wifi_auto_connect", lambda run=None: (["Home"], 1))
    real_run_phase, phases = selfcheck.run_phase, []

    def run_phase(phase: str, ctx: Any) -> Any:             # the pre-bot run is real; the in-bot one is not run here
        phases.append(phase)
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("the in-bot self-check is not run in this test")
        return real_run_phase(phase, ctx)

    monkeypatch.setattr(selfcheck, "run_phase", run_phase)
    clock = ScaledClock(ist_datetime(S, "15:40:05"))
    children: list[FakeDashChild] = []
    k, out = FakeKernel32(), []
    ban_text = f"Securities in Ban For Trade Date {S:%d-%b-%Y}:\nNIL\n".upper()
    with store_mod.core_thread(), caplog.at_level(logging.INFO):
        code = laptop.run_day(
            env, out=out.append, clock=clock, calendar=CAL, cli_mod=cli,
            token_step=lambda e, v, **kw: laptop.ensure_token(e, v, now=clock.now_wall(), **kw),
            keep_awake=laptop.KeepAwake(kernel32=k), popen=lambda cmd, **kw: children.append(
                FakeDashChild(cmd, **kw)) or children[-1],
            bot_kwargs=dict(notifier=runtime.Notifier(send=lambda p: None), kite_factory=lambda *_a, **_k: kite,
                            ticker_factory=lambda *_a, **_k: ticker, reactor=InlineReactor(),
                            ban_fetch=lambda: ban_text, cwd=_clean_app(tmp_path), check_kite=lambda **_k: None))
    text = "\n".join(out)
    assert code == runtime.EXIT_OK, text
    assert out[0].startswith("titan2 laptop: PAPER only") and f"{S}: trading session 09:15-15:40" in out
    assert "token: today's Kite session works" in out
    summary = next(line for line in out if line.startswith("titan2 self-check laptop "))
    assert "FAIL clock: this clock is 6.000 s behind internet time" in summary
    assert summary.splitlines()[0].split(": ", 1)[1].startswith("FAIL (") and "clock_drift" not in summary
    assert "keep-awake on" in text and "dashboard: http://127.0.0.1:5000" in text
    assert "titan2: the trading day is done" in text and f"{S}" in out[-1]       # then the daily report, last
    assert phases[0] == "laptop" and "on AC power" in caplog.text
    (child,) = children
    assert child.cmd[1:] == ["-m", "titan2.dash.app", "serve", "--laptop", "--host", "127.0.0.1", "--port", "5000"]
    assert not [key for key in child.kw["env"] if key.startswith("ZERODHA_")] and child.rc is not None   # stopped
    assert k.states == [ON, OFF]
    events = _events(var)
    kinds = [e["kind"] for e in events]
    assert events[0]["kind"] == "BOOT" and events[0]["payload"]["profile"] == "laptop"
    assert "LIVE_REFUSED" in kinds and "READY" in kinds
    assert any(e["kind"] == "PHASE" and e["payload"].get("step") == "DONE" for e in events)
    stops = [e["payload"] for e in events if e["kind"] == "SHUTDOWN"]
    assert stops[-1]["phase"] == "EXITED" and stops[-1]["exit"] == 0
    st = store_mod.open_readonly(var / "db" / "titan2.db")
    try:
        halts = {h.kind for h in st.active_halts()}
    finally:
        st.close()
    assert "CLOCK_UNSYNCED" in halts and not halts & {"TOKEN", "INVARIANT"}   # the self-check's FAIL rows, at boot
    names = {"signals", "prep", "lane-live-orders", "lane-reads", "lane-reads-quotes", "paper-sim", "laptop"}
    assert not [t.name for t in threading.enumerate() if t.name in names and t.is_alive()]
