"""Settings and the engine's code constants (spec §8.3-8.4; the DETAIL §8.4 key table is normative).

- settings.json is optional and absent keys take their defaults. The bot reads it once per process (load_once()) and
  the Settings it gets is frozen, so nothing can change under an open trade (v1 hot-reloaded every 30 s).
- Unknown or duplicate keys, wrong types, NaN/inf, or any value looser than its bound make the WHOLE file invalid:
  the process then runs on the code defaults and the runtime raises HALT(SETTINGS_INVALID, GLOBAL, AUTO). Open
  trades keep the ExitParams frozen at their entry fill.
- Tighten-only: a key's ceiling is its default (LIMITS). Loosening needs a code change. The keys that only calibrate
  the paper exchange are fixed at their defaults (lo == hi): a file can set them to nothing else.
- set_setting() (`titan2 settings set KEY VALUE`) validates the whole resulting file and replaces it atomically.
  No UI writes settings. A file that is already invalid is repaired one change at a time: a set or unset that
  removes some of its problems and adds none is written, and the file stays invalid until the last one.
- Versioned keys feed every strategy version via Settings.versioned_view(); changing one starts a new G1 trial.

Late facts (verified 2026-10-10): NSE F&O trades 09:15-15:40 since 2026-08-03, but Zerodha's MIS auto square-off
time after that change is unverified [V]. BROKER_SQUAREOFF_AT keeps the spec default and may be set to any minute from
15:20 to 15:39 (the DETAIL §8.4 band ended at 15:29, before the close moved); titan2's own EOD times must stay strictly
before it. Option quotes are stale right after 09:15, so ENTRY_START can never be before 09:20.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass, fields
from datetime import time as dtime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from titan2 import laptop
from titan2.clock import OPENING_QUOTE_GRACE_S
from titan2.model import ExitProfile

DEFAULT_VAR_DIR = "/var/lib/titan2"
MAX_FILE_BYTES = 64 * 1024          # a settings file is a few hundred bytes; anything huge is refused, never parsed


def default_var_dir(env: Mapping[str, str] | None = None) -> Path:
    """The state folder: TITAN2_VAR, else %LOCALAPPDATA%\\titan2\\var on Windows (the laptop), else /var/lib/titan2.
    env defaults to the process environment."""
    return laptop.default_var_dir(os.environ if env is None else env, DEFAULT_VAR_DIR)


def default_path() -> Path:
    """$TITAN2_VAR/config/settings.json; the systemd unit sets TITAN2_VAR=/var/lib/titan2 (DETAIL §13.1). In the
    laptop profile on Windows the default folder is the laptop's (default_var_dir()). The bot, the CLI and the
    self-check always pass their own folder's file; this is only the library default."""
    if laptop.is_laptop(os.environ):
        return default_var_dir() / "config" / "settings.json"
    return Path(os.environ.get("TITAN2_VAR") or DEFAULT_VAR_DIR) / "config" / "settings.json"


# ---------------------------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------------------------
_LIST_KEYS = ("STOCK_ALLOWLIST", "STOCK_DENYLIST")


@dataclass(frozen=True, slots=True)
class Settings:
    """Every setting; Settings() is the code defaults. Field names are the settings.json keys (DETAIL §8.4).

    Times are IST times of day. The stock lists hold NFO underlying names ("RELIANCE", "M&M"), sorted; an empty
    allow-list means no allow-list restriction. Construction does not validate (tests build odd values on
    purpose): load() and problems() do.
    """

    # Risk (versioned)
    MAX_LOTS: int = 1                            # phase-1 code ceiling
    MAX_UNITS: int = 5000
    MAX_LOSS_PER_TRADE_RS: float = 15000.0
    MAX_PREMIUM_PER_TRADE_RS: float = 50000.0
    MIN_PREMIUM_RS: float = 20.0
    STOP_SLIP_ALLOW_PCT: float = 5.0
    DAILY_STOP_RS: float = 15000.0               # per FULL book
    MAX_WORST_CASE_DAY_LOSS_RS: float = 50000.0  # per FULL book
    MAX_POSITIONS_PER_BOOK: int = 2
    MAX_ENTRIES_PER_DAY: int = 3
    MAX_ENTRY_ATTEMPTS_PER_DAY: int = 6
    MAX_ORDERS_PER_DAY_PER_BOOK: int = 40
    MAX_ENTRY_ORDERS_PER_MIN: int = 3
    REENTRY_COOLDOWN_MIN: float = 30.0
    VOID_COOLDOWN_MIN: float = 5.0
    REJECT_COOLDOWN_MIN: float = 10.0
    STOP_AFTER_FIRST_LOSS: bool = False
    # Liquidity and freshness (versioned)
    LIQ_MAX_SPREAD_PCT: float = 1.5
    LIQ_DEPTH_MULT: float = 3.0
    LIQ_MAX_SLIP_VS_MID_PCT: float = 1.0
    LIQ_MAX_LAST_TRADE_AGE_S: float = 60.0
    MAX_CHARGES_HURDLE_PCT: float = 1.0
    QUOTE_MAX_AGE_ENTRY_S: float = 1.5
    UNDERLYING_MAX_AGE_S: float = 2.0
    STALE_REFRESH_S: float = 3.0
    STALE_PROTECTIVE_S: float = 5.0
    # Times (versioned)
    ENTRY_START: dtime = dtime(9, 20)
    NO_NEW_AFTER: dtime = dtime(14, 55)
    EOD_FLATTEN: dtime = dtime(15, 15)
    EOD_BACKSTOP: dtime = dtime(15, 22)
    STOP_SENDING: dtime = dtime(15, 25)
    BROKER_SQUAREOFF_AT: dtime = dtime(15, 26)   # [V] Zerodha MIS auto square-off, unverified since the 15:40 close
    MIN_TD_TO_EXPIRY_INDEX: int = 2
    MIN_TD_TO_EXPIRY_STOCK: int = 5
    STOCK_ALLOWLIST: tuple[str, ...] = ()
    STOCK_DENYLIST: tuple[str, ...] = ()
    # Paper exchange (versioned)
    PAPER_PARTICIPATION: float = 0.5
    PAPER_LATENCY_MEDIAN_MS: float = 250.0
    PAPER_LATENCY_SIGMA: float = 0.6
    PAPER_READ_LATENCY_MEDIAN_MS: float = 250.0  # positions()/orders() round trip (decision B1): p95 about 1 s
    PAPER_READ_LATENCY_SIGMA: float = 0.85
    PAPER_ADVERSE_TICKS: int = 0
    PAPER_PRICE_BAND_PCT: float = 20.0
    PAPER_BOOK_MAX_AGE_S: float = 1.0
    PAPER_CAPITAL_RS: float = 500000.0
    # Unversioned
    FEED_MAX_TICK_GAP_S: float = 5.0
    FEED_WARM_S: float = 60.0
    RECON_INTERVAL_S: float = 10.0
    RECON_MAX_AGE_ENTRY_S: float = 30.0
    LIVE_ACCOUNT_MAX_POSITIONS: int = 2
    LIVE_ACCOUNT_DAILY_STOP_RS: float = 10000.0
    LIVE_FIRST_WEEK_MAX_ENTRIES: int = 1
    LIVE_FIRST_WEEK_SESSIONS: int = 5
    LIVE_DD_STOP_RS: float = 15000.0
    LIVE_PROBATION_TRADES: int = 20
    LIVE_PROBATION_LOSS_RS: float = 6000.0
    LIVE_MIN_MARGIN_RS: float = 60000.0

    def __post_init__(self) -> None:
        # Code may pass a list of names: store a sorted tuple so Settings stays hashable and has one canonical form.
        for key in _LIST_KEYS:
            v = getattr(self, key)
            if isinstance(v, (list, tuple, set, frozenset)) and all(isinstance(x, str) for x in v):
                object.__setattr__(self, key, tuple(sorted(set(v))))

    def versioned_view(self) -> dict[str, Any]:
        """JSON values of the versioned keys only: the settings part of every strategy version hash (§10.2)."""
        return {k: _to_json(LIMITS[k], getattr(self, k)) for k in VERSIONED_KEYS}

    def version_basis(self) -> dict[str, Any]:
        """The engine-side inputs of a strategy version hash (§10.2): strategies/base.py can hash these from the
        Settings it is handed, without importing this module."""
        return {"settings": self.versioned_view(), "engine": ENGINE_SEMANTICS_VERSION,
                "paper_model": PAPER_MODEL_VERSION, "semantic_constants": SEMANTIC_HASH}

    def to_json(self) -> dict[str, Any]:
        """JSON values of every key, in table order (`titan2 settings show`)."""
        return {k: _to_json(lim, getattr(self, k)) for k, lim in LIMITS.items()}

    def problems(self) -> list[str]:
        """Every way this object breaks LIMITS (invariant I16); [] when it is valid."""
        out = [p for k, lim in LIMITS.items() if (p := lim.problem(k, getattr(self, k))) is not None]
        return out or _cross_key_problems(self)


