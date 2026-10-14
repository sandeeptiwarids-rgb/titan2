"""Laptop mode: titan2 on a Windows laptop, PAPER only (`titan2 laptop`, `titan2 laptop check`).

Profile. TITAN2_PROFILE=laptop selects it, and `titan2 laptop` sets it for its own process; is_laptop(env) is the one
test. LIVE is refused in this profile whatever ALLOW_LIVE or the strategy modes say (LIVE_REFUSED_REASONS): the bot
runs with allow_live False, so a LIVE-mode strategy trades only its PAPER twin for the session (a WARNING and a
LIVE_REFUSED event say so) and titan2.db keeps its mode. No LIVE order of any role leaves the process, not even the
exit of a titan2 LIVE position that EC2 left in the shared Kite account: its KiteBroker refuses every LIVE place and
cancel, and recovery does not import those orders (HALT UNKNOWN_POSITION and a CRITICAL alert instead; see
runtime.BotProcess). In the bot the self-check phase "laptop" replaces "prestart".

Paths. On Windows the defaults are %LOCALAPPDATA%\\titan2\\var (state; session/ and the token file as on Linux),
%LOCALAPPDATA%\\titan2\\logs and %APPDATA%\\titan2\\titan2.env (the secrets, outside the repo). TITAN2_VAR,
TITAN2_LOG_DIR and TITAN2_ENV_FILE override them. Elsewhere the Linux defaults stand (/var/lib/titan2, ...).
`titan2 laptop --date D` (tests and rehearsals) runs in its own %LOCALAPPDATA%\\titan2\\sim\\<D>\\var and \\logs
(next to the state dir in use; TITAN2_SIM_DIR replaces the sim folder), never in the real state dir.

`titan2 laptop` runs today's trading day in the foreground: is it a trading day (else exit 0) -> the token (today's
session if it works, else the automatic TOTP login, else the manual request-token flow on this console) -> the
laptop self-check (its FAIL rows become entry halts at the bot's boot: entries off, exits never blocked) -> the bot
session in PAPER (recovery first, exits resume before READY, as on Linux; restarted in-process on exit 75 and, a few
times, on exit 3, since no systemd restarts it here) -> the EOD timeline (the bot's own) -> the daily report -> exit.

Ctrl+C (also Ctrl+Break, or closing the console window):
- first press: a FLATTEN_ALL in commands_cli.db (as `titan2 flatten-all` would send; also when nothing is open), so
  its FLATTEN halt stops new entries within a second and every open paper leg is flattened through the normal exit
  engine, its progress printed until verified flat; then the bot stops cleanly with that halt still up. Entries are
  never allowed again before the stop: the engine clears this halt itself once the run ends flat (Shutdown, or the
  EOD exit; core.operator.release_stop_flatten), so the next start trades normally, and the command expires like a
  non-operator one, so it never runs at a later start. An operator PAUSE or FLATTEN stays as it was;
- second press: a clean stop now (entries stop, entry orders cancel, exits in flight finish): open positions stay
  open and are recovered at the next start (where the FLATTEN halt, still up unless flat by then, keeps them exiting
  and entries off until `titan2 resume`).
Windows ends a process about 5 s after its console closes, so a close gets the first press for 3 s, then the clean
stop. Signal handlers only set flags; the "laptop" thread (Supervisor) does the work and the printing.

While a session runs, SetThreadExecutionState keeps Windows from sleeping on its idle timer (closing the lid still
sleeps it), and the power source is logged at the start and every 15 min (a WARNING on battery). The console gets
the log through a queue, with QuickEdit off, so a console that stops reading never blocks the core thread; the log
file is logs/laptop-<date>.log (30 days kept). clock_offset_s() measures this clock against internet time (SNTP,
then the HTTP Date header of api.kite.trade) for the self-check, which blocks entries beyond 1 s, the reading's own
error counted.
"""

from __future__ import annotations

import ctypes
import email.utils
import importlib
import logging
import math
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

Out = Callable[[str], Any]

PROFILE_ENV, PROFILE = "TITAN2_PROFILE", "laptop"
SELFCHECK_PHASE = "laptop"                   # titan2.selfcheck.LAPTOP: the bot runs it instead of "prestart"
WINDOWS = os.name == "nt"
LINUX_VAR_DIR, LINUX_LOG_DIR, LINUX_ENV_FILE = "/var/lib/titan2", "/var/log/titan2", "/etc/titan2/titan2.env"
LIVE_REFUSED_REASONS = (
    "SEBI requires a registered static IP for API orders, which a laptop does not have",
    "a laptop has no backstop if it sleeps, loses Wi-Fi or reboots",
)
APP_DIR = Path(__file__).resolve().parents[1]        # agentic_trader/: `python -m titan2` runs from here
DASH_PORT = 5000
REQUESTED_BY = "laptop-ctrl-c"                       # commands_cli.db requested_by of the Ctrl+C flatten (the bot's
#                                                      EngineContext.stop_flatten_by is "CLI:" + this)
POWER_LOG_EVERY_S = 900.0
CLOCK_REFRESH_S = 300.0                              # internet time re-measured this often (titan2.clock.CORRECTION)
CLOCK_RETRY_S = 20.0                                 # ... and this often while there is none, or after a clock step
CLOCK_COARSE_S = 0.25                                # a reading less exact than this never replaces a better one
CLOCK_MAX_CORRECTION_S = 60.0                        # beyond this a reading is not trusted to move titan2's clock
CLOCK_MAX_ERROR_S = 0.75                             # a reading less exact than this is never applied (retried instead)
NTP_SAMPLES = 3                                      # NTP exchanges per host; the most exact one is kept
CLOCK_CORRECT_ENV = "TITAN2_CLOCK_CORRECT"           # 0: use the Windows clock as it is (no correction)
SIM_DIR_ENV = "TITAN2_SIM_DIR"                       # where `--date` runs keep their own state (simulation_dirs)
SIM_COPIED = (("session", "kite_session.json"), ("config", "settings.json"))   # seed_simulation's copies
POLL_S = 0.2                                         # the Supervisor's pass
PROGRESS_EVERY_S = 10.0                              # flatten progress: on every change, else this often
CLOSE_FLATTEN_S = 3.0                                # console close: the first press's share of Windows' ~5 s
CLOSE_STOP_S = 1.5
CRASH_RESTARTS = 5                                   # exit 3 restarts per `titan2 laptop` run
CRASH_RESTART_S = 10.0
LOG_KEEP_DAYS = 30
EXIT_OK, EXIT_FAILED, EXIT_LOCKED, EXIT_INTERRUPTED = 0, 2, 73, 130    # 73 = runtime.EXIT_LOCKED


def is_laptop(env: Mapping[str, str] | None = None) -> bool:
    """True in the laptop profile: TITAN2_PROFILE=laptop (env defaults to the process environment)."""
    e = os.environ if env is None else env
    return str(e.get(PROFILE_ENV) or "").strip().lower() == PROFILE


# ---------------------------------------------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Paths:
    base: Path               # %LOCALAPPDATA%\titan2
    var: Path
    logs: Path
    env_file: Path


def windows_paths(env: Mapping[str, str] | None = None) -> Paths:
    """The Windows layout, TITAN2_VAR / TITAN2_LOG_DIR / TITAN2_ENV_FILE (from env) overriding it. LOCALAPPDATA and
    APPDATA come from env, else the process environment, else the user's home."""
    e = os.environ if env is None else env

    def folder(key: str, *under: str) -> Path:
        return Path(e.get(key) or os.environ.get(key) or Path.home().joinpath(*under))

    local, roaming = folder("LOCALAPPDATA", "AppData", "Local"), folder("APPDATA", "AppData", "Roaming")
    base = local / "titan2"
    return Paths(base, Path(e.get("TITAN2_VAR") or base / "var"), Path(e.get("TITAN2_LOG_DIR") or base / "logs"),
                 Path(e.get("TITAN2_ENV_FILE") or roaming / "titan2" / "titan2.env"))


