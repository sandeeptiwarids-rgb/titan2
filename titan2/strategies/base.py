"""Strategy plug-in API, intent factory, registry checks, version hash and the REGISTRY.lock tools (spec §10.1-10.2).

Rules every strategy follows (tests/unit/test_strategies.py and tests/arch check them):
- Pure. A strategy module imports only titan2.strategies.base, titan2.model and a few stdlib modules. It has no
  engine, broker, risk or store access; MarketView is its only window on the market.
- It never reads the clock. Time comes from ticks (recv_mono for intervals, recv_wall for clock-of-day), bars and
  on_timer(now); an intent emitted from on_timer takes its monotonic stamp from MarketView.now_mono().
- It only proposes. An Intent has no quantity: size() alone sizes (§8.5).

The signals thread drives each strategy through a StrategyRunner: a hook that raises disables the strategy for the
rest of the session (the host sends the alert), at most INTENT_MAX_PER_MIN intents pass per rolling minute, and an
intent stamped outside the strategy's WINDOW is dropped. Ticks before 09:15:00 never reach a strategy.

Version (§10.2): f"{LOGIC_VERSION}." + sha256(canonical JSON of the name, PARAMS, EXIT_PROFILE,
Settings.version_basis(): every versioned setting, ENGINE_SEMANTICS_VERSION, PAPER_MODEL_VERSION, SEMANTIC_HASH; code:
module_ast_hash, the AST hash of the strategy's module combined with this module's, which every strategy runs on; and
strategy_api: this module's alone)[:10]. Any change gives a new version, so a new G1 trial: silent tuning is
impossible, even through a code edit with REGISTRY.lock refreshed by hand.

REGISTRY.lock pins, per strategy, the AST hash of its module combined with this module's (module_ast_hash;
docstrings, comments and layout ignored), and the semantic constants hash. A code change of a strategy module or of
base.py without a LOGIC_VERSION bump (of every strategy, for base.py), or a SEMANTIC_CONSTANTS change without an
ENGINE_SEMANTICS_VERSION bump, fails the tests. After bumping, refresh the lock from agentic_trader/ with
UPDATE_LOCK_CMD.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import re
import uuid
from abc import ABC
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Protocol, Sequence

from titan2.model import (
    EXPIRY_RULE_NEAREST, Bar, Direction, ExitProfile, Intent, Recheck, StrikePref, StructureType, UnderlyingTick,
)

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # India has used a fixed +05:30 (no DST) since 1945
MARKET_OPEN = time(9, 15)        # strategies ignore ticks before the open (§9.5 step 14)
EARLIEST_ENTRY = time(9, 20)     # options have no pre-open and quote stale just after 09:15: no window starts earlier
UNIVERSES = ("STOCKS", "INDICES")
MAX_TTL_S = 10.0
DEFAULT_MAX_INTENTS_PER_MIN = 30  # == settings.INTENT_MAX_PER_MIN, which the host passes to StrategyRunner
EPS = 1e-9                       # float slack on percent/ratio thresholds: a value exactly at a bound counts as at it

DIRECTION_STRUCTURE: Mapping[Direction, StructureType] = {
    Direction.UP: StructureType.LONG_CE, Direction.DOWN: StructureType.LONG_PE,
}

UPDATE_LOCK_CMD = ('python -c "import sys, titan2.settings as t, titan2.strategies as s; '
                   'sys.exit(s.main(t.DEFAULTS))" --update-lock')


# ---------------------------------------------------------------------------------------------------------------
# Time helpers (stdlib only: strategies may not import titan2.clock)
# ---------------------------------------------------------------------------------------------------------------
def ist(dt: datetime) -> datetime:
    """The same instant in IST. A naive datetime is a host bug and raises (the runner then disables the strategy)."""
    if not isinstance(dt, datetime):
        raise TypeError(f"expected datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime {dt!r}: wall times must be timezone-aware")
    return dt.astimezone(IST)


def ist_tod(dt: datetime) -> time:
    """IST time of day (naive) of an aware datetime."""
    return ist(dt).time()


def ist_day(dt: datetime) -> date:
    """IST calendar date (the session date) of an aware datetime."""
    return ist(dt).date()


def parse_tod(text: str) -> time:
    """'HH:MM:SS' -> a naive time of day (IST implied)."""
    if not isinstance(text, str):
        raise TypeError(f"time of day must be 'HH:MM:SS', got {text!r}")
    t = time.fromisoformat(text)
    if t.tzinfo is not None:
        raise ValueError(f"time of day must not carry an offset: {text!r}")
    return t


def in_window(t: time, window: tuple[time, time]) -> bool:
    """window[0] <= t < window[1] (an emission window is half-open, like [ENTRY_START, NO_NEW_AFTER))."""
    return window[0] <= t < window[1]


def _finite(x: object) -> bool:
    """A finite int or float, never a bool."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---------------------------------------------------------------------------------------------------------------