# ---------------------------------------------------------------------------------------------------------------
# LIMITS: kind, bounds, tighten direction, versioned flag
# ---------------------------------------------------------------------------------------------------------------
DOWN = "down"       # lower is safer; hi is the bound (the loosest allowed value)
UP = "up"           # higher is safer (for a bool: true is safer); lo is the bound
BAND = "band"       # a fixed window; neither side is safer. lo == hi: one fixed value, which only code can change
SUBSET = "subset"   # a list of symbol names; any list only restricts eligibility

_KIND_TEXT = {"int": "an integer", "float": "a finite number", "bool": "true or false",
              "time": "a time of day 'HH:MM' or 'HH:MM:SS'", "list": "a list of NSE symbol names"}


@dataclass(frozen=True, slots=True)
class Limit:
    """One key's rule. lo/hi are inclusive (None = open); a looser-than-bound value makes the file invalid."""

    kind: str           # "int" | "float" | "bool" | "time" | "list"
    lo: Any
    hi: Any
    tighten: str        # DOWN | UP | BAND | SUBSET
    versioned: bool
    default: Any

    @property
    def bound(self) -> Any:
        """The loosest allowed value (None for BAND and SUBSET)."""
        return self.hi if self.tighten == DOWN else self.lo if self.tighten == UP else None

    @property
    def fixed(self) -> bool:
        """One allowed value, the default (lo == hi): a settings file can set it to nothing else."""
        return self.lo is not None and self.lo == self.hi

    def problem(self, key: str, v: Any) -> str | None:
        if not _has_kind(self.kind, v):
            return f"{key}: {v!r} is not {_KIND_TEXT[self.kind]}"
        if self.fixed and v != self.lo:
            return f"{key}: {_show(v)} is not {_show(self.lo)} (fixed: only a code change can change it)"
        if self.lo is not None and v < self.lo:
            return f"{key}: {_show(v)} is below {_show(self.lo)}" + (" (cannot loosen)" if self.tighten == UP else "")
        if self.hi is not None and v > self.hi:
            return f"{key}: {_show(v)} is above {_show(self.hi)}" + (" (cannot loosen)" if self.tighten == DOWN else "")
        return None

    def allowed_text(self) -> str:
        if self.kind == "list":
            return "list of NSE symbol names (only restricts)"
        if self.kind == "bool":
            return "true or false (true is tighter)"
        if self.fixed:
            return f"{_show(self.lo)} only (fixed in code)"
        lo = "" if self.lo is None else _show(self.lo)
        hi = "" if self.hi is None else _show(self.hi)
        side = {DOWN: "lower is tighter", UP: "higher is tighter", BAND: "fixed window"}[self.tighten]
        return f"{lo}..{hi} ({side})"