def default_var_dir(env: Mapping[str, str] | None = None, linux: str | Path = LINUX_VAR_DIR) -> Path:
    """TITAN2_VAR, else %LOCALAPPDATA%\\titan2\\var on Windows, else `linux`."""
    e = os.environ if env is None else env
    return windows_paths(e).var if WINDOWS else Path(e.get("TITAN2_VAR") or linux)


def default_log_dir(env: Mapping[str, str] | None = None, linux: str | Path = LINUX_LOG_DIR) -> Path:
    e = os.environ if env is None else env
    return windows_paths(e).logs if WINDOWS else Path(e.get("TITAN2_LOG_DIR") or linux)


def default_env_file(env: Mapping[str, str] | None = None, linux: str | Path = LINUX_ENV_FILE) -> Path:
    e = os.environ if env is None else env
    return windows_paths(e).env_file if WINDOWS else Path(e.get("TITAN2_ENV_FILE") or linux)


def simulation_dirs(env: Mapping[str, str] | None, day: date) -> tuple[Path, Path]:
    """(state dir, log dir) of a `titan2 laptop --date <day>` run: <root>\\<day>\\var and \\logs, where root is
    TITAN2_SIM_DIR (an explicit override, for tests), else `sim` next to the real state dir
    (%LOCALAPPDATA%\\titan2\\sim). A --date run never uses the real state dir: its rehearsal was written there as
    session <day>, so it became that day's G1 evidence, flagged the real day IMPAIRED and used up the real day's paper
    square-off (review round 3)."""
    e = os.environ if env is None else env
    root = Path(e[SIM_DIR_ENV]) if e.get(SIM_DIR_ENV) else default_var_dir(e).parent / "sim"
    base = root / day.isoformat()
    return base / "var", base / "logs"


def same_dir(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def seed_simulation(real_var: str | Path, sim_var: str | Path) -> list[str]:
    """Copy today's Kite session and the settings file from the real state dir into a --date run's, each when the
    real one is newer: the rehearsal reuses today's login (a new login would replace the token of a session started
    later today) and runs on the real settings. It only reads the real state dir. Returns what it copied."""
    copied = []
    for sub, name in SIM_COPIED:
        src, dst = Path(real_var) / sub / name, Path(sim_var) / sub / name
        try:
            if src.is_file() and (not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime):
                dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(src, dst)
                copied.append(f"{sub}/{name}")
        except OSError as exc:
            log.warning("cannot copy %s into the simulation's state dir: %s", src, exc)
    return copied


def prune_logs(log_dir: str | Path, *, keep_days: int = LOG_KEEP_DAYS, now: float | None = None) -> int:
    """Delete laptop-*.log and dash-*.log files older than keep_days (no logrotate here). Returns how many."""
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    gone = 0
    for p in list(Path(log_dir).glob("laptop-*.log")) + list(Path(log_dir).glob("dash-*.log")):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                gone += 1
        except OSError as exc:
            log.warning("cannot prune %s: %s", p, exc)
    return gone


# ---------------------------------------------------------------------------------------------------------------
# Win32: keep-awake, power status, the console close handler (all no-ops elsewhere)
# ---------------------------------------------------------------------------------------------------------------
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 2, 5, 6
CLOSE_EVENTS = frozenset({CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT})


def _kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True) if WINDOWS else None


class KeepAwake:
    """SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) from the thread that runs the session: Windows
    then does not sleep on its idle timer (the request ends with that thread, or with off())."""

    def __init__(self, kernel32: Any = None) -> None:
        self._kernel32 = kernel32
        self.active = False

    def _call(self, flags: int) -> int:
        k = self._kernel32 if self._kernel32 is not None else _kernel32()
        if k is None:
            return 0
        fn = k.SetThreadExecutionState
        fn.argtypes, fn.restype = [ctypes.c_uint32], ctypes.c_uint32
        return int(fn(flags))

    def on(self) -> bool:
        self.active = self._call(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0
        return self.active

    def off(self) -> None:
        if self.active:
            self._call(ES_CONTINUOUS)
            self.active = False


class SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]


class PowerStatus(dict):
    """{"on_ac": True | False | None, "battery_pct": 0-100 | None}; the keys are also attributes."""

    @property
    def on_ac(self) -> bool | None:
        return self.get("on_ac")

    @property
    def battery_pct(self) -> int | None:
        return self.get("battery_pct")


def power_status(kernel32: Any = None) -> PowerStatus:
    """GetSystemPowerStatus: on AC or on battery, and the charge. None where unknown (not Windows, the call failed,
    no battery)."""
    out = PowerStatus(on_ac=None, battery_pct=None)
    k = kernel32 if kernel32 is not None else _kernel32()
    if k is None:
        return out
    st = SystemPowerStatus()
    try:
        if not k.GetSystemPowerStatus(ctypes.byref(st)):
            return out
    except Exception as exc:
        log.warning("GetSystemPowerStatus failed: %r", exc)
        return out
    out["on_ac"] = {0: False, 1: True}.get(st.ACLineStatus)
    if st.BatteryFlag != 128 and st.BatteryLifePercent <= 100:          # 128: no system battery; 255: unknown
        out["battery_pct"] = int(st.BatteryLifePercent)
    return out


def network_probe() -> Any:
    """The self-check's `network` row: timed IPv4 TCP connects to api.kite.trade:443 (titan2.net.Probe)."""
    from titan2 import net

    return net.tcp_probe()


def power_text(status: Mapping[str, Any]) -> str:
    pct = status.get("battery_pct")
    charge = f", battery {pct}%" if pct is not None else ""
    return {True: "on AC power", False: "ON BATTERY", None: "power source unknown"}[status.get("on_ac")] + charge


def console_routine(ctrl_type: int, on_close: Callable[[int], Any]) -> int:
    """The SetConsoleCtrlHandler routine's body: a close, logoff or shutdown runs on_close (on Windows' own thread,
    which may block until the process is done) and is handled (1); Ctrl+C and Ctrl+Break are not (0), so the C
    runtime raises SIGINT / SIGBREAK for Python's handlers."""
    if ctrl_type not in CLOSE_EVENTS:
        return 0
    try:
        on_close(ctrl_type)
    except Exception:
        log.exception("console close handler failed")
    return 1


STD_INPUT_HANDLE = 0xFFFFFFF6                        # (DWORD)-10
ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080


def quick_edit_off(kernel32: Any = None) -> Callable[[], None] | None:
    """Turn the console's QuickEdit mode off for this run: in a classic console window a click starts a selection
    that pauses every write to the console until Esc, and with it any thread that writes there. Returns the
    restorer, or None (not Windows, or the input is not a console)."""
    k = kernel32 if kernel32 is not None else _kernel32()
    if k is None:
        return None
    k.GetStdHandle.argtypes, k.GetStdHandle.restype = [ctypes.c_uint32], ctypes.c_void_p
    k.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = k.GetStdHandle(STD_INPUT_HANDLE)
    mode = ctypes.c_uint32()
    if not handle or not k.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    old = mode.value
    if not k.SetConsoleMode(handle, (old & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS):
        return None
    return lambda: k.SetConsoleMode(handle, old)


def install_console_handler(on_close: Callable[[int], Any], kernel32: Any = None) -> Callable[[], None] | None:
    """Register console_routine (Windows only). Returns the function that removes it, or None."""
    k = kernel32 if kernel32 is not None else _kernel32()
    if k is None:
        return None
    routine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint32)(lambda ctrl_type: console_routine(ctrl_type, on_close))
    if not k.SetConsoleCtrlHandler(routine, True):
        log.warning("SetConsoleCtrlHandler failed: closing the window kills titan2 without a clean stop")
        return None

    def remove() -> None:
        k.SetConsoleCtrlHandler(routine, False)        # the closure keeps the callback alive until then

    return remove