# Plug-in API (§10.1)
# ---------------------------------------------------------------------------------------------------------------
class MarketView(Protocol):
    """Read-only market access, built by the signals thread. No engine, broker or risk access."""

    def ltp(self, underlying: str) -> tuple[float, float] | None: ...          # (price, age_s)

    def last_tick(self, underlying: str) -> UnderlyingTick | None: ...

    def bars(self, underlying: str, n: int) -> tuple[Bar, ...]: ...            # completed LIVE bars today, oldest first

    def prev_session_bars(self, underlying: str, n: int) -> tuple[Bar, ...]: ...  # HIST bars of the previous session

    def universe(self) -> frozenset[str]: ...                                  # eligible underlyings today

    def feed_ok(self) -> bool: ...

    def now_mono(self) -> float: ...
    # The host's monotonic time for the current hook call (e.g. the Timer's now_mono). Strategies use it only to
    # stamp intents emitted from on_timer/on_bar_close; tick-driven intents use the tick's recv_mono.


class Strategy(ABC):
    """Base of every strategy. One instance per strategy per process, driven only by the signals thread.

    The host builds it with the version computed by version(cls, settings) at registry load; every intent the
    strategy emits carries that version.
    """

    NAME: ClassVar[str]
    LOGIC_VERSION: ClassVar[int]
    PARAMS: ClassVar[Mapping[str, object]]
    EXIT_PROFILE: ClassVar[ExitProfile] = ExitProfile()
    UNIVERSE: ClassVar[str] = "STOCKS"             # "STOCKS" | "INDICES"
    WINDOW: ClassVar[tuple[str, str]]              # emission window, inside [ENTRY_START, NO_NEW_AFTER)

    def __init__(self, version: str) -> None:
        if not isinstance(version, str) or not version:
            raise ValueError(f"{type(self).__name__}: version must be a non-empty string, got {version!r}")
        self.version = version

    def on_tick(self, t: UnderlyingTick, mv: MarketView) -> Sequence[Intent]:
        return ()

    def on_bar_close(self, b: Bar, mv: MarketView) -> Sequence[Intent]:
        return ()

    def on_timer(self, now: datetime, mv: MarketView) -> Sequence[Intent]:
        return ()

    def on_feed_reset(self) -> None:
        """WS reconnect: drop baselines, pending sustains and bars in progress."""


_INTENT_ID_RE = re.compile(r"[0-9A-Za-z_:-]{1,64}")
_MAX_REASON_KEYS = 32
_MAX_REASON_TEXT = 200