_T = dtime
# key: (kind, lo, hi, tighten, versioned). Bounds are DETAIL §8.4. Where the table gives one side only, the other
# side is a sanity floor/cap marked "sanity" (it refuses nonsense such as 0 s).
_TABLE: dict[str, tuple[str, Any, Any, str, bool]] = {
    # Risk
    "MAX_LOTS": ("int", 0, 1, DOWN, True),
    "MAX_UNITS": ("int", 0, 5000, DOWN, True),
    "MAX_LOSS_PER_TRADE_RS": ("float", 0.0, 15000.0, DOWN, True),
    "MAX_PREMIUM_PER_TRADE_RS": ("float", 0.0, 50000.0, DOWN, True),
    "MIN_PREMIUM_RS": ("float", 20.0, 500.0, UP, True),
    "STOP_SLIP_ALLOW_PCT": ("float", 5.0, 15.0, UP, True),
    "DAILY_STOP_RS": ("float", 0.0, 15000.0, DOWN, True),
    "MAX_WORST_CASE_DAY_LOSS_RS": ("float", 0.0, 50000.0, DOWN, True),
    "MAX_POSITIONS_PER_BOOK": ("int", 0, 2, DOWN, True),
    "MAX_ENTRIES_PER_DAY": ("int", 0, 3, DOWN, True),
    "MAX_ENTRY_ATTEMPTS_PER_DAY": ("int", 0, 6, DOWN, True),
    "MAX_ORDERS_PER_DAY_PER_BOOK": ("int", 0, 40, DOWN, True),
    "MAX_ENTRY_ORDERS_PER_MIN": ("int", 0, 3, DOWN, True),
    "REENTRY_COOLDOWN_MIN": ("float", 30.0, 390.0, UP, True),
    "VOID_COOLDOWN_MIN": ("float", 5.0, 390.0, UP, True),
    "REJECT_COOLDOWN_MIN": ("float", 10.0, 390.0, UP, True),
    "STOP_AFTER_FIRST_LOSS": ("bool", False, True, UP, True),
    # Liquidity and freshness
    "LIQ_MAX_SPREAD_PCT": ("float", 0.1, 1.5, DOWN, True),
    "LIQ_DEPTH_MULT": ("float", 3.0, 20.0, UP, True),
    "LIQ_MAX_SLIP_VS_MID_PCT": ("float", 0.1, 1.0, DOWN, True),
    "LIQ_MAX_LAST_TRADE_AGE_S": ("float", 5.0, 60.0, DOWN, True),
    "MAX_CHARGES_HURDLE_PCT": ("float", 0.1, 1.0, DOWN, True),
    "QUOTE_MAX_AGE_ENTRY_S": ("float", 0.2, 1.5, DOWN, True),
    "UNDERLYING_MAX_AGE_S": ("float", 0.5, 2.0, DOWN, True),
    "STALE_REFRESH_S": ("float", 1.0, 3.0, DOWN, True),                  # lo: sanity
    "STALE_PROTECTIVE_S": ("float", 2.0, 5.0, DOWN, True),               # lo: sanity; also > STALE_REFRESH_S
    # Times (IST); the cross-key chain below keeps them in timeline order
    "ENTRY_START": ("time", _T(9, 20), None, UP, True),
    "NO_NEW_AFTER": ("time", None, _T(14, 55), DOWN, True),
    "EOD_FLATTEN": ("time", None, _T(15, 15), DOWN, True),
    "EOD_BACKSTOP": ("time", None, _T(15, 22), DOWN, True),
    "STOP_SENDING": ("time", None, _T(15, 25), DOWN, True),
    # [V] any minute before the 15:40 F&O close (NSE, since 2026-08-03): Zerodha's MIS square-off time after that
    #   change is unverified, so a verified later time must be configurable; STOP_SENDING stays strictly before it
    "BROKER_SQUAREOFF_AT": ("time", _T(15, 20), _T(15, 39), BAND, True),
    "MIN_TD_TO_EXPIRY_INDEX": ("int", 2, 10, UP, True),
    "MIN_TD_TO_EXPIRY_STOCK": ("int", 5, 15, UP, True),
    "STOCK_ALLOWLIST": ("list", None, None, SUBSET, True),
    "STOCK_DENYLIST": ("list", None, None, SUBSET, True),
    # Paper exchange. The keys that only calibrate the paper exchange are fixed at their defaults (lo == hi; paper
    #   model 2, 2026-10-14): a settings file may not change them at all, the model changes in code, with a
    #   PAPER_MODEL_VERSION bump. For most of them neither side is safe. A lower participation misses more often the
    #   lone gap bid a live IOC takes (a scratch where LIVE books the gap's loss); a wider sigma makes every order
    #   whose seeded draw is under the median faster; a slower order or read delay (decision B1's), a younger book
    #   age or a narrower price band can land a stop's exit after a gap. They stay versioned, so a new default
    #   re-versions every strategy as well. PAPER_CAPITAL_RS is the paper account's size, not a calibration.
    "PAPER_PARTICIPATION": ("float", 0.5, 0.5, BAND, True),
    "PAPER_LATENCY_MEDIAN_MS": ("float", 250.0, 250.0, BAND, True),
    "PAPER_LATENCY_SIGMA": ("float", 0.6, 0.6, BAND, True),
    "PAPER_READ_LATENCY_MEDIAN_MS": ("float", 250.0, 250.0, BAND, True),
    "PAPER_READ_LATENCY_SIGMA": ("float", 0.85, 0.85, BAND, True),
    "PAPER_ADVERSE_TICKS": ("int", 0, 0, BAND, True),
    "PAPER_PRICE_BAND_PCT": ("float", 20.0, 20.0, BAND, True),
    "PAPER_BOOK_MAX_AGE_S": ("float", 1.0, 1.0, BAND, True),
    "PAPER_CAPITAL_RS": ("float", 100000.0, 500000.0, DOWN, True),
    # Unversioned
    "FEED_MAX_TICK_GAP_S": ("float", 1.0, 5.0, DOWN, False),            # lo: sanity
    "FEED_WARM_S": ("float", 60.0, None, UP, False),
    "RECON_INTERVAL_S": ("float", 2.0, 10.0, DOWN, False),              # lo: sanity (API budget)
    "RECON_MAX_AGE_ENTRY_S": ("float", 5.0, 30.0, DOWN, False),         # lo: sanity
    "LIVE_ACCOUNT_MAX_POSITIONS": ("int", 0, 2, DOWN, False),
    "LIVE_ACCOUNT_DAILY_STOP_RS": ("float", 0.0, 10000.0, DOWN, False),
    "LIVE_FIRST_WEEK_MAX_ENTRIES": ("int", 0, 1, DOWN, False),
    "LIVE_FIRST_WEEK_SESSIONS": ("int", 5, None, UP, False),
    "LIVE_DD_STOP_RS": ("float", 0.0, 15000.0, DOWN, False),
    "LIVE_PROBATION_TRADES": ("int", 20, None, UP, False),
    "LIVE_PROBATION_LOSS_RS": ("float", 0.0, 6000.0, DOWN, False),
    "LIVE_MIN_MARGIN_RS": ("float", 60000.0, None, UP, False),
}

_FIELD_DEFAULTS = {f.name: f.default for f in fields(Settings)}
LIMITS: Mapping[str, Limit] = MappingProxyType(
    {k: Limit(kind, lo, hi, tighten, versioned, _FIELD_DEFAULTS[k])
     for k, (kind, lo, hi, tighten, versioned) in _TABLE.items()}
)
VERSIONED_KEYS: tuple[str, ...] = tuple(k for k, lim in LIMITS.items() if lim.versioned)