def ctrl_c_on(kernel32: Any = None) -> bool:
    """SetConsoleCtrlHandler(NULL, FALSE): Ctrl+C reaches this process even when whatever started it ignores Ctrl+C
    (CREATE_NEW_PROCESS_GROUP, a scheduler), which children inherit. True when set (Windows only)."""
    k = kernel32 if kernel32 is not None else _kernel32()
    return k is not None and bool(k.SetConsoleCtrlHandler(None, False))


# ---------------------------------------------------------------------------------------------------------------
# The clock against internet time (titan2.selfcheck's clock row)
# ---------------------------------------------------------------------------------------------------------------
NTP_HOSTS = ("time.windows.com", "pool.ntp.org")
TIME_URL = "https://api.kite.trade"
NTP_PORT = 123
_NTP_EPOCH = 2208988800                              # 1900-01-01 -> 1970-01-01, in seconds


def _ntp_stamp(t: float) -> bytes:
    t += _NTP_EPOCH
    return struct.pack("!II", int(t) & 0xFFFFFFFF, int((t % 1) * 2 ** 32) & 0xFFFFFFFF)


def _ntp_time(raw: bytes) -> float:
    sec, frac = struct.unpack("!II", raw)
    return sec - _NTP_EPOCH + frac / 2 ** 32


class ClockOffset(float):
    """A clock reading: the seconds to add to this clock to get internet time (positive: it is behind), with .error_s,
    how far the reading itself can be off (half the NTP round trip; for the whole-second HTTP Date, 0.5 s plus half
    the round trip), and .source. The self-check judges abs(offset) + error_s, so a coarse reading fails closed."""

    error_s: float
    source: str

    def __new__(cls, seconds: float, error_s: float = 0.0, source: str = "") -> ClockOffset:
        reading = super().__new__(cls, seconds)
        reading.error_s, reading.source = float(error_s), source
        return reading


def ntp_offset_s(host: str, timeout_s: float, *, sock: Any = None,
                 now: Callable[[], float] = time.time) -> ClockOffset:
    """One SNTP (RFC 4330) exchange: the seconds to add to this clock, accurate to half the time the request and the
    reply spent on the network. Raises OSError or ValueError."""
    s = sock if sock is not None else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with closing(s):
        s.settimeout(timeout_s)
        t1 = now()
        sent = _ntp_stamp(t1)
        s.sendto(b"\x23" + b"\0" * 39 + sent, (host, NTP_PORT))      # LI 0, version 4, mode 3 (client)
        data = s.recvfrom(512)[0]
        t4 = now()
    if len(data) < 48:
        raise ValueError(f"{host}: short NTP reply ({len(data)} bytes)")
    if data[0] & 7 != 4 or data[0] >> 6 == 3 or not 1 <= data[1] <= 15:
        raise ValueError(f"{host}: not a synchronized NTP server reply")
    if data[24:32] != sent:
        raise ValueError(f"{host}: the reply does not answer this request")
    t2, t3 = _ntp_time(data[32:40]), _ntp_time(data[40:48])
    return ClockOffset(((t2 - t1) + (t3 - t4)) / 2, max(0.0, (t4 - t1) - (t3 - t2)) / 2, f"NTP {host}")


def http_date_offset_s(url: str, timeout_s: float, *, head: Callable[..., Any] | None = None,
                       now: Callable[[], float] = time.time) -> ClockOffset:
    """The server's Date header against the request. It is whole seconds: the server's clock read [H, H + 1) while
    the request ran, so the offset lies in [H - t4, H + 1 - t1], whose midpoint this is, +-(0.5 s + half the round
    trip)."""
    if head is None:
        import requests                              # lazy

        head = requests.head
    t1 = now()
    resp = head(url, timeout=timeout_s, allow_redirects=False)
    t4 = now()
    text = (getattr(resp, "headers", None) or {}).get("Date")
    if not text:
        raise ValueError(f"{url}: no Date header")
    server = email.utils.parsedate_to_datetime(text).timestamp()
    return ClockOffset(server + 0.5 - (t1 + t4) / 2, 0.5 + max(0.0, t4 - t1) / 2, f"the Date header of {url}")


def measure_clock(timeout_s: float = 2.0, *, hosts: tuple[str, ...] = NTP_HOSTS, url: str = TIME_URL,
                  samples: int = NTP_SAMPLES, ntp: Callable[..., Any] | None = None) -> tuple[ClockOffset | None, str]:
    """(the reading or None, where it came from): NTP from each host in turn, then the HTTP Date header. Each host is
    asked `samples` times and the most exact answer (smallest error_s: the shortest round trip) is kept, so one slow
    packet cannot make the reading (2026-10-14: a single +-3.6 s answer moved titan2's clock 3.5 s)."""
    problems = []
    ask = ntp or ntp_offset_s
    for host in hosts:
        got = []
        for _ in range(max(1, samples)):
            try:
                got.append(ask(host, timeout_s))
            except Exception as exc:
                problems.append(f"{host}: {type(exc).__name__}: {exc}")
        if got:
            return min(got, key=lambda r: getattr(r, "error_s", 0.0)), f"NTP {host}"
    try:
        return http_date_offset_s(url, timeout_s), f"the Date header of {url}"
    except Exception as exc:
        problems.append(f"{url}: {type(exc).__name__}: {exc}")
    log.warning("clock offset unknown: %s", "; ".join(problems))
    return None, "no time source answered: " + "; ".join(problems)


def correct_clock(measure: Callable[[], tuple] | None = None, *, out: Out | None = None) -> ClockOffset | None:
    """Measure internet time and correct titan2's clock by it (titan2.clock.CORRECTION): once at the start of a real
    run (cli), then every CLOCK_REFRESH_S from the supervisor. Without a reading the last correction stays (the
    machine drifts by about 0.05 s an hour); a reading less exact than CLOCK_COARSE_S and the one in force (the
    HTTP Date fallback after NTP) is not applied. Returns the reading applied, or None.

    Each message goes out once: to out when given (cli passes logged(out), which writes the log file too), else to
    the log."""
    from titan2 import clock as clock_mod

    reading, source = (measure or measure_clock)()
    corr = clock_mod.CORRECTION
    if reading is None:
        text = (f"clock: no internet time ({source}): " + ("the last correction stays" if corr.offset_s is not None
                else f"titan2 uses the Windows clock as it is and tries again every {CLOCK_RETRY_S:g} s"))
        if out is not None:
            out(text)
        else:
            log.warning(text)
        return None
    error = float(getattr(reading, "error_s", 0.0))
    if not error <= CLOCK_MAX_ERROR_S:
        text = (f"clock: a reading too uncertain to use ({float(reading):+.3f} s +-{error:.3f} s, {source}); "
                + ("the last correction stays" if corr.offset_s is not None
                   else f"titan2 uses the Windows clock as it is and tries again every {CLOCK_RETRY_S:g} s"))
        if out is not None:
            out(text)
        else:
            log.warning(text)
        return None
    if not abs(reading) <= CLOCK_MAX_CORRECTION_S:
        text = (f"clock: {source} says Windows is {float(reading):+.3f} s off internet time, beyond the "
                f"{CLOCK_MAX_CORRECTION_S:g} s titan2 corrects; " + ("the last correction stays" if corr.offset_s is not None
                                                                     else "titan2 uses the Windows clock as it is"))
        if out is not None:
            out(text)
        else:
            log.warning(text)
        return None
    if corr.offset_s is not None and error > max(CLOCK_COARSE_S, corr.error_s or 0.0):
        if abs(float(reading) - corr.offset_s) <= error + (corr.error_s or 0.0):
            log.info("clock: a coarse reading (+-%.3f s, %s) agrees with the one in force (+-%.3f s): kept", error,
                     source, corr.error_s or 0.0)
            return None
        log.warning("clock: a coarse reading (%+.3f +-%.3f s, %s) proves the correction in force (%+.3f s) stale: "
                    "applied", float(reading), error, source, corr.offset_s)
    before = corr.offset_s
    corr.set(float(reading), error, source)
    text = (f"clock: Windows is {abs(reading):.3f} s {'behind' if reading > 0 else 'ahead of'} internet time "
            f"(+-{error:.3f} s, {source}); titan2 keeps internet time")
    if before is None:
        clock_mod.install_log_correction()
        if out is not None:
            out(text)
        else:
            log.info(text)
    else:
        log.info("%s (correction moved %+.3f s)", text, float(reading) - before)
    return reading