def intent_problem(i: object) -> str | None:
    """The first way this object breaks the make_intent rules, else None. The gateway re-validates with it (§5.4)."""
    if not isinstance(i, Intent):
        return f"not an Intent: {type(i).__name__}"
    if not (isinstance(i.intent_id, str) and _INTENT_ID_RE.fullmatch(i.intent_id)):
        return f"bad intent_id {i.intent_id!r}"
    for name in ("strategy", "strategy_version", "underlying"):
        v = getattr(i, name)
        if not isinstance(v, str) or not v or v.strip() != v:
            return f"bad {name} {v!r}"
    try:
        direction, structure = Direction(i.direction), StructureType(i.structure)
        StrikePref(i.strike_pref)
    except ValueError as exc:
        return f"bad enum value: {exc}"
    if DIRECTION_STRUCTURE[direction] is not structure:      # phase 1: UP <-> LONG_CE, DOWN <-> LONG_PE (DB CHECK)
        return f"direction {direction} needs {DIRECTION_STRUCTURE[direction]}, got {structure}"
    if i.expiry_rule != EXPIRY_RULE_NEAREST:
        return f"expiry_rule must be {EXPIRY_RULE_NEAREST}, got {i.expiry_rule!r}"
    if not (_finite(i.trigger_px) and i.trigger_px > 0):
        return f"trigger_px must be a finite number > 0, got {i.trigger_px!r}"
    r = i.recheck
    if not isinstance(r, Recheck):
        return f"recheck must be a Recheck, got {type(r).__name__}"
    for v in (r.lo, r.hi):                                    # +-inf is allowed (an open side), NaN never
        if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
            return f"recheck bound must be a number, got {v!r}"
    if not r.lo <= r.hi:
        return f"recheck lo {r.lo!r} > hi {r.hi!r}"
    if not (_finite(i.ttl_s) and 0 < i.ttl_s <= MAX_TTL_S):
        return f"ttl_s must be in (0, {MAX_TTL_S:g}], got {i.ttl_s!r}"
    if not _finite(i.created_mono):
        return f"created_mono must be a finite number, got {i.created_mono!r}"
    if not isinstance(i.created_wall, datetime) or i.created_wall.utcoffset() is None:
        return f"created_wall must be an aware datetime, got {i.created_wall!r}"
    if not isinstance(i.reason, Mapping) or len(i.reason) > _MAX_REASON_KEYS:
        return f"reason must be a mapping of at most {_MAX_REASON_KEYS} entries"
    for k, v in i.reason.items():
        if not isinstance(k, str) or not k:
            return f"reason key must be a non-empty string, got {k!r}"
        if isinstance(v, str):
            if len(v) > _MAX_REASON_TEXT:
                return f"reason[{k!r}] is longer than {_MAX_REASON_TEXT} characters"
        elif not _finite(v):                                  # the intents.reason column must stay valid JSON
            return f"reason[{k!r}] must be a string or a finite number, got {v!r}"
    if i.legs:
        return "legs are phase 2 only"
    if not isinstance(i.is_placebo, bool):
        return f"is_placebo must be a bool, got {i.is_placebo!r}"
    if i.is_placebo != (i.parent_intent_id is not None):
        return "a placebo needs a parent_intent_id, and only a placebo has one"
    return None


def make_intent(cls: Any, version: str, *, underlying: str, direction: Direction | str,
                strike_pref: StrikePref | str, trigger_px: float, recheck: Recheck, ttl_s: float,
                now_mono: float, now_wall: datetime, reason: Mapping[str, float | str]) -> Intent:
    """The only way a strategy builds an Intent. Derives the structure (UP -> LONG_CE, DOWN -> LONG_PE) and
    validates every field (0 < ttl_s <= 10, trigger_px > 0, lo <= hi, reason values numeric/str). Raises ValueError."""
    name = getattr(cls, "NAME", None)
    try:
        direction, strike_pref = Direction(direction), StrikePref(strike_pref)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None
    if not isinstance(reason, Mapping):
        raise ValueError(f"{name}: reason must be a mapping, got {type(reason).__name__}")
    intent = Intent(
        intent_id=uuid.uuid4().hex[:12], strategy=name, strategy_version=version, underlying=underlying,
        direction=direction, structure=DIRECTION_STRUCTURE[direction], strike_pref=strike_pref,
        expiry_rule=EXPIRY_RULE_NEAREST, trigger_px=trigger_px, recheck=recheck, ttl_s=ttl_s,
        created_mono=now_mono, created_wall=now_wall, reason=dict(reason),
    )
    problem = intent_problem(intent)
    if problem is not None:
        raise ValueError(f"{name}: {problem}")
    return intent


# ---------------------------------------------------------------------------------------------------------------
# Host side: one runner per strategy on the signals thread (§10.1 failure and rate rules)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunResult:
    intents: tuple[Intent, ...] = ()               # post each as IntentReceived
    dropped: tuple[tuple[str, str], ...] = ()      # (intent_id, "WINDOW" | "RATE"): log an event for each
    error: str | None = None                       # set on the one call that disabled the strategy: alert on it