# (earlier, later, may_be_equal): entries stop no later than the flatten, and titan2's own EOD steps all come
# strictly before the broker's MIS square-off.
_TIME_CHAIN = (
    ("ENTRY_START", "NO_NEW_AFTER", False),
    ("NO_NEW_AFTER", "EOD_FLATTEN", True),
    ("EOD_FLATTEN", "EOD_BACKSTOP", False),
    ("EOD_BACKSTOP", "STOP_SENDING", False),
    ("STOP_SENDING", "BROKER_SQUAREOFF_AT", False),
)


def _cross_key_problems(s: Settings) -> list[str]:
    out = []
    if not s.STALE_PROTECTIVE_S > s.STALE_REFRESH_S:
        out.append(f"STALE_PROTECTIVE_S ({s.STALE_PROTECTIVE_S!r}) must be above STALE_REFRESH_S "
                   f"({s.STALE_REFRESH_S!r})")
    for a, b, may_equal in _TIME_CHAIN:
        ta, tb = getattr(s, a), getattr(s, b)
        if not (ta <= tb if may_equal else ta < tb):
            out.append(f"{a} ({_show(ta)}) must be {'at or ' if may_equal else ''}before {b} ({_show(tb)})")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------------------------------------------
_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])(?::([0-5][0-9]))?")
_NAME_RE = re.compile(r"[A-Z0-9][A-Z0-9&_-]{0,29}")


class _Bad(ValueError):
    pass


def _number(v: Any) -> bool:
    """A finite int or float, never a bool."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:           # an int too large for a float
        return False


def _has_kind(kind: str, v: Any) -> bool:
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "float":
        return _number(v)
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "time":
        return isinstance(v, dtime) and v.tzinfo is None
    if kind == "list":
        return isinstance(v, tuple) and all(isinstance(x, str) and _NAME_RE.fullmatch(x) for x in v)
    return False


def _show(v: Any) -> str:
    return v.isoformat(timespec="seconds") if isinstance(v, dtime) else repr(v)


def _symbol_name(x: Any) -> str | None:
    """' nse:reliance ' -> 'RELIANCE'. None if it is not a plausible NSE symbol name."""
    if not isinstance(x, str):
        return None
    name = x.strip().upper()
    if name.startswith("NSE:"):
        name = name[4:]
    return name if _NAME_RE.fullmatch(name) else None


def _coerce(key: str, lim: Limit, raw: Any) -> Any:
    """A parsed JSON value -> the typed value for this key (bounds are checked later, by problems())."""
    kind = lim.kind
    if kind == "int" and isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if kind == "float" and _number(raw):
        return float(raw)
    if kind == "bool" and isinstance(raw, bool):
        return raw
    if kind == "time" and isinstance(raw, str):
        m = _TIME_RE.fullmatch(raw)
        if m:
            return dtime(int(m[1]), int(m[2]), int(m[3] or 0))
    if kind == "list" and isinstance(raw, list):
        names = [_symbol_name(x) for x in raw]
        bad = [x for x, n in zip(raw, names) if n is None]
        if bad:
            raise _Bad(f"{key}: {bad[0]!r} is not an NSE symbol name")
        return tuple(sorted(set(names)))
    raise _Bad(f"{key}: {raw!r} is not {_KIND_TEXT[kind]}")


def _to_json(lim: Limit, v: Any) -> Any:
    """Typed value -> its JSON form: floats stay floats, times 'HH:MM:SS', name lists sorted lists."""
    if lim.kind == "float" and _number(v):
        return float(v)
    if lim.kind == "time" and isinstance(v, dtime):
        return v.isoformat(timespec="seconds")
    if lim.kind == "list" and isinstance(v, (tuple, list)):
        return list(v)
    return v


def _unknown(key: str) -> str:
    hint = f" (did you mean {key.upper()}?)" if key != key.upper() and key.upper() in LIMITS else ""
    return f"{key!r}: unknown key{hint}"


# ---------------------------------------------------------------------------------------------------------------
# Loading (read once)
# ---------------------------------------------------------------------------------------------------------------
class SettingsError(ValueError):
    """A refused settings change; .errors lists every reason. The file is left untouched."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        ValueError.__init__(self, "; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class LoadResult:
    settings: Settings          # what the process runs on: the file's values, or the code defaults if invalid
    valid: bool                 # False -> the runtime raises HALT(SETTINGS_INVALID, GLOBAL, AUTO)
    errors: tuple[str, ...]
    path: Path
    exists: bool
    sha256: str | None          # of the file bytes (for the SETTINGS_LOADED event)

    def summary(self) -> str:
        if not self.exists:
            return f"no settings file at {self.path}: code defaults"
        if self.valid:
            return f"settings {self.path} valid (sha256 {self.sha256[:12] if self.sha256 else '?'})"
        more = f" (+{len(self.errors) - 3} more)" if len(self.errors) > 3 else ""
        return f"settings {self.path} INVALID, running on code defaults: {'; '.join(self.errors[:3])}{more}"


def from_mapping(data: Mapping[str, Any]) -> tuple[Settings | None, list[str]]:
    """Checked Settings from a parsed settings.json object: (settings, []) or (None, every problem found)."""
    errors: list[str] = []
    values: dict[str, Any] = {}
    for key, raw in data.items():
        lim = LIMITS.get(key)
        if lim is None:
            errors.append(_unknown(key))
            continue
        try:
            values[key] = _coerce(key, lim, raw)
        except _Bad as exc:
            errors.append(str(exc))
    if errors:
        return None, errors
    s = Settings(**values)
    errors = s.problems()
    return (None, errors) if errors else (s, [])


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


