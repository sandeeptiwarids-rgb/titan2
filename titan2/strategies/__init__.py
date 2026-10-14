"""The strategy registry (spec §10.2), registry load and the REGISTRY.lock helper.

Every strategy starts in PAPER (§10.5). load() refuses a strategy whose class breaks the plug-in rules or whose
ExitProfile is outside EXIT_BOUNDS; it is then disabled and the host alerts. price_spike_down (2026-10-12) is a
PAPER experiment the user asked for against brief E1; it is registered last, so on a tick that reaches every
strategy the earlier ones still run, and post their intents, first.

Lock helper (run from agentic_trader/ after bumping a LOGIC_VERSION or ENGINE_SEMANTICS_VERSION):
    python -c "import sys, titan2.settings as t, titan2.strategies as s; sys.exit(s.main(t.DEFAULTS))" --update-lock
It refuses while any code or SEMANTIC_CONSTANTS change still lacks its version bump. --check (the default) only
reports. The settings object is passed in because strategy modules may not import titan2.settings.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from titan2.model import ExitProfile
from titan2.strategies.base import (
    DEFAULT_MAX_INTENTS_PER_MIN, UPDATE_LOCK_CMD, LockError, MarketView, RunResult, Strategy, StrategyRunner,
    intent_problem, lock_problems, lock_snapshot, make_intent, read_lock, strategy_problems, version,
    version_inputs, write_lock,
)
from titan2.strategies.opening_drive import OpeningDrive
from titan2.strategies.price_spike_down import PriceSpikeDown
from titan2.strategies.price_spike_up import PriceSpikeUp

__all__ = [
    "REGISTRY", "LOCK_PATH", "load", "current_lock", "lock_status", "update_lock", "main",
    "LockError", "MarketView", "RunResult", "Strategy", "StrategyRunner", "intent_problem", "make_intent",
    "strategy_problems", "version", "version_inputs", "OpeningDrive", "PriceSpikeDown", "PriceSpikeUp",
]

REGISTRY: Mapping[str, type[Strategy]] = MappingProxyType({
    "opening_drive": OpeningDrive,
    "price_spike_up": PriceSpikeUp,
    "price_spike_down": PriceSpikeDown,      # 2026-10-12, PAPER experiment (user request, against brief E1)
})

LOCK_PATH = Path(__file__).resolve().with_name("REGISTRY.lock")


def load(settings: Any, profile_problems: Callable[[ExitProfile], Sequence[str]], *,
         max_per_min: int = DEFAULT_MAX_INTENTS_PER_MIN, registry: Mapping[str, type[Strategy]] | None = None
         ) -> tuple[dict[str, StrategyRunner], dict[str, tuple[str, ...]]]:
    """({name: runner} for every loadable strategy, {name: problems} for every refused one).

    settings is the process's frozen Settings; profile_problems is titan2.settings.exit_profile_problems;
    max_per_min is settings.INTENT_MAX_PER_MIN. Each runner's .version is version(cls, settings).
    """
    runners: dict[str, StrategyRunner] = {}
    failed: dict[str, tuple[str, ...]] = {}
    for name, cls in (REGISTRY if registry is None else registry).items():
        problems = strategy_problems(cls, settings, profile_problems)
        if not problems and cls.NAME != name:
            problems = [f"registered as {name!r} but NAME is {cls.NAME!r}"]
        if problems:
            failed[name] = tuple(problems)
            continue
        try:
            runners[name] = StrategyRunner(cls(version(cls, settings)), max_per_min)
        except Exception as exc:  # a strategy that cannot be built does not run; the host alerts on `failed`
            failed[name] = (f"cannot build: {exc!r}",)
    return runners, failed


def current_lock(settings: Any) -> dict[str, dict[str, Any]]:
    """What REGISTRY.lock must hold for this code (engine part from settings.version_basis())."""
    basis = settings.version_basis()
    return lock_snapshot(REGISTRY, basis["engine"], basis["semantic_constants"])


def lock_status(settings: Any, path: Path = LOCK_PATH) -> tuple[list[str], list[str]]:
    """(violations, stale) of the pinned lock against this code; both empty when the lock is current."""
    return lock_problems(read_lock(path), current_lock(settings))


def update_lock(settings: Any, path: Path = LOCK_PATH) -> list[str]:
    """Rewrite the lock for this code and return what changed. Raises LockError while a version bump is missing,
    leaving the file untouched."""
    pinned = read_lock(path)
    current = current_lock(settings)
    violations, stale = lock_problems(pinned, current)
    if violations:
        raise LockError(violations)
    if pinned != current:
        write_lock(path, current)
    return stale


def main(settings: Any, argv: Sequence[str] | None = None, path: Path = LOCK_PATH) -> int:
    """`--check` (default) or `--update-lock`. 0 when the lock is (now) current, 2 otherwise."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--check"], ["--update-lock"]):
        print(f"usage: {UPDATE_LOCK_CMD.replace('--update-lock', '[--check | --update-lock]')}", file=sys.stderr)
        return 2
    try:
        if args == ["--update-lock"]:
            changes = update_lock(settings, path)
            print(f"{path}: " + ("; ".join(changes) if changes else "already current"))
            return 0
        violations, stale = lock_status(settings, path)
    except LockError as exc:
        print(f"{path} NOT updated:\n  " + "\n  ".join(exc.problems), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"{path} is unreadable ({exc}); restore it from git", file=sys.stderr)
        return 2
    for line in violations + stale:
        print(f"  {line}", file=sys.stderr)
    if violations or stale:
        print(f"{path} is not current" + ("" if violations else f"; run: {UPDATE_LOCK_CMD}"), file=sys.stderr)
        return 2
    print(f"{path}: current")
    return 0