def correction_missing() -> bool:
    """True while titan2's clock has no internet-time correction (the start's reading failed)."""
    from titan2 import clock as clock_mod

    return clock_mod.CORRECTION.offset_s is None


def clock_offset_s(timeout_s: float = 2.0) -> ClockOffset | None:
    """Seconds to add to this machine's clock to get internet time (positive: it is behind), as a ClockOffset (its
    .error_s says how far the reading itself can be off); None when no source answered. The laptop self-check blocks
    entries when abs(offset) + error_s is beyond 1 s (selfcheck.MAX_CLOCK_OFFSET_S), or when it is unknown."""
    return measure_clock(timeout_s)[0]


# ---------------------------------------------------------------------------------------------------------------
# Ctrl+C: the two-stage stop
# ---------------------------------------------------------------------------------------------------------------
class CtrlC:
    """Presses of Ctrl+C / Ctrl+Break / a console close. press() only updates attributes: safe in a signal handler
    (no lock, no I/O). stage 1: flatten, then a clean stop; stage 2: a clean stop now."""

    def __init__(self) -> None:
        self.presses = 0
        self.sources: list[str] = []

    @property
    def stage(self) -> int:
        return min(self.presses, 2)

    def press(self, source: str) -> int:
        self.presses += 1
        self.sources.append(source)
        return self.stage