class StrategyRunner:
    """Calls one strategy's hooks and applies the rules every strategy shares (§10.1, §9.5 step 14).

    - A hook that raises, or returns anything but a list/tuple of valid Intents of this strategy and version,
      disables the strategy for the rest of the session (fails closed); that call's RunResult.error says why.
    - At most max_per_min intents pass per rolling 60 s (by created_mono); the excess is dropped ("RATE").
    - An intent stamped outside the strategy's WINDOW is dropped ("WINDOW").
    - Ticks stamped before 09:15:00 IST are not delivered.
    """

    def __init__(self, strategy: Strategy, max_per_min: int = DEFAULT_MAX_INTENTS_PER_MIN) -> None:
        if not isinstance(strategy, Strategy):
            raise TypeError(f"expected a Strategy, got {type(strategy).__name__}")
        if isinstance(max_per_min, bool) or not isinstance(max_per_min, int) or max_per_min < 0:
            raise ValueError(f"max_per_min must be an int >= 0, got {max_per_min!r}")
        lo, hi = type(strategy).WINDOW
        self.strategy = strategy
        self.name: str = type(strategy).NAME
        self.version: str = strategy.version
        self.window = (parse_tod(lo), parse_tod(hi))
        self.max_per_min = max_per_min
        self.disabled: str | None = None
        self._passed: deque[float] = deque()        # created_mono of the intents passed in the last 60 s

    def on_tick(self, t: UnderlyingTick, mv: MarketView) -> RunResult:
        return self._run("on_tick", lambda: () if ist_tod(t.recv_wall) < MARKET_OPEN else self.strategy.on_tick(t, mv))

    def on_bar_close(self, b: Bar, mv: MarketView) -> RunResult:
        return self._run("on_bar_close", lambda: self.strategy.on_bar_close(b, mv))

    def on_timer(self, now: datetime, mv: MarketView) -> RunResult:
        return self._run("on_timer", lambda: self.strategy.on_timer(now, mv))

    def on_feed_reset(self) -> RunResult:
        return self._run("on_feed_reset", lambda: self.strategy.on_feed_reset() or ())

    def _run(self, hook: str, call: Callable[[], object]) -> RunResult:
        if self.disabled is not None:
            return RunResult()
        try:
            intents = self._checked(call())
        except Exception as exc:  # any strategy failure: stop it for the session and tell the host (never silent)
            self.disabled = f"{self.name}.{hook} raised {exc!r}"[:500]
            return RunResult(error=self.disabled)
        return self._admit(intents)

    def _checked(self, out: object) -> tuple[Intent, ...]:
        if out is None:
            return ()
        if not isinstance(out, (list, tuple)):
            raise TypeError(f"hook returned {type(out).__name__}, not a list or tuple of Intents")
        for i in out:
            problem = intent_problem(i)
            if problem is not None:
                raise ValueError(f"invalid intent: {problem}")
            if i.strategy != self.name or i.strategy_version != self.version or i.is_placebo:
                raise ValueError(f"intent {i.intent_id} is not a {self.name} {self.version} strategy intent")
        return tuple(out)

    def _admit(self, intents: tuple[Intent, ...]) -> RunResult:
        passed: list[Intent] = []
        dropped: list[tuple[str, str]] = []
        for i in intents:
            if not in_window(ist_tod(i.created_wall), self.window):
                dropped.append((i.intent_id, "WINDOW"))
                continue
            while self._passed and i.created_mono - self._passed[0] >= 60.0:
                self._passed.popleft()
            if len(self._passed) >= self.max_per_min:
                dropped.append((i.intent_id, "RATE"))
                continue
            self._passed.append(i.created_mono)
            passed.append(i)
        return RunResult(tuple(passed), tuple(dropped))


# ---------------------------------------------------------------------------------------------------------------
# Registry checks and the version hash (§10.2)
# ---------------------------------------------------------------------------------------------------------------
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")      # names starting with "_" are reserved (LIVE:_selftest)


