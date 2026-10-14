"""U15: titan2.strategies: pure plug-ins, make_intent, the runner rules, the version hash, REGISTRY.lock, and the
PAPER strategies opening_drive (§10.3), price_spike_up (§10.4) and price_spike_down, its signed mirror (a PAPER
experiment added on 2026-10-12 at the user's request, against brief E1)."""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

import titan2.strategies as strategies
from titan2 import settings as st
from titan2.model import (
    EXPIRY_RULE_NEAREST, Bar, Direction, ExitProfile, Intent, Recheck, StrikePref, StructureType, UnderlyingTick,
)
from titan2.settings import DEFAULTS, exit_profile_problems
from titan2.strategies import REGISTRY, base
from titan2.strategies.base import (
    IST, LockError, Strategy, StrategyRunner, ast_hash, intent_problem, lock_problems, make_intent, version,
    version_inputs,
)
from titan2.strategies.opening_drive import OpeningDrive
from titan2.strategies.price_spike_down import PriceSpikeDown
from titan2.strategies.price_spike_up import PriceSpikeUp

DAY = date(2026, 10, 12)          # a Monday
PREV_DAY = date(2026, 10, 9)      # the previous session (Friday)
NEXT_DAY = date(2026, 10, 13)
STRATEGY_DIR = Path(base.__file__).resolve().parent
AGENTIC_TRADER = STRATEGY_DIR.parents[1]


def at(hms: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hms), tzinfo=IST)


class FakeMV:
    """A scripted MarketView. bars()/prev_session_bars() return everything stored, like a host that over-returns."""

    def __init__(self, universe=(), bars=None, prev=None, feed_ok=True, mono=5000.0):
        self._universe = frozenset(universe)
        self._bars = bars or {}
        self._prev = prev or {}
        self.ok = feed_ok
        self.mono = mono

    def ltp(self, u):
        return None

    def last_tick(self, u):
        return None

    def bars(self, u, n):
        return tuple(self._bars.get(u, ()))

    def prev_session_bars(self, u, n):
        return tuple(self._prev.get(u, ()))

    def universe(self):
        return self._universe

    def feed_ok(self):
        return self.ok

    def now_mono(self):
        return self.mono


# ---------------------------------------------------------------------------------------------------------------
# opening_drive helpers
# ---------------------------------------------------------------------------------------------------------------
def obar(sym, o, h, lo, c, v, valid=True, start="09:15:00", day=DAY, source="LIVE"):
    return Bar(sym, at(start, day), o, h, lo, c, v, valid, source)


def hist(sym, vols, day=PREV_DAY, valid=True):
    """The previous session's last len(vols) 5-minute HIST bars, ending with the 15:35 bar."""
    last = at("15:35:00", day)
    return [Bar(sym, last - timedelta(minutes=5 * (len(vols) - 1 - k)), 100.0, 101.0, 99.0, 100.0, v, valid, "HIST")
            for k, v in enumerate(vols)]


def od_market(bars: dict, vols=None, **kw) -> FakeMV:
    """Every symbol in bars gets 20 previous-session bars of volume 1000 (mean 1000, so the 3x bar needs 3000)."""
    vols = [1000] * 20 if vols is None else vols
    return FakeMV(universe=bars, bars={u: [b] for u, b in bars.items()}, prev={u: hist(u, vols) for u in bars}, **kw)


# ---------------------------------------------------------------------------------------------------------------
# price_spike_up helpers
# ---------------------------------------------------------------------------------------------------------------
class Tape:
    """Feeds one symbol's ticks to a PriceSpikeUp at start + sec seconds (recv_mono = 1000 + sec)."""

    def __init__(self, start="10:00:00", sym="NSE:RELIANCE", strat=None, mv=None, step_vol=100):
        self.s = strat or PriceSpikeUp("1.test")
        self.start = at(start)
        self.sym = sym
        self.mv = mv or FakeMV([sym])
        self.cum = 10_000
        self.step = step_vol
        self.out: list[Intent] = []

    def tick(self, sec, ltp, vol=None):
        self.cum += self.step if vol is None else vol
        t = UnderlyingTick(self.sym, 1, ltp, self.cum, None, None, None, 1000.0 + sec,
                           self.start + timedelta(seconds=sec))
        got = list(self.s.on_tick(t, self.mv))
        self.out += got
        return got

    def run(self, secs, ltp):
        for sec in secs:
            self.tick(sec, ltp)

    def pending(self):
        state = self.s._syms.get(self.sym)
        return None if state is None else state.pending

    def spike(self, move_px=100.7, at_sec=5):
        """Flat at 100.0 for at_sec seconds (A = 100.0), then one tick at move_px."""
        self.run(range(at_sec), 100.0)
        self.tick(at_sec, move_px)


# ---------------------------------------------------------------------------------------------------------------
# Purity, syntax and provenance
# ---------------------------------------------------------------------------------------------------------------
STRATEGY_STDLIB = {"__future__", "collections", "dataclasses", "datetime", "math", "types", "typing"}
NEVER_STDLIB = {"time", "random", "threading", "socket", "subprocess", "sqlite3", "asyncio", "multiprocessing",
                "urllib", "http", "ssl", "signal", "os", "shutil"}
CLOCK_ATTRS = {"now", "utcnow", "today", "monotonic", "perf_counter", "time_ns", "monotonic_ns", "process_time"}