# Ported from agentic_trader/settings_manager.py:117-129 @b4694f3 — changes: read once (no mtime hot reload); any
#   error makes the file invalid instead of being printed and ignored; strict JSON (no NaN, no duplicate keys)
def _read_document(p: Path) -> tuple[bool, str | None, dict[str, Any] | None, list[str]]:
    """(exists, sha256 of the bytes, the parsed JSON object, problems). The file is opened and read exactly once."""
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return False, None, None, []
    except OSError as exc:
        return True, None, None, [f"cannot stat {p}: {exc}"]
    if not stat.S_ISREG(st.st_mode):        # a directory or FIFO would fail or block the startup read
        return True, None, None, [f"{p} is not a regular file"]
    try:
        with open(p, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return True, None, None, [f"cannot read {p}: {exc}"]
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_FILE_BYTES:
        return True, digest, None, [f"{p} is larger than {MAX_FILE_BYTES} bytes"]
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:      # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return True, digest, None, [f"{p} is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return True, digest, None, [f"{p} must hold one JSON object, not {type(data).__name__}"]
    return True, digest, data, []


def load(path: str | Path | None = None) -> LoadResult:
    """Read and check the settings file. Absent -> valid code defaults; invalid -> code defaults plus the errors.

    Pure: every call reads the file. The bot uses load_once(); the CLI and the self-check use load() directly.
    """
    p = Path(path) if path is not None else default_path()
    exists, digest, data, errors = _read_document(p)
    if not exists:
        return LoadResult(DEFAULTS, True, (), p, False, None)
    s = None
    if not errors:
        s, errors = from_mapping(data)
    if s is None:
        return LoadResult(DEFAULTS, False, tuple(errors), p, True, digest)
    return LoadResult(s, True, (), p, True, digest)


_once_lock = threading.Lock()
_once: LoadResult | None = None


def load_once(path: str | Path | None = None) -> LoadResult:
    """The process's settings: only the first call reads the file; every later call returns that same result."""
    global _once
    with _once_lock:
        if _once is None:
            _once = load(path)
        elif path is not None and Path(path) != _once.path:
            raise RuntimeError(f"settings were already loaded from {_once.path}; they are read once per process")
        return _once


def _reset_load_once() -> None:
    """Tests only."""
    global _once
    with _once_lock:
        _once = None


# ---------------------------------------------------------------------------------------------------------------
# Writing (`titan2 settings set|unset`) and diff
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SetResult:
    key: str
    old: Any                # JSON value the process would have run on before (None for an unknown key)
    new: Any                # JSON value now in force from the next restart (None when an unknown key was removed)
    changed: bool           # False -> nothing was written
    versioned: bool         # True -> every strategy gets a new version (a new G1 trial)
    before: Settings
    after: Settings
    path: Path
    # a repair of an invalid file that leaves it invalid: the problems still in it. Then new is the key's value once
    #   the file is valid, after is the code defaults (what the bot runs on until then) and versioned is False.
    still_invalid: tuple[str, ...] = ()

    def message(self) -> str:
        if not self.changed:
            return f"{self.key} unchanged ({json.dumps(self.new)}); nothing written"
        text = (f"{self.key}: {json.dumps(self.old)} -> {json.dumps(self.new)} written to {self.path}; "
                f"takes effect at next restart")
        if self.versioned:
            text += "; versioned key: every strategy gets a new version (a new G1 trial)"
        if self.still_invalid:
            text += ("; the file is still invalid, so the bot keeps running on the code defaults (HALT "
                     f"SETTINGS_INVALID) until these are fixed or unset too: {'; '.join(self.still_invalid)}")
        return text


_UNSET = object()


def _parse_cli_text(lim: Limit, text: str) -> Any:
    """CLI text -> a JSON value: JSON when it parses, else the plain string; a list key also takes "A,B,C"."""
    try:
        v = json.loads(text, parse_constant=_no_constant)
    except ValueError:
        v = text.strip()
    if lim.kind == "list" and isinstance(v, str):
        v = [x for x in v.split(",") if x.strip()]
    return v


def set_setting(key: str, value: Any, path: str | Path | None = None) -> SetResult:
    """Validate KEY=VALUE together with the rest of the file, then replace the file atomically.

    value is CLI text or a Python value. Raises SettingsError, leaving the file untouched, when the key is unknown,
    the value is malformed or looser than its bound, or the rest of the file is invalid (unless the change repairs
    one of its problems and adds none: _update).
    """
    lim = LIMITS.get(key)
    if lim is None:
        raise SettingsError([_unknown(key)])
    raw = _parse_cli_text(lim, value) if isinstance(value, str) else _to_json(lim, value)
    try:
        new = _to_json(lim, _coerce(key, lim, raw))
    except _Bad as exc:
        raise SettingsError([str(exc)]) from None
    return _update(Path(path) if path is not None else default_path(), key, new)


def unset_setting(key: str, path: str | Path | None = None) -> SetResult:
    """Remove KEY from the file so it takes its default; also how an unknown key left in a file is dropped."""
    return _update(Path(path) if path is not None else default_path(), key, _UNSET)


def _problems(data: Mapping[str, Any]) -> set[str]:
    """Every problem of a settings object: each key's own (unknown, wrong type, beyond its bounds), then the rules
    between keys with those keys left out. from_mapping() stops at the first kind, so a repair could otherwise hide a
    new cross-key problem behind an old one."""
    own: dict[str, str] = {}
    for k, raw in data.items():
        lim = LIMITS.get(k)
        if lim is None:
            own[k] = _unknown(k)
            continue
        try:
            problem = lim.problem(k, _coerce(k, lim, raw))
        except _Bad as exc:
            problem = str(exc)
        if problem is not None:
            own[k] = problem
    _, rest = from_mapping({k: v for k, v in data.items() if k not in own})
    return set(own.values()) | set(rest)


def _update(p: Path, key: str, new: Any) -> SetResult:
    _, _, data, errors = _read_document(p)
    if errors:
        raise SettingsError(errors + [f"restore {p} from a backup or remove it, then retry"])
    data = data or {}
    parsed, _ = from_mapping(data)
    before = parsed if parsed is not None else DEFAULTS
    doc = {k: v for k, v in data.items() if k != key}
    if new is not _UNSET:
        doc[key] = new
    after, errors = from_mapping(doc)
    lim = LIMITS.get(key)
    if after is None:
        # A file that is already invalid is repaired one change at a time: one that removes some of its problems and
        #   adds none is written though others remain. A file holding several paper keys the code has since fixed
        #   (paper model 2) could otherwise be repaired by neither `unset` nor `set KEY <default>` (review round 4):
        #   removing one key left the other's error, so nothing was written. Any other change waits for a valid file.
        left = _problems(doc)
        if parsed is not None or not left < _problems(data):
            raise SettingsError(errors + ([] if parsed is not None else [
                f"{p} is already invalid: a change is written only when it removes one of its problems and adds "
                "none (`titan2 settings unset KEY` removes a key)"]))
        try:
            _atomic_write_json(p, doc)
        except OSError as exc:
            raise SettingsError([f"cannot write {p}: {exc}"]) from exc
        return SetResult(key=key, old=before.to_json()[key] if lim else data.get(key),
                         new=(new if new is not _UNSET else _to_json(lim, lim.default)) if lim else None,
                         changed=True, versioned=False, before=before, after=DEFAULTS, path=p,
                         still_invalid=tuple(errors) + tuple(sorted(left - set(errors))))
    doc = {k: _to_json(LIMITS[k], getattr(after, k)) for k in doc}     # one normalized form on disk
    # an invalid file always changes; a valid one only when a value does (not for formatting such as "09:30")
    changed = parsed is None or doc != {k: _to_json(LIMITS[k], getattr(parsed, k)) for k in data}
    if changed:
        try:
            _atomic_write_json(p, doc)
        except OSError as exc:
            raise SettingsError([f"cannot write {p}: {exc}"]) from exc
    # a versioned key gives new versions only when what the bot runs on changes: the last repair of an invalid file
    #   that leaves the defaults in force (the bot ran on them meanwhile) starts no new G1 trial
    return SetResult(key=key,
                     old=before.to_json()[key] if lim else data.get(key),
                     new=after.to_json()[key] if lim else None,
                     changed=changed, versioned=bool(lim and lim.versioned)
                     and before.versioned_view() != after.versioned_view(), before=before, after=after, path=p)


# Ported from agentic_trader/settings_manager.py:131-145 @b4694f3 — changes: temp file in the same directory, fsync,
#   then os.replace (atomic on Windows too, where v1 unlinked first); errors raise instead of being printed; no
#   _last_updated key; the directory entry is fsynced on POSIX
def _atomic_write_json(p: Path, doc: Mapping[str, Any]) -> None:
    text = json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if os.name == "posix":                  # Windows cannot open a directory to fsync it
        dfd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def diff(base: Settings, other: Settings) -> dict[str, tuple[Any, Any]]:
    """{key: (base value, other value)} in JSON form for every key that differs (`titan2 settings diff`)."""
    a, b = base.to_json(), other.to_json()
    return {k: (a[k], b[k]) for k in LIMITS if a[k] != b[k]}


# ---------------------------------------------------------------------------------------------------------------
# Code constants (not settings; DETAIL §8.4). Changing anything in SEMANTIC_CONSTANTS needs an
# ENGINE_SEMANTICS_VERSION bump (REGISTRY.lock test), and that bump gives every strategy a new version.
# ---------------------------------------------------------------------------------------------------------------
ENGINE_SEMANTICS_VERSION = 2        # bump on any change in how the engine trades or books: every version changes
# 2 (2026-10-14, one bump for the release): a G09 refusal due only to pending structures waits, one REST quote
#   retry, and the engine fixes of review rounds 2 and 3 (FINAL_SPEC errata "Engine semantics 2")
PAPER_MODEL_VERSION = 2             # bump on any paper-fill recalibration (§6.7): every strategy version changes
# 2 (2026-10-14): a lot's leftover fraction fills by a seeded draw, crossed or older-than-decision books count as
#   missing (the square-off fills one at its worse side), and the paper calibration keys are fixed at their defaults
#   (broker/paper.py; FINAL_SPEC errata "Paper model v2")
SCHEMA_VERSION = 1                  # titan2.db PRAGMA user_version (§4.3)

# G1 and live review (§10.7, §10.9)
G1_MIN_TRADES = 100
G1_MIN_SESSIONS = 30
G1_LOOKS = (100, 150, 200, 300)
G1_ALPHA = 0.05                     # one-sided (the brief's 90% CI lower bound)
G1_FAMILY_FLOOR = 4
G1_SPREAD_STRESS = 1.5
G1_PLACEBO_MIN_N = 50
G1_PLACEBO_MIN_COVERAGE = 0.8
G1_MAX_IMPAIRED_FRAC = 0.10
G1_MAX_BAR_MISMATCH_FRAC = 0.10
G1_CONFIRM_TRADES = 25
G1_FUTILITY_MIN_N = 50
G1_FUTILITY_EVERY = 25
G1_FUTILITY_ALPHA = 0.10
G1_REPORT_MAX_AGE_DAYS = 7
G1_BOOT_MIN = 10_000
G1_IMPAIRED_GAP_S = 600.0           # a heartbeat gap > 10 min inside the entry window marks the session IMPAIRED
G1_LIVE_ROLLING_N = 50              # demote when the mean of the last 50 LIVE trades is below 0
G1_X2_MIN_PAIRS = 20                # X2 falsifier: twin pairs needed before it can fire
G1_X2_MAX_SLIP_GAP_PP = 0.5         # median (live - paper) slip_vs_walk above this -> PAPER_MODEL_OPTIMISTIC
G1_X2_MAX_FILL_RATE_GAP_PP = 10.0   # live IOC fill rate this far below paper -> PAPER_MODEL_OPTIMISTIC

# Pricing and the exit engine (§5.5, §5.7, §7)
BANDS = (0.0, 0.005, 0.01, 0.02, 0.04, 0.08, 0.12)
MAX_EXIT_DISCOUNT = 0.12            # an exit limit is never below ceil_tick(ref_bid x 0.88) (I13)
THIN_BOOK_PENALTY = 0.02            # rb values qty beyond the shown bids at the deepest bid x 0.98
PROTECTIVE_BAND = 3                 # starting band for protective pricing, and the floor after EOD_BACKSTOP
PRICE_EDGE_TICKS = 2                # entry ask + 2 ticks; exit sweep - 2 ticks; band-reject re-price bid - 2 ticks
ENTRY_MAX_OVER_MID = 0.01           # entry limit <= floor_tick(mid x 1.01) (I13)
SELFTEST_BID_FACTOR = 0.97          # the self-test BUY sits at floor_tick(bid x 0.97): it can never fill
EXIT_QUOTE_MAX_AGE_S = 1.0          # the exit loop prices only from a quote at most 1 s old
EXIT_RETRY_MIN_S = 0.25             # next exit attempt after max(250 ms, the next fresh quote)
FILL_PX_MAX_DEV = 0.20              # apply_cum: a derived fill price > 20% from the broker average uses the average
EXIT_BOUNDS = {                     # ExitProfile field: (lo, hi) inclusive; a profile outside fails registry load
    "hard_sl_pct": (5.0, 15.0),
    "lock_arm_pct": (2.0, 10.0),
    "lock_floor_pct": (2.0, 9.5),   # and <= lock_arm_pct - EXIT_FLOOR_ARM_GAP_PCT
    "trail_retain": (0.5, 0.9),
    "tp_pct": (8.0, 50.0),          # and above lock_arm_pct (tp_px > arm_px is re-checked on the frozen levels)
    "time_stop_min": (10.0, 120.0),
    "stop_confirm_ms": (0, 1000),
    "stop_confirm_max_ms": (500, 3000),
    "stop_confirm_bypass_pct": (1.0, 10.0),
    "no_bid_exit_s": (3.0, 30.0),
}
EXIT_FLOOR_ARM_GAP_PCT = 0.5

# Order timers (§5.2), seconds after submit. The 30 s late-accept window after an ambiguous place,
#   core/orders.LATE_ACCEPT_S, is decision B2 (2026-10-11): kept at 30 s; see the comment there and RUNBOOK §10.
ORDER_POLL_AFTER_S = 3.0
ORDER_POLL_EVERY_S = 1.0
ORDER_CANCEL_AFTER_S = 6.0
ORDER_STUCK_AFTER_S = 10.0          # after the cancel
LOOKUP_AT_S = (1.0, 3.0)

# Reconciliation and verify-flat (§9.2-9.4)
FRESH_FILL_GRACE_S = 5.0
POSITIONS_POST_FILL_LAG_S = 2.0     # [V] measure the positions() lag p95
RECON_CONFIRM_GAP_S = 3.0
POSITIONS_WAIT_MAX_S = 5.0
# recon.broker_cap: how long before an exit attempt's decision (the exit decision, or the end of the leg's previous
#   attempt if later) a post-fill read may have been requested and still cap it (§9.4 step 1). Decision B1
#   (2026-10-11): 0, strict. Only a read requested at or after the decision caps the attempt, so a close just before it
#   (a manual close, an RMS square-off) is never sold again as a naked short (G3). Every attempt pays one positions()
#   round trip, and paper pays it too (the seeded read delay of broker/paper.py), so paper exit timing matches LIVE.
EXIT_CAP_PRIOR_READ_S = 0.0
BROKER_READS_FAIL_UNCAP_S = 10.0
VERIFY_FLAT_RECHECK_S = 2.0
VERIFY_FLAT_TIMEOUT_S = 10.0

# Quotes and the core loop (§3)
QUOTE_REQUEST_TIMEOUT_S = 3.0
REST_BATCH = 500
INTENT_MAX_PER_MIN = 30
TIMER_DT = 0.25
HEARTBEAT_EVERY_S = 60.0

# Gates (§8.6)
LIQ_MIN_SPREAD_TICKS = 2            # G19: a spread of up to 2 ticks always passes
LIQ_DEPTH_LEVELS = 3                # G19 and size(): top-3 levels on each side
WORST_CASE_PAD_RS = 100.0           # G20: per position, on top of premium + charges
FUNDS_MAX_AGE_S = 60.0              # G21
FUNDS_PREMIUM_FACTOR = 1.1          # G21: available >= premium x 1.1 + charges
MONO_SKEW_S = 1.0                   # G03/G14: an intent created further than this in the core's future is refused
LAST_TRADE_FUTURE_S = 5.0           # G19: a last-trade stamp at most this far ahead of our clock is tolerated

# Bar check (§10.3, 15:36): a live 09:15 bar against Kite's historical candle; G1 counts the mismatches (§10.7)
BAR_CHECK_PRICE_TOL = 0.001         # open and close within 0.1 %
BAR_CHECK_VOLUME_TOL = 0.05         # volume within 5 %
BAR_CHECK_SAMPLE = 20               # seeded universe sample, on top of every symbol that fired

# Limits (§3.3, §8.6, §7.5)
EXC_LIMIT = 20                      # exceptions per EXC_WINDOW_S -> exit 3
EXC_WINDOW_S = 60.0
GATE_ERROR_LIMIT = 5                # gate errors per GATE_ERROR_WINDOW_S -> HALT(GATE_ERRORS)
GATE_ERROR_WINDOW_S = 300.0
POST_EXIT_TAIL_MIN = 45

# Paper exchange (§6); the PAPER_* settings are its calibrated part
PAPER_LATENCY_CLIP_MS = (80.0, 3000.0)
PAPER_DEPTH_WAIT_S = 1.5            # no fresh depth at the due time: wait this long for REST, else CANCELLED
PAPER_EXPIRED_GRACE_S = 1.0         # startup: an OPEN order with due + 1 s < now is CANCELLED, 0 filled
PAPER_MISSED_SQOFF_PX = 0.05        # startup: a previous-session position is squared off at Rs 0.05
PAPER_SQOFF_SELL_FACTOR = 0.95      # simulated square-off remainder: deepest bid x 0.95 (sell)
PAPER_SQOFF_BUY_FACTOR = 1.05       # ... ask x 1.05 (buy)

# Everything that changes which trades are taken, how they are priced, filled or exited, or how G1 judges them.
# Versions, storage, batching, logging and fault-handling limits are deliberately left out. A plain dict (not a
# read-only proxy) so json can serialize it; nothing may mutate it.
SEMANTIC_CONSTANTS: dict[str, Any] = {
    "G1_MIN_TRADES": G1_MIN_TRADES, "G1_MIN_SESSIONS": G1_MIN_SESSIONS, "G1_LOOKS": G1_LOOKS,
    "G1_ALPHA": G1_ALPHA, "G1_FAMILY_FLOOR": G1_FAMILY_FLOOR, "G1_SPREAD_STRESS": G1_SPREAD_STRESS,
    "G1_PLACEBO_MIN_N": G1_PLACEBO_MIN_N, "G1_PLACEBO_MIN_COVERAGE": G1_PLACEBO_MIN_COVERAGE,
    "G1_MAX_IMPAIRED_FRAC": G1_MAX_IMPAIRED_FRAC, "G1_MAX_BAR_MISMATCH_FRAC": G1_MAX_BAR_MISMATCH_FRAC,
    "G1_CONFIRM_TRADES": G1_CONFIRM_TRADES, "G1_FUTILITY_MIN_N": G1_FUTILITY_MIN_N,
    "G1_FUTILITY_EVERY": G1_FUTILITY_EVERY, "G1_FUTILITY_ALPHA": G1_FUTILITY_ALPHA,
    "G1_REPORT_MAX_AGE_DAYS": G1_REPORT_MAX_AGE_DAYS, "G1_BOOT_MIN": G1_BOOT_MIN,
    "G1_IMPAIRED_GAP_S": G1_IMPAIRED_GAP_S, "G1_LIVE_ROLLING_N": G1_LIVE_ROLLING_N,
    "G1_X2_MIN_PAIRS": G1_X2_MIN_PAIRS, "G1_X2_MAX_SLIP_GAP_PP": G1_X2_MAX_SLIP_GAP_PP,
    "G1_X2_MAX_FILL_RATE_GAP_PP": G1_X2_MAX_FILL_RATE_GAP_PP,
    "BANDS": BANDS, "MAX_EXIT_DISCOUNT": MAX_EXIT_DISCOUNT, "THIN_BOOK_PENALTY": THIN_BOOK_PENALTY,
    "PROTECTIVE_BAND": PROTECTIVE_BAND, "PRICE_EDGE_TICKS": PRICE_EDGE_TICKS, "ENTRY_MAX_OVER_MID": ENTRY_MAX_OVER_MID,
    "SELFTEST_BID_FACTOR": SELFTEST_BID_FACTOR, "EXIT_QUOTE_MAX_AGE_S": EXIT_QUOTE_MAX_AGE_S,
    "EXIT_RETRY_MIN_S": EXIT_RETRY_MIN_S, "FILL_PX_MAX_DEV": FILL_PX_MAX_DEV, "EXIT_BOUNDS": EXIT_BOUNDS,
    "EXIT_FLOOR_ARM_GAP_PCT": EXIT_FLOOR_ARM_GAP_PCT,
    "ORDER_POLL_AFTER_S": ORDER_POLL_AFTER_S, "ORDER_POLL_EVERY_S": ORDER_POLL_EVERY_S,
    "ORDER_CANCEL_AFTER_S": ORDER_CANCEL_AFTER_S, "ORDER_STUCK_AFTER_S": ORDER_STUCK_AFTER_S,
    "LOOKUP_AT_S": LOOKUP_AT_S,
    "FRESH_FILL_GRACE_S": FRESH_FILL_GRACE_S, "POSITIONS_POST_FILL_LAG_S": POSITIONS_POST_FILL_LAG_S,
    "RECON_CONFIRM_GAP_S": RECON_CONFIRM_GAP_S, "POSITIONS_WAIT_MAX_S": POSITIONS_WAIT_MAX_S,
    "EXIT_CAP_PRIOR_READ_S": EXIT_CAP_PRIOR_READ_S,
    "BROKER_READS_FAIL_UNCAP_S": BROKER_READS_FAIL_UNCAP_S, "VERIFY_FLAT_RECHECK_S": VERIFY_FLAT_RECHECK_S,
    "VERIFY_FLAT_TIMEOUT_S": VERIFY_FLAT_TIMEOUT_S,
    "QUOTE_REQUEST_TIMEOUT_S": QUOTE_REQUEST_TIMEOUT_S, "INTENT_MAX_PER_MIN": INTENT_MAX_PER_MIN,
    "TIMER_DT": TIMER_DT,
    "LIQ_MIN_SPREAD_TICKS": LIQ_MIN_SPREAD_TICKS, "LIQ_DEPTH_LEVELS": LIQ_DEPTH_LEVELS,
    "WORST_CASE_PAD_RS": WORST_CASE_PAD_RS, "FUNDS_MAX_AGE_S": FUNDS_MAX_AGE_S,
    "FUNDS_PREMIUM_FACTOR": FUNDS_PREMIUM_FACTOR, "MONO_SKEW_S": MONO_SKEW_S,
    "LAST_TRADE_FUTURE_S": LAST_TRADE_FUTURE_S,
    "BAR_CHECK_PRICE_TOL": BAR_CHECK_PRICE_TOL, "BAR_CHECK_VOLUME_TOL": BAR_CHECK_VOLUME_TOL,
    "BAR_CHECK_SAMPLE": BAR_CHECK_SAMPLE,
    "PAPER_LATENCY_CLIP_MS": PAPER_LATENCY_CLIP_MS, "PAPER_DEPTH_WAIT_S": PAPER_DEPTH_WAIT_S,
    "PAPER_EXPIRED_GRACE_S": PAPER_EXPIRED_GRACE_S, "PAPER_MISSED_SQOFF_PX": PAPER_MISSED_SQOFF_PX,
    "PAPER_SQOFF_SELL_FACTOR": PAPER_SQOFF_SELL_FACTOR, "PAPER_SQOFF_BUY_FACTOR": PAPER_SQOFF_BUY_FACTOR,
    # titan2.clock: the paper exchange never fills against a quote stamped before the open + 30 s
    "OPENING_QUOTE_GRACE_S": OPENING_QUOTE_GRACE_S,
}


def canonical_json(obj: Any) -> str:
    """The one JSON text that version hashes are taken over (§10.2)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=repr)


SEMANTIC_HASH = hashlib.sha256(canonical_json(SEMANTIC_CONSTANTS).encode("utf-8")).hexdigest()


def exit_profile_problems(p: ExitProfile) -> list[str]:
    """Every way an ExitProfile breaks EXIT_BOUNDS (§7.2); a strategy whose profile has any is disabled."""
    out = []
    for name, (lo, hi) in EXIT_BOUNDS.items():
        v = getattr(p, name, None)
        if not _number(v):
            out.append(f"{name}: {v!r} is not a finite number")
        elif not lo <= v <= hi:
            out.append(f"{name}: {v!r} is outside [{lo}, {hi}]")
    if not isinstance(p.time_stop_when_locked, bool):
        out.append(f"time_stop_when_locked: {p.time_stop_when_locked!r} is not true or false")
    if not out:
        if p.lock_floor_pct > p.lock_arm_pct - EXIT_FLOOR_ARM_GAP_PCT:
            out.append(f"lock_floor_pct {p.lock_floor_pct!r} must be <= lock_arm_pct - {EXIT_FLOOR_ARM_GAP_PCT}")
        if not p.tp_pct > p.lock_arm_pct:
            out.append(f"tp_pct {p.tp_pct!r} must be above lock_arm_pct {p.lock_arm_pct!r}")
    return out


# ---------------------------------------------------------------------------------------------------------------
# The code defaults, checked once at import: a table edit that breaks them must fail loudly, never at runtime.
# ---------------------------------------------------------------------------------------------------------------
DEFAULTS = Settings()
if [f.name for f in fields(Settings)] != list(LIMITS):
    raise RuntimeError("titan2.settings: the Settings fields and LIMITS keys differ")
if DEFAULTS.problems():
    raise RuntimeError(f"titan2.settings: the code defaults break LIMITS: {DEFAULTS.problems()}")