def strategy_problems(cls: Any, settings: Any, profile_problems: Callable[[ExitProfile], Sequence[str]]
                      ) -> list[str]:
    """Every reason this class cannot be loaded; [] when it can. A strategy with problems is disabled (§7.2).

    profile_problems is titan2.settings.exit_profile_problems (EXIT_BOUNDS); it is passed in because strategies
    may not import the settings module.
    """
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        return [f"{cls!r} is not a Strategy subclass"]
    out: list[str] = []
    name = getattr(cls, "NAME", None)
    if not (isinstance(name, str) and _NAME_RE.fullmatch(name)):
        out.append(f"NAME {name!r} must match {_NAME_RE.pattern}")
    lv = getattr(cls, "LOGIC_VERSION", None)
    if isinstance(lv, bool) or not isinstance(lv, int) or lv < 1:
        out.append(f"LOGIC_VERSION {lv!r} must be an int >= 1")
    params = getattr(cls, "PARAMS", None)
    if not isinstance(params, Mapping) or not all(isinstance(k, str) for k in params):
        out.append("PARAMS must be a mapping with string keys")
    else:
        try:
            json.dumps(dict(params), sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            out.append(f"PARAMS must be plain finite JSON values: {exc}")
    profile = getattr(cls, "EXIT_PROFILE", None)
    if not isinstance(profile, ExitProfile):
        out.append(f"EXIT_PROFILE must be an ExitProfile, got {type(profile).__name__}")
    else:
        out.extend(f"EXIT_PROFILE {p}" for p in profile_problems(profile))
    if getattr(cls, "UNIVERSE", None) not in UNIVERSES:
        out.append(f"UNIVERSE {getattr(cls, 'UNIVERSE', None)!r} must be one of {UNIVERSES}")
    try:
        lo, hi = (parse_tod(x) for x in getattr(cls, "WINDOW"))
    except (AttributeError, TypeError, ValueError) as exc:
        out.append(f"WINDOW must be ('HH:MM:SS', 'HH:MM:SS'): {exc}")
    else:
        if not lo < hi:
            out.append(f"WINDOW {cls.WINDOW} is empty")
        if lo < EARLIEST_ENTRY:
            out.append(f"WINDOW starts before {EARLIEST_ENTRY.isoformat()} (opening option quotes are stale)")
        if hi <= settings.ENTRY_START or lo >= settings.NO_NEW_AFTER:
            out.append(f"WINDOW {cls.WINDOW} never overlaps [ENTRY_START, NO_NEW_AFTER) = "
                       f"[{settings.ENTRY_START.isoformat()}, {settings.NO_NEW_AFTER.isoformat()})")
    return out


def canonical_json(obj: Any) -> str:
    """The one JSON text version hashes are taken over (same as titan2.settings.canonical_json)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=repr)


def version_inputs(cls: Any, settings: Any) -> dict[str, Any]:
    """Everything a strategy version hashes (stored as g1_trials.version_inputs). code is module_ast_hash(cls), the
    strategy module's own code combined with base.py's, and strategy_api is base.py's alone: an edit of either starts
    a new G1 trial even if REGISTRY.lock were refreshed by hand."""
    basis = settings.version_basis()
    return {
        "name": cls.NAME, "params": dict(cls.PARAMS), "exit_profile": asdict(cls.EXIT_PROFILE),
        "settings": basis["settings"], "engine": basis["engine"], "paper_model": basis["paper_model"],
        "semantic_constants": basis["semantic_constants"], "code": module_ast_hash(cls),
        "strategy_api": strategy_api_hash(),
    }


def version(cls: Any, settings: Any) -> str:
    """f"{LOGIC_VERSION}." + the first 10 hex digits of sha256(canonical_json(version_inputs))."""
    digest = hashlib.sha256(canonical_json(version_inputs(cls, settings)).encode("utf-8")).hexdigest()
    return f"{cls.LOGIC_VERSION}.{digest[:10]}"


# ---------------------------------------------------------------------------------------------------------------
# REGISTRY.lock: {name: {logic_version, ast_hash}, "_engine": {engine_semantics_version, semantic_constants_hash}}
# ---------------------------------------------------------------------------------------------------------------
ENGINE_KEY = "_engine"
_KEYS = {ENGINE_KEY: ("engine_semantics_version", "semantic_constants_hash")}
_STRATEGY_KEYS = ("logic_version", "ast_hash")


class LockError(ValueError):
    """The lock cannot be refreshed: some change lacks its version bump. .problems lists each one."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        ValueError.__init__(self, "; ".join(self.problems))


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _canonical_ast(node: Any) -> str:
    """ast.dump(include_attributes=False) without None and empty-list fields, so the text is the same on Python
    3.10-3.13 (3.12 added FunctionDef.type_params; 3.13 changed ast.dump's defaults)."""
    if isinstance(node, ast.AST):
        fields = [f"{k}={_canonical_ast(v)}" for k, v in ast.iter_fields(node) if v is not None and v != []]
        return f"{type(node).__name__}({','.join(fields)})"
    if isinstance(node, list):
        return "[" + ",".join(_canonical_ast(x) for x in node) + "]"
    return repr(node)


def ast_hash(source: str) -> str:
    """sha256 of the module's AST: docstrings, comments and layout do not count; any code or literal change does."""
    return hashlib.sha256(_canonical_ast(_strip_docstrings(ast.parse(source))).encode("utf-8")).hexdigest()


def strategy_api_hash() -> str:
    """ast_hash of this module, strategies/base.py: every strategy runs on it (StrategyRunner, make_intent,
    intent_problem, the hooks' defaults), so it is part of every strategy's version and REGISTRY.lock entry."""
    return ast_hash(Path(__file__).read_text(encoding="utf-8"))


def module_ast_hash(cls: type) -> str:
    """The code hash REGISTRY.lock pins for cls, and version_inputs' code: sha256 of the ast_hash of the whole module
    that defines it (its helpers and constants are part of its logic) and strategy_api_hash() (base.py). Both are
    looked up as module globals at call time, so a test can stand in an edited strategy module or base.py."""
    path = inspect.getsourcefile(cls)
    if path is None:
        raise ValueError(f"no source file for {cls!r}")
    own = ast_hash(Path(path).read_text(encoding="utf-8"))
    return hashlib.sha256(f"{own}|{strategy_api_hash()}".encode("utf-8")).hexdigest()


def lock_snapshot(registry: Mapping[str, Any], engine_semantics_version: int, semantic_constants_hash: str
                  ) -> dict[str, dict[str, Any]]:
    """What REGISTRY.lock must hold for this code."""
    out: dict[str, dict[str, Any]] = {
        name: {"logic_version": cls.LOGIC_VERSION, "ast_hash": module_ast_hash(cls)}
        for name, cls in sorted(registry.items())
    }
    out[ENGINE_KEY] = {"engine_semantics_version": engine_semantics_version,
                       "semantic_constants_hash": semantic_constants_hash}
    return out


def _entry_ok(entry: object, keys: tuple[str, str]) -> bool:
    return (isinstance(entry, dict) and set(entry) == set(keys) and isinstance(entry[keys[0]], int)
            and not isinstance(entry[keys[0]], bool) and isinstance(entry[keys[1]], str))


def lock_problems(pinned: Mapping[str, Any] | None, current: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(violations, stale). Violations need a version bump first; stale entries only need UPDATE_LOCK_CMD."""
    pinned = {} if pinned is None else pinned
    violations: list[str] = []
    stale: list[str] = []
    for name in sorted(set(pinned) | set(current)):
        vkey, hkey = _KEYS.get(name, _STRATEGY_KEYS)
        bump = "ENGINE_SEMANTICS_VERSION in titan2/settings.py" if name == ENGINE_KEY else f"{name}.LOGIC_VERSION"
        old, new = pinned.get(name), current.get(name)
        if new is None:
            stale.append(f"{name}: pinned in REGISTRY.lock but no longer registered")
        elif old is None:
            stale.append(f"{name}: not pinned in REGISTRY.lock yet")
        elif not _entry_ok(old, (vkey, hkey)):
            violations.append(f"{name}: REGISTRY.lock entry is malformed ({old!r}); restore the file from git")
        elif new[vkey] < old[vkey]:
            violations.append(f"{name}: {vkey} went down ({old[vkey]} -> {new[vkey]}); versions never decrease")
        elif new[hkey] != old[hkey] and new[vkey] == old[vkey]:
            what = "SEMANTIC_CONSTANTS changed" if name == ENGINE_KEY else "the strategy code changed"
            violations.append(f"{name}: {what} but {vkey} is still {old[vkey]}: bump {bump} "
                              f"(a new version starts a new G1 trial), then refresh the lock")
        elif new != old:
            stale.append(f"{name}: {vkey} {old[vkey]} -> {new[vkey]}")
    return violations, stale


def read_lock(path: Path) -> dict[str, Any] | None:
    """The pinned lock, or None when the file does not exist. Raises ValueError when it is not a JSON object."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold one JSON object")
    return data


def write_lock(path: Path, data: Mapping[str, Any]) -> None:
    """Replace the lock file atomically."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)