def _imported(tree: ast.AST):
    """Every imported module name; a relative import yields '.'-prefixed text (and fails the checks)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield "." * node.level + (node.module or "")


def test_import_purity():
    files = sorted(STRATEGY_DIR.glob("*.py"))
    assert {f.name for f in files} == {"__init__.py", "base.py", "opening_drive.py", "price_spike_down.py",
                                       "price_spike_up.py"}
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        is_strategy = f.name not in ("__init__.py", "base.py")
        allowed_titan2 = {"titan2.model", "titan2.strategies.base"}
        if f.name == "__init__.py":
            allowed_titan2 |= {"titan2.strategies.opening_drive", "titan2.strategies.price_spike_down",
                               "titan2.strategies.price_spike_up"}
        for name in _imported(tree):
            assert not name.startswith("."), f"{f.name}: relative import {name}"
            top = name.split(".")[0]
            if top == "titan2":
                assert name in allowed_titan2, f"{f.name} imports {name}"
            else:
                assert top in sys.stdlib_module_names, f"{f.name} imports non-stdlib {name}"
                assert top not in NEVER_STDLIB, f"{f.name} imports {name}"
                if is_strategy:
                    assert top in STRATEGY_STDLIB, f"strategy module {f.name} imports {name}"
        # never reads the clock, never opens files
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in CLOCK_ATTRS, f"{f.name}:{node.lineno} reads the clock via .{node.attr}"
            if is_strategy and isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("open", "eval", "exec", "__import__"), f"{f.name}:{node.lineno}"
    # importing the package pulls in nothing else from titan2 (no settings, clock, core, broker, market, store, dash)
    code = "import sys, titan2.strategies; print(sorted(m for m in sys.modules if m.startswith('titan2')))"
    res = subprocess.run([sys.executable, "-c", code], cwd=AGENTIC_TRADER, capture_output=True, text=True,
                         timeout=60)
    assert res.returncode == 0, res.stderr
    assert ast.literal_eval(res.stdout.strip()) == [
        "titan2", "titan2.model", "titan2.strategies", "titan2.strategies.base", "titan2.strategies.opening_drive",
        "titan2.strategies.price_spike_down", "titan2.strategies.price_spike_up"]
    # every registered strategy lives in this package
    for cls in REGISTRY.values():
        assert cls.__module__.startswith("titan2.strategies.")


def test_python310_syntax_and_port_headers():
    for f in sorted(STRATEGY_DIR.glob("*.py")):
        ast.parse(f.read_text(encoding="utf-8"), feature_version=(3, 10))
    src = (STRATEGY_DIR / "price_spike_up.py").read_text(encoding="utf-8")
    assert "# Ported from agentic_trader/kite_ticker.py:1407-1435, 1507-1533, 2049-2108 @b4694f3 — changes:" in src
    assert "# Ported from agentic_trader/kite_ticker.py:1701-1848 @b4694f3 — changes:" in src
    down = (STRATEGY_DIR / "price_spike_down.py").read_text(encoding="utf-8")
    assert "# Mirror of titan2/strategies/price_spike_up.py v1 (2026-10-12)" in down
    assert "at the user's request, against brief E1" in ast.get_docstring(ast.parse(down))


# ---------------------------------------------------------------------------------------------------------------
# make_intent and intent_problem
# ---------------------------------------------------------------------------------------------------------------
def _mk(**over):
    kw = dict(underlying="NSE:RELIANCE", direction="UP", strike_pref="ATM", trigger_px=100.0,
              recheck=Recheck(99.0, 101.0), ttl_s=10.0, now_mono=5.0, now_wall=at("10:00:00"), reason={"m": 0.7})
    kw.update(over)
    return make_intent(PriceSpikeUp, "1.test", **kw)


def test_make_intent_rules():
    i = _mk()
    assert (i.strategy, i.strategy_version, i.direction, i.structure) == (
        "price_spike_up", "1.test", Direction.UP, StructureType.LONG_CE)
    assert i.strike_pref is StrikePref.ATM and i.expiry_rule == EXPIRY_RULE_NEAREST
    assert re.fullmatch(r"[0-9a-f]{12}", i.intent_id) and not i.is_placebo and i.legs == ()
    assert _mk(direction=Direction.DOWN, strike_pref=StrikePref.ITM1).structure is StructureType.LONG_PE
    assert len({_mk().intent_id for _ in range(500)}) == 500
    _mk(recheck=Recheck(-math.inf, math.inf))                    # an open recheck side is allowed
    _mk(ttl_s=0.001)
    bad = [dict(ttl_s=0), dict(ttl_s=10.01), dict(ttl_s=math.nan), dict(ttl_s=True), dict(trigger_px=0.0),
           dict(trigger_px=-1.0), dict(trigger_px=math.inf), dict(trigger_px="100"),
           dict(recheck=Recheck(101.0, 99.0)), dict(recheck=Recheck(math.nan, 101.0)), dict(recheck=(99.0, 101.0)),
           dict(now_wall=datetime(2026, 10, 12, 10)),
           dict(now_mono=math.nan), dict(reason={"m": math.nan}), dict(reason={"m": True}), dict(reason={"m": None}),
           dict(reason={1: 2.0}), dict(reason=[("m", 1.0)]), dict(reason={"k": "x" * 201}),
           dict(direction="SIDEWAYS"), dict(strike_pref="OTM1"), dict(underlying=""), dict(underlying=" NSE:X")]
    for over in bad:
        with pytest.raises(ValueError):
            _mk(**over)
    with pytest.raises(ValueError):
        make_intent(PriceSpikeUp, "", underlying="NSE:X", direction="UP", strike_pref="ATM", trigger_px=1.0,
                    recheck=Recheck(0.5, 2.0), ttl_s=1.0, now_mono=1.0, now_wall=at("10:00:00"), reason={})
    # the gateway re-validates with intent_problem
    assert intent_problem(i) is None
    assert intent_problem("not an intent") is not None
    assert "LONG_CE" in intent_problem(dataclasses.replace(i, direction=Direction.DOWN))
    assert intent_problem(dataclasses.replace(i, is_placebo=True)) is not None           # placebo needs a parent
    assert intent_problem(dataclasses.replace(i, is_placebo=True, parent_intent_id="abc123")) is None
    assert intent_problem(dataclasses.replace(i, parent_intent_id="abc123")) is not None
    assert intent_problem(dataclasses.replace(i, expiry_rule="WEEKLY")) is not None


# ---------------------------------------------------------------------------------------------------------------
# The runner: disable on raise, 30/min, WINDOW, no ticks before 09:15
# ---------------------------------------------------------------------------------------------------------------
class _Scripted(Strategy):
    NAME = "scripted"
    LOGIC_VERSION = 1
    PARAMS = {}
    WINDOW = ("09:20:00", "14:55:00")

    def __init__(self, version, emit=None):
        super().__init__(version)
        self.emit = emit or (lambda now: ())
        self.ticks = []

    def on_tick(self, t, mv):
        self.ticks.append(t.recv_wall)
        return ()

    def on_timer(self, now, mv):
        return self.emit(now)


def _scripted_intent(now, mono, name="scripted", ver="1.s"):
    return dataclasses.replace(_mk(now_wall=now, now_mono=mono), strategy=name, strategy_version=ver)


def test_runner_rules():
    mv = FakeMV()
    # a hook that raises disables the strategy for the session; the error is reported exactly once
    def boom(now):
        raise RuntimeError("bad data")
    r = StrategyRunner(_Scripted("1.s", boom))
    res = r.on_timer(at("10:00:00"), mv)
    assert res.intents == () and "RuntimeError" in res.error and r.disabled == res.error
    assert r.on_timer(at("10:00:01"), mv) == base.RunResult()
    # bad output also disables: not a list/tuple, not an Intent, or another strategy's intent
    for out in ("x", [object()], [_scripted_intent(at("10:00:00"), 1.0, name="other")]):
        r = StrategyRunner(_Scripted("1.s", lambda now, out=out: out))
        assert r.on_timer(at("10:00:00"), mv).error is not None and r.disabled
    # at most 30 intents per rolling minute (by created_mono); the excess is dropped and reported
    r = StrategyRunner(_Scripted("1.s", lambda now: [_scripted_intent(now, 100.0 + k / 100) for k in range(31)]))
    res = r.on_timer(at("10:00:00"), mv)
    assert len(res.intents) == 30 and [why for _, why in res.dropped] == ["RATE"]
    assert len(r.on_timer(at("10:00:30"), mv).intents) == 0                    # same minute: still capped
    r.strategy.emit = lambda now: [_scripted_intent(now, 160.0 + k / 100) for k in range(31)]
    assert len(r.on_timer(at("10:01:00"), mv).intents) == 30                   # 60 s later the window has moved on
    assert r.max_per_min == st.INTENT_MAX_PER_MIN == base.DEFAULT_MAX_INTENTS_PER_MIN
    # intents stamped outside the strategy's WINDOW are dropped (half-open: the end is excluded)
    r = StrategyRunner(_Scripted("1.s", lambda now: [_scripted_intent(now, 1.0)]))
    assert [w for _, w in r.on_timer(at("09:19:59.999"), mv).dropped] == ["WINDOW"]
    assert len(r.on_timer(at("09:20:00"), mv).intents) == 1
    assert [w for _, w in r.on_timer(at("14:55:00"), mv).dropped] == ["WINDOW"]
    # ticks before 09:15:00 never reach a strategy
    s = _Scripted("1.s")
    r = StrategyRunner(s)
    for hms in ("09:14:59.999", "09:15:00"):
        r.on_tick(UnderlyingTick("NSE:X", 1, 100.0, 1, None, None, None, 1.0, at(hms)), mv)
    assert s.ticks == [at("09:15:00")]
    # a naive wall time is a host bug: fail closed
    r.on_tick(UnderlyingTick("NSE:X", 1, 100.0, 1, None, None, None, 1.0, datetime(2026, 10, 12, 10)), mv)
    assert r.disabled is not None


# ---------------------------------------------------------------------------------------------------------------
# Registry load and the version hash
# ---------------------------------------------------------------------------------------------------------------
def test_registry_load():
    runners, failed = strategies.load(DEFAULTS, exit_profile_problems, max_per_min=st.INTENT_MAX_PER_MIN)
    assert failed == {} and set(runners) == {"opening_drive", "price_spike_up", "price_spike_down"} == set(REGISTRY)
    for name, r in runners.items():
        assert r.name == name and r.version == version(REGISTRY[name], DEFAULTS) and r.disabled is None
    assert runners["opening_drive"].window == (time(9, 20), time(9, 20, 10))
    assert runners["price_spike_up"].window == (time(9, 20), time(14, 55))
    assert runners["price_spike_down"].window == (time(9, 20), time(14, 55))

    class WideStop(PriceSpikeUp):
        EXIT_PROFILE = ExitProfile(hard_sl_pct=30.0)            # the backtest's SL30 violates EXIT_BOUNDS (X4)

    class Early(PriceSpikeUp):
        WINDOW = ("09:15:00", "14:55:00")

    class BadParams(PriceSpikeUp):
        PARAMS = {"x": math.nan}

    class Raises(PriceSpikeUp):
        NAME = "boom"

        def __init__(self, version):
            raise RuntimeError("no")

    reg = {"price_spike_up": WideStop, "early": Early, "bad": BadParams, "boom": Raises, "opening_drive": PriceSpikeUp}
    runners, failed = strategies.load(DEFAULTS, exit_profile_problems, registry=reg)
    assert runners == {}
    assert any("hard_sl_pct" in p for p in failed["price_spike_up"])
    assert any("09:20:00" in p for p in failed["early"])
    assert any("PARAMS" in p for p in failed["bad"])
    assert "cannot build" in failed["boom"][0]
    assert "NAME" in failed["opening_drive"][0]                  # registered under another name
    # a tightened entry window that the strategy's WINDOW never overlaps refuses it; overlapping ones still load
    late = dataclasses.replace(DEFAULTS, ENTRY_START=time(9, 30))
    runners, failed = strategies.load(late, exit_profile_problems)
    assert set(runners) == {"price_spike_up", "price_spike_down"} and "never overlaps" in failed["opening_drive"][0]


def test_version_hash_inputs(monkeypatch):
    v = version(PriceSpikeUp, DEFAULTS)
    assert re.fullmatch(r"1\.[0-9a-f]{10}", v) and v == version(PriceSpikeUp, DEFAULTS)
    assert v != version(OpeningDrive, DEFAULTS)
    assert v != version(PriceSpikeDown, DEFAULTS)            # the same PARAMS: the name and code still differ
    inputs = version_inputs(PriceSpikeUp, DEFAULTS)
    assert set(inputs) == {"name", "params", "exit_profile", "settings", "engine", "paper_model", "semantic_constants",
                           "code", "strategy_api"}
    assert inputs["settings"] == DEFAULTS.versioned_view() and inputs["semantic_constants"] == st.SEMANTIC_HASH
    # the strategy module's own code (with base.py's): the hash REGISTRY.lock pins for it
    assert inputs["code"] == base.module_ast_hash(PriceSpikeUp) == strategies.current_lock(DEFAULTS)[
        "price_spike_up"]["ast_hash"]
    assert inputs["strategy_api"] == base.strategy_api_hash() == ast_hash(Path(base.__file__).read_text("utf-8"))
    assert (inputs["engine"], inputs["paper_model"]) == (st.ENGINE_SEMANTICS_VERSION, st.PAPER_MODEL_VERSION)
    assert base.canonical_json(inputs) == st.canonical_json(inputs)
    assert v == "1." + hashlib.sha256(st.canonical_json(inputs).encode("utf-8")).hexdigest()[:10]
    json.loads(base.canonical_json(inputs))                          # plain JSON (g1_trials.version_inputs)
    # every versioned input changes the version; an unversioned setting does not
    assert version(PriceSpikeUp, dataclasses.replace(DEFAULTS, LIQ_MAX_SPREAD_PCT=1.0)) != v
    assert version(PriceSpikeUp, dataclasses.replace(DEFAULTS, PAPER_PARTICIPATION=0.4)) != v
    assert version(PriceSpikeUp, dataclasses.replace(DEFAULTS, RECON_INTERVAL_S=5.0)) == v
    # an edit of the strategy module's code alone (REGISTRY.lock refreshed by hand, LOGIC_VERSION and base.py
    #   unchanged) changes the version too: a new G1 trial (review of the B1 batch: it kept the old version string)
    own = base.ast_hash
    src = (STRATEGY_DIR / "price_spike_up.py").read_text(encoding="utf-8")
    edited = ast_hash(src.replace('"retrace_max_pct": 65', '"retrace_max_pct": 66'))
    monkeypatch.setattr(base, "ast_hash", lambda text: edited if text == src else own(text))
    assert base.module_ast_hash(PriceSpikeUp) != inputs["code"] and base.strategy_api_hash() == inputs["strategy_api"]
    assert version(PriceSpikeUp, DEFAULTS) != v and version(PriceSpikeUp, DEFAULTS)[:2] == "1."
    monkeypatch.undo()
    assert version(PriceSpikeUp, DEFAULTS) == v
    # below, every subclass is hashed with PriceSpikeUp's code (theirs is this test file), so only the input under
    #   test differs
    code = base.module_ast_hash(PriceSpikeUp)
    monkeypatch.setattr(base, "module_ast_hash", lambda cls: code)

    class Tuned(PriceSpikeUp):
        PARAMS = {**PriceSpikeUp.PARAMS, "retrace_max_pct": 60}

    class OtherExit(PriceSpikeUp):
        EXIT_PROFILE = ExitProfile(tp_pct=20.0)

    class Bumped(PriceSpikeUp):
        LOGIC_VERSION = 2

    assert len({v, version(Tuned, DEFAULTS), version(OtherExit, DEFAULTS)}) == 3
    assert version(Bumped, DEFAULTS).startswith("2.") and version(Bumped, DEFAULTS)[2:] == v[2:]

    class FakeSettings:
        def __init__(self, **over):
            self.basis = {**DEFAULTS.version_basis(), **over}

        def version_basis(self):
            return self.basis

    assert version(PriceSpikeUp, FakeSettings()) == v
    # the next version of each (paper model 2 and engine semantics 2 made a literal 2 today's value, the same version)
    assert version(PriceSpikeUp, FakeSettings(engine=st.ENGINE_SEMANTICS_VERSION + 1)) != v
    assert version(PriceSpikeUp, FakeSettings(paper_model=st.PAPER_MODEL_VERSION + 1)) != v
    assert version(PriceSpikeUp, FakeSettings(semantic_constants="0" * 64)) != v


# ---------------------------------------------------------------------------------------------------------------
# REGISTRY.lock
# ---------------------------------------------------------------------------------------------------------------
def test_registry_lock_bump(tmp_path):
    # the committed lock is current for this code
    assert strategies.lock_status(DEFAULTS) == ([], [])
    pinned = json.loads(strategies.LOCK_PATH.read_text(encoding="utf-8"))
    assert pinned == strategies.current_lock(DEFAULTS)
    assert pinned["_engine"] == {"engine_semantics_version": st.ENGINE_SEMANTICS_VERSION,
                                 "semantic_constants_hash": st.SEMANTIC_HASH}

    # the pinned hash is the strategy module's AST hash combined with base.py's (§3G: base.py is in every entry)
    src = (STRATEGY_DIR / "price_spike_up.py").read_text(encoding="utf-8")
    h = ast_hash(src)
    assert pinned["price_spike_up"]["ast_hash"] == base.module_ast_hash(PriceSpikeUp) == hashlib.sha256(
        f"{h}|{base.strategy_api_hash()}".encode("utf-8")).hexdigest()
    base_src = (STRATEGY_DIR / "base.py").read_text(encoding="utf-8")
    assert ast_hash(base_src) == base.strategy_api_hash()
    edited_doc = base_src.replace('"""Strategy plug-in API', '"""Edited. Strategy plug-in API', 1)
    assert edited_doc != base_src and ast_hash(edited_doc) == ast_hash(base_src)        # a docstring edit: same
    assert ast_hash(base_src + "\n# a comment\n") == ast_hash(base_src)
    assert ast_hash(base_src.replace("MAX_TTL_S = 10.0", "MAX_TTL_S = 9.0", 1)) != ast_hash(base_src)
    # the AST hash ignores docstrings, comments and layout, but not code or literals
    assert ast_hash(src.replace('"""price_spike_up v1', '"""Edited. price_spike_up v1', 1)) == h
    assert ast_hash("# a comment\n\n" + src.replace("        return ()\n", "        return  ( )\n")) == h
    tuned = src.replace('"retrace_max_pct": 65', '"retrace_max_pct": 66')
    assert tuned != src and ast_hash(tuned) != h
    flipped = src.replace("if m_b > m:", "if abs(m_b) > abs(m):")
    assert flipped != src and ast_hash(flipped) != h

    # bump rules
    changed = copy.deepcopy(pinned)
    changed["price_spike_up"]["ast_hash"] = ast_hash(flipped)
    violations, stale = lock_problems(pinned, changed)
    assert len(violations) == 1 and "price_spike_up.LOGIC_VERSION" in violations[0] and stale == []
    changed["price_spike_up"]["logic_version"] = 2
    assert lock_problems(pinned, changed) == ([], ["price_spike_up: logic_version 1 -> 2"])
    changed["price_spike_up"]["logic_version"] = 0
    assert "went down" in lock_problems(pinned, changed)[0][0]
    eng = copy.deepcopy(pinned)
    eng["_engine"]["semantic_constants_hash"] = "0" * 64
    violations, _ = lock_problems(pinned, eng)
    assert len(violations) == 1 and "ENGINE_SEMANTICS_VERSION" in violations[0]
    eng["_engine"]["engine_semantics_version"] += 1
    assert lock_problems(pinned, eng)[0] == []
    added = {**pinned, "new_strategy": {"logic_version": 1, "ast_hash": "a" * 64}}
    assert lock_problems(pinned, added) == ([], ["new_strategy: not pinned in REGISTRY.lock yet"])
    assert lock_problems(added, pinned) == ([], ["new_strategy: pinned in REGISTRY.lock but no longer registered"])
    malformed = {**pinned, "opening_drive": {"logic_version": "1"}}
    assert "malformed" in lock_problems(malformed, pinned)[0][0]

    # the update helper refuses while a bump is missing and leaves the file alone
    lock = tmp_path / "REGISTRY.lock"
    stale_code = copy.deepcopy(pinned)
    stale_code["price_spike_up"]["ast_hash"] = "f" * 64      # as if the code changed after pinning v1
    base.write_lock(lock, stale_code)
    before = lock.read_bytes()
    with pytest.raises(LockError) as err:
        strategies.update_lock(DEFAULTS, lock)
    assert "LOGIC_VERSION" in str(err.value) and lock.read_bytes() == before
    assert strategies.main(DEFAULTS, ["--update-lock"], lock) == 2 and lock.read_bytes() == before
    assert strategies.main(DEFAULTS, ["--check"], lock) == 2
    # ... and refreshes once the pinned entry is older than the code's version
    stale_code["price_spike_up"]["logic_version"] = 0
    base.write_lock(lock, stale_code)
    assert strategies.main(DEFAULTS, ["--update-lock"], lock) == 0
    assert json.loads(lock.read_text(encoding="utf-8")) == pinned
    assert strategies.main(DEFAULTS, [], lock) == 0 and strategies.update_lock(DEFAULTS, lock) == []
    # a missing lock is created; a corrupt one is refused; unknown arguments are refused
    lock.unlink()
    assert strategies.main(DEFAULTS, ["--update-lock"], lock) == 0 and lock.exists()
    lock.write_text("{not json", encoding="utf-8")
    assert strategies.main(DEFAULTS, ["--update-lock"], lock) == 2
    assert strategies.main(DEFAULTS, ["--force"], lock) == 2


def test_base_py_is_part_of_every_strategy(tmp_path, monkeypatch):
    """§3G: strategies/base.py (the runner, make_intent, intent_problem, the hook defaults) is inside every strategy's
    version and REGISTRY.lock entry. An edited base.py (here a stand-in digest) changes every registered strategy's
    version (a new G1 trial), lock_status reports a LOGIC_VERSION violation for each one, and update_lock refuses and
    leaves the file untouched. Before, a base.py edit was invisible to both."""
    lock = tmp_path / "REGISTRY.lock"
    base.write_lock(lock, strategies.current_lock(DEFAULTS))
    before_versions = {n: version(c, DEFAULTS) for n, c in REGISTRY.items()}
    before = lock.read_bytes()
    assert strategies.lock_status(DEFAULTS, lock) == ([], [])
    monkeypatch.setattr(base, "strategy_api_hash", lambda: "e" * 64)          # as if base.py's code changed
    after_versions = {n: version(c, DEFAULTS) for n, c in REGISTRY.items()}
    assert all(after_versions[n] != v and after_versions[n][:2] == v[:2] for n, v in before_versions.items())
    violations, _stale = strategies.lock_status(DEFAULTS, lock)
    assert sorted(v.split(":", 1)[0] for v in violations) == sorted(REGISTRY)
    assert all("LOGIC_VERSION" in v for v in violations)
    with pytest.raises(LockError):
        strategies.update_lock(DEFAULTS, lock)
    assert lock.read_bytes() == before


# ---------------------------------------------------------------------------------------------------------------
# opening_drive (§10.3)
# ---------------------------------------------------------------------------------------------------------------
def test_opening_drive_boundaries():
    bars = {
        "NSE:AAA": obar("NSE:AAA", 100.0, 103.0, 99.5, 102.5, 3000),     # ret exactly +2.5%, volume exactly 3x
        "NSE:BBB": obar("NSE:BBB", 100.0, 103.0, 99.5, 102.45, 9000),    # +2.45%: no signal
        "NSE:CCC": obar("NSE:CCC", 200.0, 200.5, 194.0, 194.8, 3000),    # -2.6%: DOWN
        "NSE:DDD": obar("NSE:DDD", 100.0, 104.5, 100.0, 104.0, 5000),    # +4%
        "NSE:EEE": obar("NSE:EEE", 100.0, 100.0, 96.5, 97.0, 3000),      # -3%
        "NSE:FFF": obar("NSE:FFF", 100.0, 103.0, 100.0, 103.0, 2999),    # 1 share short of 3x volume
        "NSE:GGG": obar("NSE:GGG", 100.0, 101.0, 97.5, 97.55, 3000),     # -2.45%: no signal
    }
    mv = od_market(bars, mono=7777.0)
    od = OpeningDrive("1.od")
    assert od.on_timer(at("09:19:59.750"), mv) == ()
    now = at("09:20:00")
    out = od.on_timer(now, mv)
    assert [i.underlying for i in out] == ["NSE:DDD", "NSE:EEE", "NSE:CCC", "NSE:AAA"]     # |ret| descending
    assert [i.direction for i in out] == [Direction.UP, Direction.DOWN, Direction.DOWN, Direction.UP]
    for i in out:
        assert i.strategy == "opening_drive" and i.strategy_version == "1.od"
        assert i.strike_pref is StrikePref.ITM1 and i.ttl_s == 10.0 and i.trigger_px == bars[i.underlying].close
        assert i.structure is (StructureType.LONG_CE if i.direction is Direction.UP else StructureType.LONG_PE)
        assert (i.created_wall, i.created_mono) == (now, 7777.0) and intent_problem(i) is None
    by = {i.underlying: i for i in out}
    assert (by["NSE:AAA"].recheck.lo, by["NSE:AAA"].recheck.hi) == pytest.approx((102.5 * 0.995, 102.5 * 1.010))
    assert (by["NSE:CCC"].recheck.lo, by["NSE:CCC"].recheck.hi) == pytest.approx((194.8 * 0.990, 194.8 * 1.005))
    assert by["NSE:AAA"].reason["vol_mult"] == pytest.approx(3.0)
    assert od.skips == {"NSE:BBB": "SKIP:RET", "NSE:FFF": "SKIP:VOLUME", "NSE:GGG": "SKIP:RET"}
    # one batch per day, at the first timer in the window
    assert od.on_timer(at("09:20:00.250"), mv) == () and od.on_timer(at("09:20:05"), mv) == ()
    # a first timer at or after 09:20:10 is too late: the batch is skipped, never caught up
    late = OpeningDrive("1.od")
    assert late.on_timer(at("09:20:10"), mv) == () and late.skips == {"*": "SKIP:WINDOW_MISSED"}
    assert late.on_timer(at("09:20:11"), mv) == ()
    # the feed being down at the batch skips it
    down = OpeningDrive("1.od")
    assert down.on_timer(at("09:20:00"), od_market(bars, feed_ok=False)) == () and down.skips == {"*": "SKIP:FEED"}
    # a new day gets a new batch
    nxt = {u: obar(u, b.open, b.high, b.low, b.close, b.volume, day=NEXT_DAY) for u, b in bars.items()}
    assert len(od.on_timer(at("09:20:00", NEXT_DAY), od_market(nxt))) == 4


def test_opening_drive_retrace_history():
    def batch(bar_by_sym, vols=None, prev=None):
        mv = od_market(bar_by_sym, vols)
        if prev is not None:
            mv._prev = prev
        od = OpeningDrive("1.od")
        return {i.underlying: i.direction for i in od.on_timer(at("09:20:00"), mv)}, od.skips

    # VSURGE retrace: (high - close)/(high - open) <= 0.65 for UP; (close - low)/(open - low) <= 0.65 for DOWN
    got, skips = batch({
        "NSE:UP65": obar("NSE:UP65", 100.0, 110.0, 100.0, 103.5, 3000),     # 6.5/10 = 0.65: passes
        "NSE:UP66": obar("NSE:UP66", 100.0, 110.0, 100.0, 103.4, 3000),     # 6.6/10 = 0.66
        "NSE:DN65": obar("NSE:DN65", 100.0, 100.0, 90.0, 96.5, 3000),       # 6.5/10 = 0.65: passes
        "NSE:DN66": obar("NSE:DN66", 100.0, 100.0, 90.0, 96.6, 3000),
    })
    assert got == {"NSE:UP65": Direction.UP, "NSE:DN65": Direction.DOWN}
    assert skips == {"NSE:UP66": "SKIP:RETRACE", "NSE:DN66": "SKIP:RETRACE"}
    ok = obar("NSE:X", 100.0, 103.0, 100.0, 103.0, 3000)
    # fewer than 20 previous-session bars: no signal
    assert batch({"NSE:X": ok}, [1000] * 19) == ({}, {"NSE:X": "SKIP:NO_HISTORY"})
    assert batch({"NSE:X": ok}, [1000] * 20)[0] == {"NSE:X": Direction.UP}
    # only the previous session's LAST 20 bars count (a host that returns more is trimmed)
    assert batch({"NSE:X": ok}, [100_000] * 5 + [1000] * 20)[0] == {"NSE:X": Direction.UP}
    assert batch({"NSE:X": ok}, [1000] * 20 + [100_000] * 5)[1] == {"NSE:X": "SKIP:VOLUME"}
    # unusable history fails closed
    assert batch({"NSE:X": ok}, prev={"NSE:X": hist("NSE:X", [1000] * 20, valid=False)})[1] == {
        "NSE:X": "SKIP:BAD_HISTORY"}
    assert batch({"NSE:X": ok}, prev={"NSE:X": hist("NSE:X", [1000] * 20, day=DAY)})[1] == {
        "NSE:X": "SKIP:BAD_HISTORY"}                                          # not from a previous session
    assert batch({"NSE:X": ok}, [0] * 20)[1] == {"NSE:X": "SKIP:BAD_HISTORY"}
    # the bar itself: invalid (feed gap / no ticks), missing, wrong slot, or inconsistent
    assert batch({"NSE:X": obar("NSE:X", 100.0, 103.0, 100.0, 103.0, 3000, valid=False)})[1] == {
        "NSE:X": "SKIP:INVALID_BAR"}
    assert batch({"NSE:X": obar("NSE:X", 100.0, 103.0, 100.0, 103.0, 3000, start="09:20:00")})[1] == {
        "NSE:X": "SKIP:NO_BAR"}
    assert batch({"NSE:X": obar("NSE:X", 100.0, 103.0, 100.0, 103.0, 3000, source="HIST")})[1] == {
        "NSE:X": "SKIP:NO_BAR"}
    assert batch({"NSE:X": obar("NSE:X", 100.0, 102.0, 100.0, 103.0, 3000)})[1] == {"NSE:X": "SKIP:BAD_BAR"}
    mv = FakeMV(universe=["NSE:X"], prev={"NSE:X": hist("NSE:X", [1000] * 20)})
    od = OpeningDrive("1.od")
    assert od.on_timer(at("09:20:00"), mv) == [] and od.skips == {"NSE:X": "SKIP:NO_BAR"}


# ---------------------------------------------------------------------------------------------------------------
# price_spike_up (§10.4)
# ---------------------------------------------------------------------------------------------------------------
def test_spike_signed_sustain():
    # a move that holds: +0.7% (>= 0.6 after 09:45), held +0.6% at t0 + 12 s
    tp = Tape()
    tp.spike(100.7)
    assert tp.pending() is not None
    tp.run(range(6, 17), 100.65)
    assert tp.out == []                                    # 11 s is not 12 s
    (i,) = tp.tick(17, 100.6)
    assert (i.direction, i.structure, i.strike_pref) == (Direction.UP, StructureType.LONG_CE, StrikePref.ATM)
    assert i.trigger_px == 100.6 and i.ttl_s == 10.0 and i.underlying == "NSE:RELIANCE"
    assert (i.recheck.lo, i.recheck.hi) == pytest.approx((100.0 * 1.0035, 100.6 * 1.005))
    assert (i.created_mono, i.created_wall) == (1017.0, at("10:00:17"))
    assert {k: i.reason[k] for k in ("m", "held", "peak")} == pytest.approx({"m": 0.7, "held": 0.6, "peak": 0.7})
    assert i.reason["retrace"] == pytest.approx(14.29, abs=0.01) and i.reason["vol_ratio"] == pytest.approx(1.0)
    assert tp.pending() is None

    # brief §4.1 #9 (k09): +0.7%, then -0.4% THROUGH the baseline at t0 + 12 s. v1's abs() saw "0.4% held" and
    # bought a CE into a falling stock; the signed sustain fails it.
    tp = Tape()
    tp.spike(100.7)
    tp.run(range(6, 17), 100.3)
    assert tp.tick(17, 99.6) == [] and tp.pending() is None
    tp.run(range(18, 60), 99.6)
    assert tp.out == []

    # held positive but below 0.35%: fails
    tp = Tape()
    tp.spike(100.7)
    tp.run(range(6, 18), 100.2)
    assert tp.out == []

    # a DOWN tape never yields anything (never DOWN, never flipped); a down move is not even a trigger
    tp = Tape()
    tp.run(range(5), 100.0)
    tp.tick(5, 98.5)                                       # -1.5%
    assert tp.pending() is None
    tp.run(range(6, 60), 98.5)
    tp.run(range(60, 200), 97.0)
    assert tp.out == [] and tp.pending() is None
    # so a dip cannot start a cooldown that blocks the real UP spike that follows it
    tp = Tape()
    tp.run(range(5), 100.0)
    tp.run(range(5, 36), 99.0)                             # -1.0% dip; B = (99.0, t=30)
    tp.tick(36, 99.7)                                      # +0.71% vs B
    tp.run(range(37, 49), 99.7)
    assert [i.direction for i in tp.out] == [Direction.UP]

    # retrace from the peak: 65% passes, 70% fails
    for final, expect in ((100.7, 1), (100.6, 0)):
        tp = Tape()
        tp.spike(100.7)
        tp.run(range(6, 11), 102.0)                       # peak +2.0%
        tp.run(range(11, 17), 101.5)
        assert len(tp.tick(17, final)) == expect           # retrace (1 - 0.7/2.0) = 65% | (1 - 0.6/2.0) = 70%


def test_spike_thresholds_cooldown():
    # theta is 1.0% before 09:45 and 0.6% from 09:45:00 (judged at the detection tick)
    tp = Tape(start="09:44:50")
    tp.run(range(5), 100.0)
    tp.tick(5, 100.95)                                     # 09:44:55, +0.95% < 1.0
    assert tp.pending() is None
    tp.run(range(6, 10), 100.95)
    tp.tick(10, 100.95)                                    # 09:45:00, +0.95% >= 0.6
    assert tp.pending() is not None
    # before 09:55 the sustain needs held >= 1.0%: +0.95% fails at 09:45:12
    tp.run(range(11, 23), 100.95)
    assert tp.out == [] and tp.pending() is None

    # held exactly at the bound passes: 1.0% before 09:55, 0.35% after
    for start, move, final, expect in (("09:50:00", 101.2, 101.0, 1), ("09:50:00", 101.2, 100.99, 0),
                                       ("10:00:00", 100.7, 100.35, 1), ("10:00:00", 100.7, 100.34, 0)):
        tp = Tape(start=start)
        tp.spike(move)
        tp.run(range(6, 17), move)
        assert len(tp.tick(17, final)) == expect, (start, final)

    # pass or fail, a 90 s cooldown starts at the sustain decision
    for final in (100.7, 99.0):                            # a pass, then a fail
        tp = Tape()
        tp.spike(100.7)
        tp.run(range(6, 18), final)                        # decision at t = 17
        tp.run(range(18, 106), 100.7)
        tp.tick(106, 102.0)                                # t = 17 + 89: a +1.3% move, still cooling down
        assert tp.pending() is None
        tp.tick(107, 102.0)                                # t = 17 + 90: detection again
        assert tp.pending() is not None

    # the 09:20-14:55 window gates detection, and nothing is queued outside it
    tp = Tape(start="09:19:30")
    tp.run(range(29), 100.0)
    tp.tick(29, 101.5)                                     # 09:19:59, +1.5%
    assert tp.pending() is None
    tp.tick(30, 101.5)                                     # 09:20:00
    assert tp.pending() is not None
    tp = Tape(start="14:54:30")
    tp.run(range(29), 100.0)
    tp.tick(29, 100.7)                                     # 14:54:59: detected inside the window ...
    assert tp.pending() is not None
    tp.run(range(30, 42), 100.7)                           # ... but its sustain passes at 14:55:11: not emitted
    assert tp.out == [] and tp.pending() is None
    tp = Tape(start="14:54:40")
    tp.run(range(20), 100.0)
    tp.tick(20, 101.0)                                     # 14:55:00 is outside [09:20, 14:55)
    assert tp.pending() is None

    # volume confirmation: with >= 3 deltas, the trigger tick's delta must be >= 0.5 x their average
    for vol, expect_pending in ((40, False), (50, True)):
        tp = Tape()
        tp.run(range(5), 100.0)                            # deltas 100 x 4 (the first tick has no delta)
        tp.tick(5, 100.7, vol=vol)                         # avg (400 + vol)/5: 40 < 44, 50 >= 45
        assert (tp.pending() is not None) is expect_pending
    tp = Tape()
    tp.tick(0, 100.0)
    tp.tick(1, 100.0)
    tp.tick(2, 100.7, vol=1)                               # only 2 deltas: volume cannot veto
    assert tp.pending() is not None

    # ticks before 09:15 are ignored, and ineligible symbols keep no state
    tp = Tape(start="09:14:00")
    tp.run(range(30), 100.0)
    assert tp.s._syms == {}
    tp = Tape(mv=FakeMV(["NSE:OTHER"]))
    tp.spike(100.7)
    assert tp.s._syms == {}


def test_spike_baseline_b():
    # B starts 30 s after A; after A's 60 s reset it still measures a move that began before the reset
    tp = Tape()
    tp.run(range(30), 100.0)                               # A = (100.0, t=0)
    tp.tick(30, 100.0)                                     # A is 30 s old: B = (100.0, t=30)
    tp.run(range(31, 62), 100.4)                           # +0.4% < 0.6; at t=61 A (61 s old) resets to 100.4
    assert tp.s._syms[tp.sym].a == (100.4, 1061.0) and tp.pending() is None
    tp.tick(62, 100.7)                                     # +0.30% vs A, +0.70% vs B
    assert tp.pending() is not None and tp.pending().baseline == 100.0
    # signed: the larger UP move wins even when A shows a larger DOWN move (v1 compared absolute moves and
    # would have called this a DOWN spike)
    tp = Tape()
    tp.run(range(30), 101.0)                               # A = 101.0
    tp.run(range(30, 40), 99.5)                            # B = (99.5, t=30); -1.49% vs A is no trigger
    assert tp.pending() is None
    tp.tick(40, 100.2)                                     # -0.79% vs A, +0.70% vs B
    assert tp.pending() is not None and tp.pending().baseline == 99.5
    assert tp.pending().m == pytest.approx((100.2 - 99.5) / 99.5 * 100)
    # B counts only once it is at least 5 s old
    tp = Tape()
    tp.run(range(62), 101.0)                               # A = (101.0, t=0), reset to (101.0, t=61); B at t=30
    assert tp.s._syms[tp.sym].a == (101.0, 1061.0)
    tp.run(range(62, 91), 100.0)                           # -0.99%: nothing
    tp.tick(91, 100.0)                                     # B is 61 s old: resets to (100.0, t=91)
    assert tp.s._syms[tp.sym].b == (100.0, 1091.0)
    tp.run(range(93, 96), 100.7)                           # -0.30% vs A; +0.70% vs a B only 2-4 s old: ignored
    assert tp.pending() is None
    tp.tick(96, 100.7)                                     # B is 5 s old: +0.70% counts
    assert tp.pending() is not None and tp.pending().baseline == 100.0


def test_feed_reset():
    # price_spike_up: a reconnect drops the pending sustain and the baselines ...
    tp = Tape()
    tp.spike(100.7)
    assert tp.pending() is not None
    runner = StrategyRunner(tp.s)
    assert runner.on_feed_reset() == base.RunResult()
    assert tp.pending() is None
    state = tp.s._syms[tp.sym]
    assert state.a is None and state.b is None
    tp.run(range(6, 40), 100.7)                            # A re-anchors at 100.7: no move, no sustain
    assert tp.out == [] and tp.pending() is None
    # ... and the cumulative volume, so the gap's volume is not one giant delta
    n = len(state.deltas)
    tp.s.on_feed_reset()
    tp.tick(40, 100.7, vol=1_000_000)
    assert len(state.deltas) == n
    tp.tick(41, 100.7)
    assert len(state.deltas) == n + 1 and state.deltas[-1] == 100

    # opening_drive: a reconnect while the opening bar is built skips that day's batch
    bars = {"NSE:AAA": obar("NSE:AAA", 100.0, 103.0, 99.5, 103.0, 3000)}
    od = OpeningDrive("1.od")
    od.on_timer(at("09:16:59.750"), od_market(bars))
    od.on_feed_reset()
    assert od.on_timer(at("09:20:00"), od_market(bars)) == () and od.skips == {"*": "SKIP:FEED"}
    # ... but not the next day's
    nxt = {"NSE:AAA": obar("NSE:AAA", 100.0, 103.0, 99.5, 103.0, 3000, day=NEXT_DAY)}
    assert len(od.on_timer(at("09:20:00", NEXT_DAY), od_market(nxt))) == 1
    # a reconnect before 09:15 does not touch the opening bar
    early = OpeningDrive("1.od")
    early.on_timer(at("09:10:00"), od_market(bars))
    early.on_feed_reset()
    assert len(early.on_timer(at("09:20:00"), od_market(bars))) == 1
    # a reconnect before any timer was seen (the host delivers resets even before StartSignals) cannot be placed
    #   inside the bar: the bar's own validity decides (F-17; this assertion used to pin a skipped day) ...
    blind = OpeningDrive("1.od")
    blind.on_feed_reset()
    assert len(blind.on_timer(at("09:20:00"), od_market(bars))) == 1
    # ... and a reconnect that did fall inside the bar left the bar FEED_GAP-invalid (market/bars.py)
    gap = OpeningDrive("1.od")
    gap.on_feed_reset()
    invalid = {"NSE:AAA": obar("NSE:AAA", 100.0, 103.0, 99.5, 103.0, 3000, valid=False)}
    assert not gap.on_timer(at("09:20:00"), od_market(invalid)) and gap.skips == {"NSE:AAA": "SKIP:INVALID_BAR"}


def test_opening_drive_premarket_reconnect_keeps_the_day():
    """F-17: SignalsHost delivers on_feed_reset() on any WS reconnect, also before StartSignals (no timer yet). A
    reconnect at ~08:55 used to skip the whole session (SKIP:FEED) although market/bars.py keeps the 09:15 bar valid;
    now a valid +3 % / 9x-volume opening bar with 20 previous-session bars still emits its intent."""
    bars = {"NSE:RELIANCE": obar("NSE:RELIANCE", 100.0, 103.2, 100.0, 103.0, 9000)}
    s = OpeningDrive("1.test")
    s.on_feed_reset()                                       # a WS reconnect before any timer
    out = s.on_timer(at("09:20:00.100"), od_market(bars))
    assert [i.underlying for i in out] == ["NSE:RELIANCE"] and s.skips == {}, s.skips
    control = OpeningDrive("1.test")                        # the same bar without the reset
    assert [i.underlying for i in control.on_timer(at("09:20:00.100"), od_market(bars))] == ["NSE:RELIANCE"]


# ---------------------------------------------------------------------------------------------------------------
# price_spike_down: the signed mirror of price_spike_up (a PAPER experiment, 2026-10-12, user request against E1)
# ---------------------------------------------------------------------------------------------------------------
class DownTape(Tape):
    """A Tape that drives a PriceSpikeDown; spike() falls to move_px (default -0.7%)."""

    def __init__(self, start="10:00:00", sym="NSE:RELIANCE", strat=None, mv=None, step_vol=100):
        super().__init__(start, sym, strat or PriceSpikeDown("1.test"), mv, step_vol)

    def spike(self, move_px=99.3, at_sec=5):
        super().spike(move_px, at_sec)


# The REGISTRY.lock entries of the two strategies whose G1 trials were collecting when price_spike_down was
# registered (2026-10-12). Registering a strategy must never touch them: a changed entry is a new version, and so a
# new trial. A later, deliberate LOGIC_VERSION bump of either strategy (or a base.py edit) updates this pin too.
TRIALS_RUNNING_2026_10_12 = {
    "opening_drive": {"ast_hash": "802605155cf2924ac859ea1dc67a3c87c285a54f4fcba06913171b3e089d0edd",
                      "logic_version": 1},
    "price_spike_up": {"ast_hash": "fb09fbb8185aba5fc3596e65793c6fc2f9baf7d762c6d70fb10ae07675ca95fd",
                       "logic_version": 1},
}

# price_spike_up v1's code -> price_spike_down v1's code: every hunk of the mirror (docstrings and comments aside)
MIRROR = (
    ('class PriceSpikeUp(Strategy):\n    NAME = "price_spike_up"',
     'def _down_pct(baseline: float, ltp: float) -> float:\n    return (baseline - ltp) / baseline * 100.0\n\n\n'
     'class PriceSpikeDown(Strategy):\n    NAME = "price_spike_down"'),
    ("m = (ltp - base) / base * 100.0", "m = _down_pct(base, ltp)"),
    ("m_b = (ltp - st.b[0]) / st.b[0] * 100.0", "m_b = _down_pct(st.b[0], ltp)"),
    ("held = (t.ltp - pend.baseline) / pend.baseline * 100.0", "held = _down_pct(pend.baseline, t.ltp)"),
    ("direction=Direction.UP", "direction=Direction.DOWN"),
    ('recheck=Recheck(pend.baseline * (1 + p["hold_pct"] / 100.0), t.ltp * (1 + p["recheck_chase_pct"] / 100.0))',
     'recheck=Recheck(t.ltp * (1 - p["recheck_chase_pct"] / 100.0), pend.baseline * (1 - p["hold_pct"] / 100.0))'),
)


def test_price_spike_down_registration_leaves_the_running_trials_alone():
    """price_spike_down is registered like the others (the engine gives every registered strategy a PAPER
    strategy_modes row), with its own version and REGISTRY.lock entry, and loaded last. Registering it changed
    nothing of opening_drive or price_spike_up: their lock entries are the ones pinned when it was added, and their
    lock entries and versions are the same with or without it registered, so their running G1 trials go on."""
    assert list(REGISTRY) == ["opening_drive", "price_spike_up", "price_spike_down"]
    assert REGISTRY["price_spike_down"] is PriceSpikeDown is strategies.PriceSpikeDown
    pinned = json.loads(strategies.LOCK_PATH.read_text(encoding="utf-8"))
    current = strategies.current_lock(DEFAULTS)
    assert set(pinned) == {"_engine", "opening_drive", "price_spike_up", "price_spike_down"}
    for name, entry in TRIALS_RUNNING_2026_10_12.items():
        assert pinned[name] == current[name] == entry, (
            f"{name}'s REGISTRY.lock entry changed: that is a new version and a new G1 trial")
    assert pinned["price_spike_down"] == current["price_spike_down"] == {
        "logic_version": 1, "ast_hash": base.module_ast_hash(PriceSpikeDown)}
    # the others' lock entries and versions do not depend on price_spike_down being registered
    without = {n: c for n, c in REGISTRY.items() if n != "price_spike_down"}
    basis = DEFAULTS.version_basis()
    assert base.lock_snapshot(without, basis["engine"], basis["semantic_constants"]) == {
        n: e for n, e in current.items() if n != "price_spike_down"}
    alone, failed = strategies.load(DEFAULTS, exit_profile_problems, registry=without)
    full, failed_full = strategies.load(DEFAULTS, exit_profile_problems)
    assert failed == failed_full == {}
    assert {n: r.version for n, r in alone.items()} == {n: full[n].version for n in without}
    assert list(full) == list(REGISTRY)         # the signals host runs the hooks in this order: the newcomer last
    # its own version: price_spike_up's PARAMS, exits, universe and window, yet another version (name and code)
    v = version(PriceSpikeDown, DEFAULTS)
    assert re.fullmatch(r"1\.[0-9a-f]{10}", v) and v not in {version(c, DEFAULTS) for c in without.values()}
    assert full["price_spike_down"].version == v and version_inputs(PriceSpikeDown, DEFAULTS)["code"] == (
        current["price_spike_down"]["ast_hash"])
    assert dict(PriceSpikeDown.PARAMS) == dict(PriceSpikeUp.PARAMS)
    assert (PriceSpikeDown.EXIT_PROFILE, PriceSpikeDown.UNIVERSE, PriceSpikeDown.WINDOW) == (
        PriceSpikeUp.EXIT_PROFILE, PriceSpikeUp.UNIVERSE, PriceSpikeUp.WINDOW)


def test_spike_down_is_the_signed_mirror_of_spike_up():
    """price_spike_down v1 is price_spike_up v1 with exactly the MIRROR hunks (compared as ASTs, so docstrings and
    comments do not count): every move is measured downward, the intent is DOWN and the recheck band is mirrored.
    When price_spike_up changes (its LOGIC_VERSION bump), decide whether price_spike_down follows (its own bump),
    then update MIRROR."""
    up = (STRATEGY_DIR / "price_spike_up.py").read_text(encoding="utf-8")
    down = (STRATEGY_DIR / "price_spike_down.py").read_text(encoding="utf-8")
    mirrored = up
    for old, new in MIRROR:
        assert mirrored.count(old) == 1, old
        mirrored = mirrored.replace(old, new)
    assert ast_hash(mirrored) == ast_hash(down) != ast_hash(up)


def test_spike_down_signed_sustain():
    # a fall that holds: -0.7% (>= 0.6 after 09:45), held -0.6% at t0 + 12 s
    tp = DownTape()
    tp.spike(99.3)
    assert tp.pending() is not None
    tp.run(range(6, 17), 99.35)
    assert tp.out == []                                    # 11 s is not 12 s
    (i,) = tp.tick(17, 99.4)
    assert (i.direction, i.structure, i.strike_pref) == (Direction.DOWN, StructureType.LONG_PE, StrikePref.ATM)
    assert (i.strategy, i.strategy_version, i.is_placebo) == ("price_spike_down", "1.test", False)
    assert i.trigger_px == 99.4 and i.ttl_s == 10.0 and i.underlying == "NSE:RELIANCE" and intent_problem(i) is None
    assert (i.recheck.lo, i.recheck.hi) == pytest.approx((99.4 * 0.995, 100.0 * 0.9965))
    assert i.recheck.contains(99.4) and not i.recheck.contains(99.7)      # 0.3% under the baseline: too little
    assert (i.created_mono, i.created_wall) == (1017.0, at("10:00:17"))
    assert {k: i.reason[k] for k in ("m", "held", "peak")} == pytest.approx({"m": 0.7, "held": 0.6, "peak": 0.7})
    assert i.reason["retrace"] == pytest.approx(14.29, abs=0.01) and i.reason["vol_ratio"] == pytest.approx(1.0)
    assert (i.reason["baseline"], i.reason["detect_px"]) == (100.0, 99.3)
    assert tp.pending() is None

    # the mirror of brief §4.1 #9 (k09): -0.7%, then +0.4% back up THROUGH the baseline at t0 + 12 s. An abs()
    # sustain would see "0.4% held" and buy a PE into a rising stock; the signed sustain fails it.
    tp = DownTape()
    tp.spike(99.3)
    tp.run(range(6, 17), 99.7)
    assert tp.tick(17, 100.4) == [] and tp.pending() is None
    tp.run(range(18, 60), 100.4)
    assert tp.out == []

    # held positive (a fall) but below 0.35%: fails
    tp = DownTape()
    tp.spike(99.3)
    tp.run(range(6, 18), 99.8)
    assert tp.out == []

    # an UP tape never yields anything (never UP, never flipped); an up move is not even a trigger
    tp = DownTape()
    tp.run(range(5), 100.0)
    tp.tick(5, 101.5)                                      # +1.5%
    assert tp.pending() is None
    tp.run(range(6, 60), 101.5)
    tp.run(range(60, 200), 103.0)
    assert tp.out == [] and tp.pending() is None
    # so a rally cannot start a cooldown that blocks the real DOWN spike that follows it
    tp = DownTape()
    tp.run(range(5), 100.0)
    tp.run(range(5, 36), 101.0)                            # +1.0% rally; B = (101.0, t=30)
    tp.tick(36, 100.3)                                     # -0.69% vs B
    tp.run(range(37, 49), 100.3)
    assert [i.direction for i in tp.out] == [Direction.DOWN]

    # retrace from the trough: 65% passes, 70% fails
    for final, expect in ((99.3, 1), (99.4, 0)):
        tp = DownTape()
        tp.spike(99.3)
        tp.run(range(6, 11), 98.0)                         # the trough: -2.0%
        tp.run(range(11, 17), 98.5)
        assert len(tp.tick(17, final)) == expect           # retrace (1 - 0.7/2.0) = 65% | (1 - 0.6/2.0) = 70%


def test_spike_down_thresholds_cooldown():
    # theta is 1.0% before 09:45 and 0.6% from 09:45:00 (judged at the detection tick)
    tp = DownTape(start="09:44:50")
    tp.run(range(5), 100.0)
    tp.tick(5, 99.05)                                      # 09:44:55, -0.95%: 0.95 < 1.0
    assert tp.pending() is None
    tp.run(range(6, 10), 99.05)
    tp.tick(10, 99.05)                                     # 09:45:00, -0.95%: 0.95 >= 0.6
    assert tp.pending() is not None
    # before 09:55 the sustain needs held >= 1.0%: a 0.95% fall fails at 09:45:12
    tp.run(range(11, 23), 99.05)
    assert tp.out == [] and tp.pending() is None

    # held exactly at the bound passes: 1.0% before 09:55, 0.35% after
    for start, move, final, expect in (("09:50:00", 98.8, 99.0, 1), ("09:50:00", 98.8, 99.01, 0),
                                       ("10:00:00", 99.3, 99.65, 1), ("10:00:00", 99.3, 99.66, 0)):
        tp = DownTape(start=start)
        tp.spike(move)
        tp.run(range(6, 17), move)
        assert len(tp.tick(17, final)) == expect, (start, final)

    # pass or fail, a 90 s cooldown starts at the sustain decision
    for final in (99.3, 101.0):                            # a pass, then a fail
        tp = DownTape()
        tp.spike(99.3)
        tp.run(range(6, 18), final)                        # decision at t = 17
        tp.run(range(18, 106), 99.3)
        tp.tick(106, 98.0)                                 # t = 17 + 89: a -1.3% move, still cooling down
        assert tp.pending() is None
        tp.tick(107, 98.0)                                 # t = 17 + 90: detection again
        assert tp.pending() is not None

    # the 09:20-14:55 window gates detection, and nothing is queued outside it
    tp = DownTape(start="09:19:30")
    tp.run(range(29), 100.0)
    tp.tick(29, 98.5)                                      # 09:19:59, -1.5%
    assert tp.pending() is None
    tp.tick(30, 98.5)                                      # 09:20:00
    assert tp.pending() is not None
    tp = DownTape(start="14:54:30")
    tp.run(range(29), 100.0)
    tp.tick(29, 99.3)                                      # 14:54:59: detected inside the window ...
    assert tp.pending() is not None
    tp.run(range(30, 42), 99.3)                            # ... but its sustain passes at 14:55:11: not emitted
    assert tp.out == [] and tp.pending() is None
    tp = DownTape(start="14:54:40")
    tp.run(range(20), 100.0)
    tp.tick(20, 99.0)                                      # 14:55:00 is outside [09:20, 14:55)
    assert tp.pending() is None

    # volume confirmation: with >= 3 deltas, the trigger tick's delta must be >= 0.5 x their average
    for vol, expect_pending in ((40, False), (50, True)):
        tp = DownTape()
        tp.run(range(5), 100.0)                            # deltas 100 x 4 (the first tick has no delta)
        tp.tick(5, 99.3, vol=vol)                          # avg (400 + vol)/5: 40 < 44, 50 >= 45
        assert (tp.pending() is not None) is expect_pending
    tp = DownTape()
    tp.tick(0, 100.0)
    tp.tick(1, 100.0)
    tp.tick(2, 99.3, vol=1)                                # only 2 deltas: volume cannot veto
    assert tp.pending() is not None

    # ticks before 09:15 are ignored, and ineligible symbols keep no state
    tp = DownTape(start="09:14:00")
    tp.run(range(30), 100.0)
    assert tp.s._syms == {}
    tp = DownTape(mv=FakeMV(["NSE:OTHER"]))
    tp.spike(99.3)
    assert tp.s._syms == {}


def test_spike_down_baseline_b():
    # B starts 30 s after A; after A's 60 s reset it still measures a move that began before the reset
    tp = DownTape()
    tp.run(range(30), 100.0)                               # A = (100.0, t=0)
    tp.tick(30, 100.0)                                     # A is 30 s old: B = (100.0, t=30)
    tp.run(range(31, 62), 99.6)                            # -0.4%: 0.4 < 0.6; at t=61 A (61 s old) resets to 99.6
    assert tp.s._syms[tp.sym].a == (99.6, 1061.0) and tp.pending() is None
    tp.tick(62, 99.3)                                      # -0.30% vs A, -0.70% vs B
    assert tp.pending() is not None and tp.pending().baseline == 100.0
    # signed: the larger DOWN move wins even when A shows a larger UP move (v1 compared absolute moves and would
    # have called this an UP spike)
    tp = DownTape()
    tp.run(range(30), 99.0)                                # A = 99.0
    tp.run(range(30, 40), 100.5)                           # B = (100.5, t=30); +1.52% vs A is no trigger
    assert tp.pending() is None
    tp.tick(40, 99.8)                                      # +0.81% vs A, -0.70% vs B
    assert tp.pending() is not None and tp.pending().baseline == 100.5
    assert tp.pending().m == pytest.approx((100.5 - 99.8) / 100.5 * 100)
    # B counts only once it is at least 5 s old
    tp = DownTape()
    tp.run(range(62), 99.0)                                # A = (99.0, t=0), reset to (99.0, t=61); B at t=30
    assert tp.s._syms[tp.sym].a == (99.0, 1061.0)
    tp.run(range(62, 91), 100.0)                           # +1.01%: nothing
    tp.tick(91, 100.0)                                     # B is 61 s old: resets to (100.0, t=91)
    assert tp.s._syms[tp.sym].b == (100.0, 1091.0)
    tp.run(range(93, 96), 99.3)                            # +0.30% vs A; -0.70% vs a B only 2-4 s old: ignored
    assert tp.pending() is None
    tp.tick(96, 99.3)                                      # B is 5 s old: the 0.70% fall counts
    assert tp.pending() is not None and tp.pending().baseline == 100.0


def test_spike_down_feed_reset():
    # a reconnect drops the pending sustain and the baselines ...
    tp = DownTape()
    tp.spike(99.3)
    assert tp.pending() is not None
    runner = StrategyRunner(tp.s)
    assert runner.on_feed_reset() == base.RunResult()
    assert tp.pending() is None
    state = tp.s._syms[tp.sym]
    assert state.a is None and state.b is None
    tp.run(range(6, 40), 99.3)                             # A re-anchors at 99.3: no move, no sustain
    assert tp.out == [] and tp.pending() is None
    # ... and the cumulative volume, so the gap's volume is not one giant delta
    n = len(state.deltas)
    tp.s.on_feed_reset()
    tp.tick(40, 99.3, vol=1_000_000)
    assert len(state.deltas) == n
    tp.tick(41, 99.3)
    assert len(state.deltas) == n + 1 and state.deltas[-1] == 100


def test_spike_down_rate_cap_lives_in_the_runner():
    """The 30-intents-per-minute cap is StrategyRunner's, as for every strategy: 31 stocks falling 0.7% at once give
    30 DOWN intents and one RATE drop."""
    syms = [f"NSE:S{k:02d}" for k in range(31)]
    mv = FakeMV(syms)
    runner = StrategyRunner(PriceSpikeDown("1.test"), st.INTENT_MAX_PER_MIN)
    passed, dropped = [], []
    for sec in range(18):
        for k, sym in enumerate(syms):
            t = UnderlyingTick(sym, k + 1, 100.0 if sec < 5 else 99.3, 10_000 + 100 * sec, None, None, None,
                               1000.0 + sec + k / 1000, at("10:00:00") + timedelta(seconds=sec, milliseconds=k))
            res = runner.on_tick(t, mv)
            assert res.error is None
            passed += res.intents
            dropped += res.dropped
    assert len(passed) == 30 and [why for _, why in dropped] == ["RATE"]
    assert {i.direction for i in passed} == {Direction.DOWN} and runner.max_per_min == 30


def test_spike_down_volume_rule_only_skips_a_quiet_trigger_tick():
    """docs/LAPTOP.md section 8 (review, 2026-10-12): the volume rule needs no rising volume. It only skips a fall
    whose trigger tick traded under half the average of the recent deltas (its own included); a fall on volume that
    has been shrinking tick after tick still triggers."""
    p = PriceSpikeDown.PARAMS
    assert (p["vol_confirm_mult"], p["vol_min_hist"]) == (0.5, 3)
    for vol, expect_pending in ((90, True), (70, False)):    # avg (680 + vol) / 5: 90 >= 77, 70 < 75
        tp = DownTape()
        for sec, v in enumerate((100, 200, 180, 160, 140)):    # the first tick has no delta; then falling volume
            tp.tick(sec, 100.0, vol=v)
        tp.tick(5, 99.3, vol=vol)                             # -0.7% on the smallest delta yet
        assert (tp.pending() is not None) is expect_pending, vol


def test_spike_up_and_spike_down_share_no_cooldown():
    """Review, 2026-10-12 (docs/LAPTOP.md section 8, TITAN_INSTRUCTIONS §11): the two spike strategies share no state.
    Each keeps its own 90 s per-stock cooldown, where v1's watcher had one per stock for PRICE_SPIKE_UP and
    PRICE_SPIKE_DOWN alike (equal priority, so neither bypassed the other's). So one stock gives a CE and, seconds
    later, a PE, and both can fire on one tick, when the price is up against baseline A and down against baseline B.
    A shared cooldown would change price_spike_up's behaviour, and so its running G1 trial: there is none."""
    assert PriceSpikeUp.PARAMS["cooldown_s"] == PriceSpikeDown.PARAMS["cooldown_s"] == 90

    def both(ticks):
        """[(sec, strategy, structure, baseline)] of a PriceSpikeUp and a PriceSpikeDown fed the same ticks."""
        up, down, out = Tape(), DownTape(), []
        for sec, px, vol in ticks:
            out += [(sec, i.strategy, i.structure, i.reason["baseline"]) for tp in (up, down)
                    for i in tp.tick(sec, px, vol)]
        return out, up.out + down.out

    # seconds apart: a +0.8% jump that holds gives the CE at t = 17, and price_spike_up cools down until t = 107; the
    #   drop to 100.1 at t = 40, -0.69% against B (100.8, set at t = 30), holds and gives the PE at t = 52
    ticks = [(k, 100.0, 100) for k in range(5)] + [(k, 100.8, 100) for k in range(5, 40)]
    ticks += [(k, 100.1, 100) for k in range(40, 60)]
    out, _ = both(ticks)
    assert out == [(17, "price_spike_up", StructureType.LONG_CE, 100.0),
                   (52, "price_spike_down", StructureType.LONG_PE, 100.8)]

    # one tick: A = 100.0 at t = 0; a jump to 101.5 on dry volume (no trigger) sets B there at t = 30; at t = 35 the
    #   price is 100.8 on volume, +0.80% against A and -0.69% against B. Both hold for 12 s and fire on one tick, and
    #   both recheck bands pass the same price
    ticks = [(k, 100.0, 100) for k in range(30)] + [(k, 101.5, 1) for k in range(30, 35)]
    ticks += [(35, 100.8, 200)] + [(k, 100.8, 100) for k in range(36, 48)]
    out, intents = both(ticks)
    assert out == [(47, "price_spike_up", StructureType.LONG_CE, 100.0),
                   (47, "price_spike_down", StructureType.LONG_PE, 101.5)]
    assert all(i.trigger_px == 100.8 and i.recheck.contains(100.8) for i in intents)