class Session:
    """The bot run of this `titan2 laptop` (each in-process restart binds a new one) and the stop request that
    outlives them. request_stop() is signal-safe: attribute stores only (BotProcess.stop is a StopRequest)."""

    def __init__(self) -> None:
        self.bot: Any = None
        self.reason: str | None = None

    def bind(self, bot: Any) -> None:
        self.bot = bot
        if bot is not None and self.reason is not None:
            bot.stop.request(self.reason)

    def request_stop(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        bot = self.bot
        if bot is not None:
            bot.stop.request(reason)


def on_console_close(ctl: CtrlC, session: Session, finished: threading.Event, *,
                     flatten_s: float = CLOSE_FLATTEN_S, stop_s: float = CLOSE_STOP_S) -> None:
    """Windows ends the process about 5 s after a console close whatever happens: the first press while time allows,
    then the clean stop. What is still open then is recovered at the next start."""
    log.warning("console window closing: Windows ends titan2 within about 5 s; open positions are recovered at the "
                "next start")
    if ctl.press("CONSOLE_CLOSE") >= 2 or not finished.wait(flatten_s):
        session.request_stop("CONSOLE_CLOSE")
        finished.wait(stop_s)


def install_handlers(ctl: CtrlC, session: Session, finished: threading.Event, *,
                     kernel32: Any = None) -> Callable[[], None]:
    """SIGINT and SIGBREAK count presses (the second requests the clean stop), SIGTERM is a clean stop now, and on
    Windows a console close / logoff / shutdown runs on_console_close. Main thread only. Returns the restorer."""
    def on_press(signum: int, _frame: Any) -> None:
        if ctl.press(signal.Signals(signum).name) >= 2:
            session.request_stop("CTRL_C_TWICE")

    def on_term(signum: int, _frame: Any) -> None:
        session.request_stop(signal.Signals(signum).name)

    saved: dict[int, Any] = {}
    for name, fn in (("SIGINT", on_press), ("SIGBREAK", on_press), ("SIGTERM", on_term)):
        sig = getattr(signal, name, None)
        if sig is not None:
            saved[sig] = signal.getsignal(sig)
            signal.signal(sig, fn)
    remove = install_console_handler(lambda _t: on_console_close(ctl, session, finished), kernel32=kernel32)

    def restore() -> None:
        for sig, old in saved.items():
            try:
                signal.signal(sig, old if old is not None else signal.SIG_DFL)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("cannot restore the %s handler: %s", signal.Signals(sig).name, exc)
        if remove is not None:
            remove()

    return restore


def _commands_module(cli_mod: Any) -> Any:
    """titan2.__main__ (the CLI's commands_cli.db helpers): the running one when given, else imported."""
    return cli_mod if cli_mod is not None else importlib.import_module("titan2.__main__")


def open_legs(db_path: Path) -> list[str]:
    """'<book> <symbol> <state>' of every held leg of a live structure, read-only ([] without a database)."""
    from titan2 import store as store_mod

    if not Path(db_path).is_file():
        return []
    con = store_mod.connect_readonly(db_path)          # WAL: reads beside the bot's writer
    try:
        rows = con.execute(
            "SELECT l.book, l.symbol, l.state FROM legs l JOIN structures s ON s.structure_id = l.structure_id "
            "WHERE l.state IN ('ENTERING','OPEN','EXITING','VERIFY_FLAT') "
            "AND s.state NOT IN ('CLOSED','CLOSED_EXTERNAL','VOID') ORDER BY l.leg_id").fetchall()
    finally:
        con.close()
    return [f"{r[0]} {r[1]} {r[2]}" for r in rows]


class Supervisor:
    """Thread "laptop" of a `titan2 laptop` run: the power log, the dashboard watch and the Ctrl+C presses (see the
    module docstring). step() is one pass; the thread runs it every POLL_S until stop()."""

    def __init__(self, *, ctl: CtrlC, session: Session, var: Path, clock: Any, out: Out, cli_mod: Any = None,
                 power: Callable[[], Mapping[str, Any]] = power_status, mono: Callable[[], float] = time.monotonic,
                 power_every_s: float = POWER_LOG_EVERY_S, poll_s: float = POLL_S,
                 clock_keeper: Callable[[], Any] | None = None, clock_every_s: float = CLOCK_REFRESH_S) -> None:
        from titan2 import store as store_mod

        self.ctl, self.session, self.clock, self.out = ctl, session, clock, out
        self.cli = _commands_module(cli_mod)
        self.db = Path(var) / "db" / store_mod.DB_FILENAME
        self.cli_db = Path(var) / "db" / self.cli.CLI_DB
        self.power, self.mono = power, mono
        self.power_every_s, self.poll_s = power_every_s, poll_s
        self.finished = threading.Event()
        self.dash: Any = None                            # the dashboard's Popen, watched once started
        self.condor: Any = None                          # condor.CondorJob: the PAPER condor's 15:25 duty
        self.clock_keeper, self.clock_every_s = clock_keeper, clock_every_s     # correct_clock, on its own thread
        self._next_clock: float | None = None            # None until the first pass
        self._last_clock = -math.inf
        self._clock_thread: threading.Thread | None = None
        self.flatten_id: int | None = None
        self._handled = 0                                # presses already acted on
        self._next_power = 0.0
        self._progress: tuple | None = None
        self._progress_at = 0.0
        self._failed_noted = self._dash_noted = False
        self._thread: threading.Thread | None = None

    # -- the thread ---------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="laptop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.finished.set()
        if self._thread is not None:
            self._thread.join(2.0)
        if self.condor is not None:
            self.condor.join(10.0)                       # a tick in flight finishes its paper booking

    def _run(self) -> None:
        while not self.finished.is_set():
            try:
                self.step()
            except Exception:
                log.exception("laptop: supervisor step failed")
            self.finished.wait(self.poll_s)

    # -- one pass -----------------------------------------------------------------------------------------------
    def step(self) -> None:
        now = self.mono()
        if now >= self._next_power:
            self._next_power = now + self.power_every_s
            self.log_power()
        self._watch_dash()
        presses = self.ctl.presses
        if presses > self._handled:
            before, self._handled = self._handled, presses
            if presses >= 2 and before < 2:              # two presses within one pass: no flatten is started
                self._second_press()
            elif before == 0:
                self._first_press()
            else:
                self.out("already stopping: titan2 ends by itself within about 25 s")
        if self.flatten_id is not None:
            self._follow_flatten(now)
        if self.clock_keeper is not None:
            self._keep_clock(now)
        if self.condor is not None:                      # last: it can never delay a Ctrl+C
            try:
                self.condor.poll()
            except Exception:
                log.exception("laptop: condor poll failed")

    def _keep_clock(self, now: float) -> None:
        """Re-measure internet time every clock_every_s; every CLOCK_RETRY_S while titan2 has no correction yet (the
        start's reading failed) or has just absorbed a step of the Windows clock."""
        from titan2 import clock as clock_mod

        if self._clock_thread is not None and self._clock_thread.is_alive():
            return
        corr = clock_mod.CORRECTION
        if self._next_clock is None:                     # first pass: a correction from the start waits its turn
            self._next_clock = now + (self.clock_every_s if corr.offset_s is not None else 0.0)
        urgent = corr.offset_s is None or corr.step_pending
        if now < self._next_clock and not (urgent and now >= self._last_clock + CLOCK_RETRY_S):
            return
        self._last_clock, self._next_clock = now, now + self.clock_every_s
        self._clock_thread = threading.Thread(target=self._refresh_clock, name="laptop-clock", daemon=True)
        self._clock_thread.start()

    def _refresh_clock(self) -> None:
        try:
            self.clock_keeper()
        except Exception:
            log.exception("laptop: clock refresh failed")

    def log_power(self) -> None:
        status = self.power()
        if status.get("on_ac") is False:
            log.warning("power: %s: plug in the charger; a laptop that sleeps or shuts down stops managing its "
                        "positions", power_text(status))
        else:
            log.info("power: %s", power_text(status))

    def _watch_dash(self) -> None:
        if self.dash is None or self._dash_noted:
            return
        rc = self.dash.poll()
        if rc is not None:
            self._dash_noted = True
            self.out(f"dashboard stopped (exit {rc}): see {getattr(self.dash, 'log_path', 'its log')}; trading is "
                     "unaffected")

    def _send(self, kind: str, confirm: str | None = None) -> int:
        floor = self.cli.bot_last_seen(self.db)
        return self.cli.write_command(self.cli_db, kind, {}, confirm_text=confirm, requested_by=REQUESTED_BY,
                                      created=self.clock.now_wall(), floor_id=floor)

    def _stop(self, why: str) -> None:
        self.out(why)
        self.session.request_stop("CTRL_C")

    def _first_press(self) -> None:
        """Always a FLATTEN_ALL, also with nothing open: its halt stops entries at once, and the bot acks an already
        flat book DONE at its next command poll (1 s). Never a stop before that halt is up."""
        self.out("Ctrl+C: no new entries; every open paper position is closed through the exit engine, then titan2 "
                 "stops cleanly. Press Ctrl+C again to stop at once (open positions are then recovered at the next "
                 "start).")
        try:
            self.flatten_id = self._send("FLATTEN_ALL", self.cli.CONFIRM_TEXT["FLATTEN_ALL"])
        except Exception as exc:
            self._stop(f"could not queue FLATTEN_ALL ({exc}): stopping cleanly instead; open positions are recovered "
                       "at the next start")
            return
        self.out(f"FLATTEN_ALL #{self.flatten_id} sent: entries stop as the bot reads it, then every open leg is "
                 "closed")

    def _second_press(self) -> None:
        self.out("second Ctrl+C: stopping now (entries stop, entry orders cancel, exits already sent finish within "
                 "20 s). Open positions stay open; the next `titan2 laptop` start recovers and manages them.")
        if self.flatten_id is not None:
            self.out("unless everything is flat by the stop, the FLATTEN halt stays: at the next start the open legs "
                     "keep exiting and entries stay off until `titan2 resume`")
        self.flatten_id = None
        self.session.request_stop("CTRL_C_TWICE")

    def _follow_flatten(self, now: float) -> None:
        ack = self.cli.read_ack(self.db, self.flatten_id)
        status = ack["status"] if ack is not None else "QUEUED"
        legs = open_legs(self.db)
        if (status, legs) != self._progress or now - self._progress_at >= PROGRESS_EVERY_S:
            self._progress, self._progress_at = (status, legs), now
            self.out(f"flatten #{self.flatten_id} {status}: {len(legs)} open leg(s)"
                     + (f": {', '.join(legs)}" if legs else ""))
        if status == "DONE":
            # no RESUME: the FLATTEN halt keeps entries off until the stop; the bot clears it as it stops flat
            self.flatten_id = None
            self._stop(f"verified flat ({ack['result'] or 'done'}): stopping cleanly (entries stay off until then)")
        elif status in ("REJECTED", "EXPIRED"):
            self.flatten_id = None
            self._stop(f"FLATTEN_ALL {status}: {ack['result'] or ''}; stopping cleanly; open positions are recovered "
                       "at the next start")
        elif status == "FAILED" and not self._failed_noted:
            self._failed_noted = True
            self.out(f"not flat after 60 s ({ack['result'] or ''}): the exit engine keeps trying. Ctrl+C again stops "
                     "now (open positions are recovered at the next start)")


# ---------------------------------------------------------------------------------------------------------------
# The day's steps
# ---------------------------------------------------------------------------------------------------------------
_RT_IN_TEXT = re.compile(r"request_token=([A-Za-z0-9]{6,128})")


def request_token_in(text: str | None) -> str | None:
    """The request_token in what the operator pasted: the token itself, or the redirect address carrying it."""
    s = (text or "").strip()
    m = _RT_IN_TEXT.search(s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9]{6,128}", s) else None


def _quiet(severity: str, code: str, text: str) -> bool:
    """The token step's notify: logged only (a failed token ends in the bot's own HALT(TOKEN) alert)."""
    log.warning("token %s %s: %s", severity, code, text)
    return True


def session_works(env: Mapping[str, str], var: Path, *, kite_factory: Any = None,
                  now: datetime | None = None) -> bool:
    """Today's session file (issued after 06:00 IST) answers profile() as ZERODHA_USER_ID."""
    from titan2 import token as token_mod
    from titan2.clock import RealClock

    try:
        sess = token_mod.read_session(token_mod.session_path(var))
        if not sess.is_fresh(now or RealClock().now_wall()):
            return False
        token_mod.verify_session(sess, str(env.get("ZERODHA_API_KEY", "")).strip(),
                                 expected_user=str(env.get("ZERODHA_USER_ID", "")).strip() or None,
                                 kite_factory=kite_factory)
    except token_mod.TokenError:
        return False
    return True


def ensure_token(env: Mapping[str, str], var: Path, *, out: Out, ask: Callable[[str], str] | None = None,
                 run: Callable[..., int] | None = None, kite_factory: Any = None, now: datetime | None = None) -> bool:
    """A working Kite session for today on disk: today's file if it works, else the automatic TOTP login, else the
    manual request-token flow on this console (ask is None: not interactive, so no manual flow). False leaves the
    bot to start with HALT(TOKEN): entries off, recovery and exits still run. Never prints a secret."""
    from titan2 import token as token_mod

    run = run or token_mod.run
    path = token_mod.session_path(var)
    creds = token_mod.Credentials.from_env(env)
    if creds.api_key and session_works(env, var, kite_factory=kite_factory, now=now):
        out("token: today's Kite session works")
        return True
    if str(env.get("TOKEN_MODE", "auto")).strip().lower() != "manual":
        missing = creds.missing(login=True)
        if not missing:
            out("token: logging in to Kite (password + TOTP)")
            if run(env={**env, "TOKEN_MODE": "auto"}, path=path, notify=_quiet, kite_factory=kite_factory) == 0:
                out("token: OK, a new Kite session is saved")
                return True
            out("token: the automatic login failed (the log says why)")
        else:
            out(f"token: the automatic login needs {', '.join(missing)} in the env file")
    missing = creds.missing(login=False)
    if missing:
        out(f"token: no Kite login possible without {', '.join(missing)}: entries stay off (HALT TOKEN); recovery "
            "and exits still run")
        return False
    if ask is None:
        out("token: no console to paste a request token into: entries stay off (HALT TOKEN); run `titan2 token "
            "--request-token <rt>` in another window to fix it while titan2 runs")
        return False
    out(f"token: open {token_mod.login_url(creds.api_key)} in a browser, log in, then paste the request_token "
        "(or the whole address the browser was sent to) here and press Enter; Enter alone skips")
    try:
        rt = request_token_in(ask("request_token: "))
    except EOFError:
        rt = None
    if rt is None:
        out("token: skipped: entries stay off (HALT TOKEN); recovery and exits still run")
        return False
    if run(request_token=rt, env=env, path=path, notify=_quiet, kite_factory=kite_factory) == 0:
        out("token: OK, a new Kite session is saved")
        return True
    out("token: the request token was refused (the log says why): entries stay off (HALT TOKEN)")
    return False


def run_selfcheck(env: Mapping[str, str], var: Path, env_file: Path, *, now: datetime, calendar: Any,
                  out: Out, run_phase: Callable[..., Any] | None = None) -> list[tuple[str, str, Any, str]]:
    """The laptop phase before the bot starts: its halts, raised at the bot's boot (each FAIL row an entry halt,
    never an exit). A self-check that cannot run leaves the clock unverified: HALT(CLOCK_UNSYNCED), which the bot's
    own laptop self-check after prep clears when it passes."""
    from titan2 import selfcheck

    try:
        ctx = selfcheck.Context(SELFCHECK_PHASE, env=env, now=now, var_dir=var, env_file=env_file,
                                calendar=calendar)
        report = (run_phase or selfcheck.run_phase)(SELFCHECK_PHASE, ctx)
    except Exception as exc:
        log.error("laptop self-check did not run: %r", exc, exc_info=True)
        out(f"self-check: did not run ({type(exc).__name__}: {exc}): entries stay off until the bot's own self-check "
            "passes")
        return [("CLOCK_UNSYNCED", "GLOBAL", "AUTO", f"laptop self-check did not run: {exc!r}"[:300])]
    out(report.summary())
    if not report.ok:
        out("self-check: each FAIL above blocks new entries today (exits are never blocked); `titan2 laptop check` "
            "re-runs it")
    return [(h.kind, h.scope, h.clears, h.reason) for h in report.halts()]


def start_dash(env: Mapping[str, str], var: Path, logs: Path, today: date, *, out: Out,
               popen: Callable[..., Any] = subprocess.Popen, port: int = DASH_PORT) -> Any:
    """The read-only dashboard as a child process (`python -m titan2.dash.app serve --laptop`, so it imports only its
    own modules) on 127.0.0.1:port, without the bot's secrets; its output goes to logs/dash-<date>.log."""
    child = {k: v for k, v in env.items() if not k.startswith(("ZERODHA_", "TELEGRAM_")) and k != "HEALTHCHECK_URL"}
    child.update({PROFILE_ENV: PROFILE, "TITAN2_VAR": str(var), "PYTHONUNBUFFERED": "1"})
    child.pop("TITAN2_ENV_FILE", None)                  # the bot's env file is not the dashboard's (dash.env)
    from titan2 import clock as clock_mod

    if clock_mod.CORRECTION.offset_s is not None:       # the same internet time as the session (display only)
        child[clock_mod.CLOCK_OFFSET_ENV] = repr(clock_mod.CORRECTION.offset_s)
    log_path = Path(logs) / f"dash-{today:%Y-%m-%d}.log"
    cmd = [sys.executable, "-m", "titan2.dash.app", "serve", "--laptop", "--host", "127.0.0.1", "--port", str(port)]
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as fh:
            proc = popen(cmd, cwd=str(APP_DIR), env=child, stdin=subprocess.DEVNULL, stdout=fh,
                         stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except (OSError, ValueError) as exc:
        out(f"dashboard not started ({exc}); trading is unaffected")
        return None
    proc.log_path = log_path
    out(f"dashboard: http://127.0.0.1:{port} (its log: {log_path}; --no-dash skips it)")
    return proc


def stop_dash(proc: Any) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            log.exception("cannot stop the dashboard (pid %s)", getattr(proc, "pid", "?"))


def another_runs(var: Path) -> bool:
    """The bot's instance lock (<var>/run/titan2.lock) is held by another process right now."""
    from titan2 import runtime

    lock = runtime.InstanceLock(Path(var) / "run" / runtime.LOCK_NAME)
    try:
        if not lock.acquire():
            return True
    except OSError:
        return False                                 # cannot tell: the bot itself reports it (exit 78)
    lock.release()
    return False


def print_report(var: Path, day: date, now: datetime, out: Out) -> None:
    """The day's report (ledger.daily_report) from titan2.db, read-only; nothing when there is no database."""
    from titan2 import ledger, store as store_mod

    path = Path(var) / "db" / store_mod.DB_FILENAME
    if not path.is_file():
        return
    try:
        st = store_mod.open_readonly(path)
        try:
            out(ledger.daily_report(st, day, now).markdown)
        finally:
            st.close()
    except Exception as exc:
        log.exception("daily report failed")
        out(f"daily report unavailable ({exc}); `titan2 report --date {day}` retries it")


def _condor_job(env: Mapping[str, str], var: Path, clock: Any, cal: Any, out: Out, real_time: bool) -> Any:
    """The PAPER condor's 15:25 duty (titan2.condor) for a real-time session; None under --date (its entries and
    settlements must happen on the real date), with TITAN2_CONDOR=0, or when it cannot be set up: the session runs
    on unaffected."""
    if not real_time or str(env.get("TITAN2_CONDOR", "1")).strip() == "0":
        return None
    try:
        from titan2 import condor

        return condor.CondorJob(var, env, clock, cal, out)
    except Exception as exc:
        log.exception("condor job not started")
        out(f"condor: not started today ({exc}); the trading session is unaffected")
        return None


def print_condor(var: Path, out: Out) -> None:
    """Where the PAPER condor stands (titan2.condor, read-only); nothing before its first tick; never raises."""
    try:
        from titan2 import condor

        if condor.db_path(var).is_file():
            out(condor.status_text(var))
    except Exception as exc:
        log.exception("condor status failed")
        out(f"condor status unavailable ({exc}); `titan2 condor status` retries it")


def print_whatif(var: Path, day: date, env: Mapping[str, str], out: Out) -> None:
    """What the day's refused entries would have made (titan2.whatif, read-only); nothing when there is no
    database. Never raises: it runs after the session, and its failure costs only the report."""
    from titan2 import store as store_mod

    if not (Path(var) / "db" / store_mod.DB_FILENAME).is_file():
        return
    try:
        from titan2 import whatif

        whatif.run(var, day, env, out=out)
    except Exception as exc:
        log.exception("what-if report failed")
        out(f"what-if report unavailable ({exc}); `titan2 whatif --date {day}` retries it")


# ---------------------------------------------------------------------------------------------------------------
# `titan2 laptop`
# ---------------------------------------------------------------------------------------------------------------
def _bot_loop(session: Session, *, env: Mapping[str, str], clock: Any, calendar: Any, halts: list, out: Out,
              bot_factory: Callable[..., Any] | None, bot_kwargs: Mapping[str, Any] | None,
              sleep: Callable[[float], Any]) -> int:
    """BotProcess runs until the day is done or a stop is requested; exit 75 restarts at once and exit 3 up to
    CRASH_RESTARTS times (on Linux systemd does both). Only the first run raises the self-check's halts."""
    from titan2 import runtime

    factory = bot_factory or runtime.BotProcess
    crashes, first = 0, True
    code = runtime.EXIT_OK
    while session.reason is None:
        kwargs = dict(bot_kwargs or {})
        kwargs.update(env=dict(env), clock=clock, calendar=calendar, laptop=True, install_signals=False,
                      boot_halts=list(halts) if first else [])
        first = False
        bot = factory(**kwargs)
        session.bind(bot)
        try:
            code = bot.run()
        finally:
            session.bind(None)
        if session.reason is not None or code == runtime.EXIT_OK:
            break
        if code == runtime.EXIT_RESTART:
            out("titan2: controlled restart (a new Kite session, or the WS feed gave up): recovery resumes the exits")
            continue
        if code == runtime.EXIT_EXCEPTION_STORM and crashes < CRASH_RESTARTS:
            crashes += 1
            out(f"titan2 crashed (exit 3): restart {crashes} of {CRASH_RESTARTS} in {CRASH_RESTART_S:.0f} s; recovery "
                "resumes the exits (Ctrl+C stops instead)")
            for _ in range(int(CRASH_RESTART_S / POLL_S)):
                if session.reason is not None:
                    break
                sleep(POLL_S)
            continue
        break
    if session.reason is not None and code == runtime.EXIT_RESTART:
        code = runtime.EXIT_OK                       # a stop was asked for: nothing is to be restarted
    return code


def _ending(code: int, session: Session, var: Path) -> str:
    from titan2 import runtime

    if code == runtime.EXIT_OK:
        return (f"titan2 stopped cleanly ({session.reason})" if session.reason
                else "titan2: the trading day is done")
    if code == runtime.EXIT_LOCKED:
        return f"another titan2 already runs on {var} (exit 73): this one stopped without touching anything"
    if code == runtime.EXIT_FATAL:
        return "titan2 could not start (exit 78): the log says why; fix it, then run `titan2 laptop` again"
    if code == runtime.EXIT_EXCEPTION_STORM:
        return (f"titan2 stopped after crashing (exit 3, {CRASH_RESTARTS} restarts used): open positions are NOT "
                "managed now; read the log, then run `titan2 laptop` again")
    return f"titan2 exited {code}: the log says why"


def run_day(env: Mapping[str, str], *, out: Out = print, clock: Any = None, calendar: Any = None, dash: bool = True,
            skip_token: bool = False, ask: Callable[[str], str] | None = None, cli_mod: Any = None,
            bot_factory: Callable[..., Any] | None = None, bot_kwargs: Mapping[str, Any] | None = None,
            token_step: Callable[..., Any] | None = None, selfcheck_step: Callable[..., list] | None = None,
            keep_awake: KeepAwake | None = None, power: Callable[[], Mapping[str, Any]] | None = None,
            handlers: bool = True, ctl: CtrlC | None = None, popen: Callable[..., Any] = subprocess.Popen,
            sleep: Callable[[float], Any] = time.sleep, whatif_step: Callable[..., Any] | None = None,
            condor_job: Any = None) -> int:
    """One trading day (see the module docstring). env is the merged environment (cli() builds it); every step and
    outside resource is injectable for tests. Returns the exit code: 0 done (or not a trading day), 130 stopped
    before the session, else the bot's last exit code."""
    from titan2.clock import CalendarError, RealClock, SessionCalendar

    real_time = clock is None or type(clock) is RealClock     # not a --date simulation, not a test clock
    clock = clock or RealClock()
    e = dict(env)
    e[PROFILE_ENV] = PROFILE
    corrected = type(getattr(clock, "base", clock)) is RealClock and str(e.get(CLOCK_CORRECT_ENV, "1")).strip() != "0"
    var, logs, env_file = default_var_dir(e), default_log_dir(e), default_env_file(e)
    e["TITAN2_VAR"] = str(var)
    today = clock.today()
    out(f"titan2 laptop: PAPER only. State {var}; logs {logs}; env file {env_file}")
    if not env_file.is_file():
        out(f"no env file at {env_file}: create it with ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID, "
            "ZERODHA_PASSWORD and ZERODHA_TOTP_SECRET lines (KEY=value; TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID too "
            "for alerts)")
    if str(e.get("ALLOW_LIVE", "")).strip() == "1":
        out(f"ALLOW_LIVE=1 is ignored: the laptop never trades LIVE ({'; '.join(LIVE_REFUSED_REASONS)}). LIVE-mode "
            "strategies run as PAPER today")
    try:
        cal = calendar if calendar is not None else SessionCalendar.load()
    except CalendarError as exc:
        out(f"the session calendar is unusable ({exc}): no day is a trading day; nothing to do")
        return EXIT_OK
    if not cal.is_trading_day(today):
        out(f"{today}: no NSE trading session{'' if cal.covers(today) else ' (beyond the calendar)'}: nothing to do")
        return EXIT_OK
    opens, closes = cal.session_hours(today)
    out(f"{today}: trading session {opens:%H:%M}-{closes:%H:%M}")
    if another_runs(var):                            # before any login: a second one would rotate the running token
        out(f"another titan2 already runs on {var} (exit 73): this one stopped without touching anything")
        return EXIT_LOCKED
    try:
        if skip_token:
            out("token: skipped (--skip-token): the bot checks the existing session")
        else:
            (token_step or ensure_token)(e, var, out=out, ask=ask)
        if corrected and correction_missing():
            correct_clock(out=out)                   # the start's reading failed: once more, before the self-check
        halts = (selfcheck_step or run_selfcheck)(e, var, env_file, now=clock.now_wall(), calendar=cal, out=out)
    except KeyboardInterrupt:
        out("stopped before the trading session started: nothing was traded; anything still open from an earlier run "
            "is recovered at the next start")
        return EXIT_INTERRUPTED
    ctl = ctl if ctl is not None else CtrlC()
    session = Session()
    awake = keep_awake if keep_awake is not None else KeepAwake()
    sup = Supervisor(ctl=ctl, session=session, var=var, clock=clock, out=out, cli_mod=cli_mod,
                     power=power or power_status, clock_keeper=correct_clock if corrected else None)
    sup.condor = condor_job if condor_job is not None else _condor_job(e, var, clock, cal, out, real_time)
    restore = None
    if handlers:
        if threading.current_thread() is threading.main_thread():
            restore = install_handlers(ctl, session, sup.finished)
        else:
            log.warning("laptop: Ctrl+C handlers not installed: not on the main thread")
    proc = None
    try:
        try:
            if awake.on():
                out("keep-awake on: Windows does not sleep on its idle timer while titan2 runs (closing the lid still "
                    "sleeps the laptop: keep it open, on AC power)")
            elif WINDOWS:
                out("WARNING: keep-awake could not be set: Windows may sleep during the session (set Power & battery "
                    "> Screen and sleep > 'never' while plugged in)")
            sup.start()
            if dash:
                proc = sup.dash = start_dash(e, var, logs, today, out=out, popen=popen)
            out("PAPER session starting. Ctrl+C: close every open position, then stop; Ctrl+C twice: stop now")
            code = _bot_loop(session, env=e, clock=clock, calendar=cal, halts=halts, out=out,
                             bot_factory=bot_factory, bot_kwargs=bot_kwargs, sleep=sleep)
        finally:
            sup.stop()
            stop_dash(proc)
            awake.off()
            stop_reactor()
        out(_ending(code, session, var))
        print_report(var, today, clock.now_wall(), out)
        (whatif_step or print_whatif)(var, today, e, out)
        if sup.condor is not None:
            print_condor(var, out)
        return code
    finally:
        if restore is not None:
            restore()                                # also when the session raised: never half-stopped


def run_check(env: Mapping[str, str], *, out: Out = print, clock: Any = None,
              cli: Callable[..., int] | None = None) -> int:
    """`titan2 laptop check`: only the laptop self-check, every row printed (reports/selfcheck-*-laptop.json too).
    0 when no row FAILs, else 2."""
    from titan2 import selfcheck
    from titan2.clock import RealClock

    e = dict(env)
    e[PROFILE_ENV] = PROFILE
    now = (clock or RealClock()).now_wall()
    return (cli or selfcheck.cli)(SELFCHECK_PHASE, out=out, env=e, now=now, var_dir=default_var_dir(e),
                                  env_file=default_env_file(e))


def _console_safe() -> None:
    """Redirected output on Windows uses the ANSI code page: never let a character it lacks end the run."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


ON_CONSOLE = "titan2_console"                        # LogRecord attribute: False keeps a record off the console


def console_log(stream: Any = None) -> Any:
    """Console logging through a queue (logging.handlers.QueueListener, started; stop() it at the end): the core
    thread logs, and a console that stops reading (a paused or frozen window) must never block it. The file handler
    of runtime.setup_logging() stays as it is. Records logged with extra={ON_CONSOLE: False} skip the console."""
    import logging.handlers
    import queue

    from titan2 import runtime

    q: queue.SimpleQueue = queue.SimpleQueue()
    target = logging.StreamHandler(stream if stream is not None else sys.stdout)
    target.setFormatter(logging.Formatter(runtime.LOG_FORMAT))
    handler = logging.handlers.QueueHandler(q)
    handler.addFilter(lambda record: getattr(record, ON_CONSOLE, True))
    setattr(handler, runtime._LOG_MARK, True)        # replaced like the others by a later setup_logging()
    logging.getLogger().addHandler(handler)
    listener = logging.handlers.QueueListener(q, target)
    listener.start()
    return listener


def logged(out: Out) -> Out:
    """out, and the same line in the log file (kept off the console, which already shows it)."""
    def say(text: str) -> None:
        out(text)
        log.info("%s", text, extra={ON_CONSOLE: False})

    return say


def stop_reactor() -> None:
    """The Twisted reactor kept across in-process runs ends with the day (asked on its own thread). Nothing to do
    when no feed ever started one."""
    reactor = sys.modules.get("twisted.internet.reactor")
    if reactor is not None and getattr(reactor, "running", False) is True:
        try:
            reactor.callFromThread(reactor.stop)
        except Exception:
            log.exception("stopping the Twisted reactor failed")


def cli(action: str | None = None, *, date_text: str | None = None, dash: bool = True, skip_token: bool = False,
        env: Mapping[str, str] | None = None, out: Out | None = None, cli_mod: Any = None) -> int:
    """`titan2 laptop [check] [--date YYYY-MM-DD] [--no-dash] [--skip-token]` (titan2.__main__). Without env (a real
    run) it sets TITAN2_PROFILE=laptop in the process environment and sets up logging (logs/laptop-<date>.log, and
    the queued console); the env file's values are merged under the environment, which wins. A --date other than
    today is a simulation: the wall clock moves to that day and the run keeps its own state and logs
    (simulation_dirs, seeded by seed_simulation), never the real ones; its session is refused while a titan2 runs on
    the real state dir."""
    from titan2 import runtime, token as token_mod
    from titan2.clock import RealClock, ShiftedClock

    out = out or print
    real = env is None
    base = dict(os.environ if real else env)
    base[PROFILE_ENV] = PROFILE
    if real:
        os.environ[PROFILE_ENV] = PROFILE
    env_file = default_env_file(base)
    merged = token_mod.load_env(env_file, environ=base)
    merged.update({PROFILE_ENV: PROFILE, "TITAN2_VAR": str(default_var_dir(merged)),
                   "TITAN2_LOG_DIR": str(default_log_dir(merged)), "TITAN2_ENV_FILE": str(env_file)})
    clock = RealClock()
    correcting = real and str(merged.get(CLOCK_CORRECT_ENV, "1")).strip() != "0"
    if date_text:
        try:
            day = date.fromisoformat(date_text)
        except ValueError:
            out(f"refused: --date {date_text!r} is not YYYY-MM-DD")
            return EXIT_FAILED
        shift = (day - clock.today()).days
        if shift:
            clock = ShiftedClock(clock, shift)
            # its own state and logs, never the real ones (review round 3: a rehearsal in the real state dir was
            #   session D's G1 evidence, flagged the real D IMPAIRED and used up the real D's paper square-off)
            real_var = Path(merged["TITAN2_VAR"])
            sim_var, sim_logs = simulation_dirs(merged, day)
            if same_dir(sim_var, real_var):
                out(f"refused: --date {day} would run in the real state dir {real_var}; set {SIM_DIR_ENV} to another "
                    "folder")
                return EXIT_FAILED
            if action != "check" and (real_var / "run" / runtime.LOCK_NAME).is_file() and another_runs(real_var):
                out(f"refused: titan2 runs on {real_var} now; a --date rehearsal next to it could log in to Kite again "
                    "and end that session's login. Run it after the session")
                return EXIT_LOCKED
            copied = seed_simulation(real_var, sim_var)
            merged.update({"TITAN2_VAR": str(sim_var), "TITAN2_LOG_DIR": str(sim_logs)})
            note = f" (copied from the real state: {', '.join(copied)})" if copied else ""
            out(f"SIMULATION (--date {day}): the wall clock is moved {shift:+d} day(s); Kite's data is still today's. "
                f"For tests and rehearsals only: state {sim_var}, logs {sim_logs}{note}. The real state in {real_var} "
                "is never touched")
    if action == "check":
        if real:
            _console_safe()
            runtime.setup_logging(Path(merged["TITAN2_LOG_DIR"]), filename=f"laptop-{clock.today():%Y-%m-%d}.log",
                                  console=False)          # the rows are printed; the log keeps them too
        if correcting:
            correct_clock(out=logged(out))           # before the rows: they judge titan2's corrected clock
        try:
            return run_check(merged, out=out, clock=clock)
        except KeyboardInterrupt:
            out("self-check interrupted")
            return EXIT_INTERRUPTED
    listener = restore_quick_edit = None
    if real:
        _console_safe()
        ctrl_c_on()                                  # the two-stage Ctrl+C needs Ctrl+C to arrive at all
        restore_quick_edit = quick_edit_off()
        threading.current_thread().name = "core"     # as on the server: the session's core thread
        logs = Path(merged["TITAN2_LOG_DIR"])
        runtime.setup_logging(logs, filename=f"laptop-{clock.today():%Y-%m-%d}.log", console=False)
        listener = console_log()
        out = logged(out)
    if correcting:
        correct_clock(out=out)                       # logged, and before anything is stamped
        prune_logs(logs)
    interactive = real and sys.stdin is not None and sys.stdin.isatty()
    try:
        return run_day(merged, out=out, clock=clock, dash=dash, skip_token=skip_token,
                       ask=input if interactive else None, cli_mod=cli_mod)
    finally:
        if listener is not None:
            listener.stop()
        if restore_quick_edit is not None:
            restore_quick_edit()
