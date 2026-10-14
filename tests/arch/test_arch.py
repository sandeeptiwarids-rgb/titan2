"""Architecture tests (spec §16.6; DETAIL §0.5, §1 rules 4-5, §2 import rules, §3.4, §6, §10.2, §12.1).

Static checks over the titan2 package: every rule is an AST scan of the source (nothing is imported except where a
rule is about importing), so a violation is reported with its file and line.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import textwrap
import tokenize
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.arch

ROOT = Path(__file__).resolve().parents[2]          # agentic_trader/
PKG = ROOT / "titan2"
TESTS = ROOT / "tests"

# v1 module names (DETAIL §1 rule 5, §2): every agentic_trader/*.py, plus these even after the v1 files are gone.
V1_MODULES = frozenset({
    "config", "zerodha_tools", "autonomous_trader", "options_trader", "kite_ticker", "exit_manager", "state_db",
    "trade_ledger", "settings_manager", "dashboard", "dashboard_ec2", "kite_token_manager", "pre_market_check",
    "position_reconciliation", "llm_agent", "safe_io", "risk_governor", "execution_guard",
    "idempotent_order_engine", "oi_watcher_engine", "market_scanner", "news_scanner", "smart_money_signals",
    "greeks_engine", "dhan_oi_fetcher", "dhan_token_manager", "dhan_risk_tools", "nse_oi_fetcher",
    "options_flow_analyzer", "quant_analytics", "regime_score", "correlation_guard", "expiry_shield",
    "data_health_gate", "capital_swap_engine", "commodities_trader", "log_watchdog", "refresh_token", "quick_auth",
    "run", "deploy", "ml_models",
})
ORDER_METHODS = frozenset({"place_order", "cancel_order", "modify_order"})
GTT_AND_ORDER_TYPES = frozenset({"place_gtt", "modify_gtt", "delete_gtt", "ORDER_TYPE_MARKET", "ORDER_TYPE_SL",
                                 "ORDER_TYPE_SLM"})
FORBIDDEN_ORDER_STRINGS = frozenset({"MARKET", "SL-M"})
PAPER_INTERNALS = frozenset({"PaperExchange", "PaperSim", "fill_from_depth", "paper_sim", "p_orders", "p_fills"})
ORDER_PATH = ("titan2.core.orders", "titan2.core.positions", "titan2.core.exits", "titan2.core.exit_loop",
              "titan2.core.sizing", "titan2.core.single_leg")
WINDOWS_EXEMPT = ("titan2.runtime", "titan2.market.feed")
STORE_WRITE_PRIMITIVES = frozenset({"_w", "_insert", "_upsert", "_update_diff", "_check_write", "_tx"})


# -- the package as source ----------------------------------------------------------------------------------------
def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@lru_cache(maxsize=None)
def modules() -> dict[str, Path]:
    return {_module_name(p): p for p in sorted(PKG.rglob("*.py")) if "__pycache__" not in p.parts}


@lru_cache(maxsize=None)
def tree(name: str) -> ast.Module:
    path = modules()[name]
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_pkg(name: str) -> bool:
    return modules()[name].name == "__init__.py"


def _where(name: str, node: ast.AST) -> str:
    return f"{modules()[name].relative_to(ROOT).as_posix()}:{getattr(node, 'lineno', '?')}"


def _type_checking_nodes(t: ast.Module) -> set[int]:
    """ids of the nodes inside `if TYPE_CHECKING:` blocks (imports that never run)."""
    out: set[int] = set()
    for node in ast.walk(t):
        if isinstance(node, ast.If):
            test = node.test
            name = test.id if isinstance(test, ast.Name) else test.attr if isinstance(test, ast.Attribute) else None
            if name == "TYPE_CHECKING":
                for child in node.body:
                    out.update(id(n) for n in ast.walk(child))
    return out


def imports(name: str, *, runtime_only: bool = False) -> list[tuple[str, ast.AST]]:
    """Every module `name` imports (dotted, relative imports resolved; `from a import b` yields a and a.b)."""
    t = tree(name)
    skip = _type_checking_nodes(t) if runtime_only else set()
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(t):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            out += [(a.name, node) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = name.split(".") if _is_pkg(name) else name.split(".")[:-1]
                base = base[:len(base) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.append((mod, node))
            out += [(f"{mod}.{a.name}", node) for a in node.names if a.name != "*"]
    return out


def titan2_imports(name: str, *, runtime_only: bool = False) -> set[str]:
    """The titan2 modules `name` imports, each with its parent packages (importing a.b.c runs a and a.b)."""
    known = modules()
    out: set[str] = set()
    for mod, _ in imports(name, runtime_only=runtime_only):
        if not mod.startswith("titan2"):
            continue
        parts = mod.split(".")
        for i in range(1, len(parts) + 1):
            if ".".join(parts[:i]) in known:
                out.add(".".join(parts[:i]))
    return out


def closure(start: str) -> set[str]:
    """Every titan2 module that importing `start` runs (TYPE_CHECKING imports aside)."""
    seen: set[str] = set()
    todo = [start] + [".".join(start.split(".")[:i]) for i in range(1, len(start.split(".")))]
    while todo:
        m = todo.pop()
        if m in seen or m not in modules():
            continue
        seen.add(m)
        todo += sorted(titan2_imports(m, runtime_only=True))
    return seen


def identifiers(t: ast.AST):
    """(identifier, node) for every name the code binds or references."""
    for node in ast.walk(t):
        if isinstance(node, ast.Name):
            yield node.id, node
        elif isinstance(node, ast.Attribute):
            yield node.attr, node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node
        elif isinstance(node, ast.arg):
            yield node.arg, node
        elif isinstance(node, ast.keyword) and node.arg is not None:
            yield node.arg, node
        elif isinstance(node, ast.alias):
            yield node.name.split(".")[-1], node
            if node.asname:
                yield node.asname, node
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for n in node.names:
                yield n, node


def strings(t: ast.AST):
    for node in ast.walk(t):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, node


def _report(problems: list[str]) -> str:
    return "\n  " + "\n  ".join(problems)


def _stdlib() -> frozenset[str]:
    return frozenset(sys.stdlib_module_names) | {"__future__"}


# -- 1. no v1 imports -----------------------------------------------------------------------------------------------
def v1_names() -> frozenset[str]:
    names = {p.stem for p in ROOT.glob("*.py")} | set(V1_MODULES)
    names |= {p.parent.name for p in ROOT.glob("*/__init__.py") if p.parent.name not in ("titan2", "tests")}
    return frozenset(names - _stdlib() - {"titan2", "tests", "conftest", "setup"})


def test_no_v1_imports():
    v1 = v1_names()
    assert "config" in v1 and "zerodha_tools" in v1
    bad = []
    for name in modules():
        for mod, node in imports(name):
            if mod.split(".")[0] in v1:
                bad.append(f"{_where(name, node)} imports v1 module {mod!r}")
    assert not bad, "titan2 never imports v1 code (DETAIL §1 rule 5):" + _report(bad)


# -- 2. only the Kite adapter touches the Kite order methods --------------------------------------------------------
def test_only_kite_adapter_calls_order_methods():
    bad = []
    for name in modules():
        if name == "titan2.broker.kite":
            continue
        t = tree(name)
        for ident, node in identifiers(t):
            if ident in ORDER_METHODS:
                bad.append(f"{_where(name, node)} references {ident!r}")
        for text, node in strings(t):
            if text in ORDER_METHODS:
                bad.append(f"{_where(name, node)} names {text!r} in a string (getattr?)")
    assert not bad, ("only titan2/broker/kite.py may reference place_order, cancel_order or modify_order "
                     "(DETAIL §2); rename other methods that share these names:" + _report(sorted(set(bad))))


# -- 3. no GTT, SL-M or MARKET ----------------------------------------------------------------------------------------
def test_no_gtt_slm_market_identifiers():
    bad = []
    for name in modules():
        t = tree(name)
        for ident, node in identifiers(t):
            if ident in GTT_AND_ORDER_TYPES:
                bad.append(f"{_where(name, node)} references {ident!r}")
        for text, node in strings(t):
            if text in FORBIDDEN_ORDER_STRINGS:
                bad.append(f"{_where(name, node)} has the string {text!r}")
    assert not bad, "Phase 1 has no MARKET, SL/SL-M or GTT orders (DETAIL §1 rule 4):" + _report(bad)


# -- 4. no multiplier ------------------------------------------------------------------------------------------------
def test_no_multiplier_identifier():
    bad = []
    for name in modules():
        t = tree(name)
        for ident, node in identifiers(t):
            if "multiplier" in ident.lower():
                bad.append(f"{_where(name, node)} identifier {ident!r}")
        for text, node in strings(t):
            if text.isidentifier() and "multiplier" in text.lower():
                bad.append(f"{_where(name, node)} string {text!r}")
    assert not bad, "size() is the only sizing and there is no multiplier anywhere (DETAIL §8.5):" + _report(bad)


# -- 5. the dashboard imports no kiteconnect, broker, core or store code ------------------------------------------------
DASH_FORBIDDEN = ("titan2.broker", "titan2.core", "titan2.store")


def _under(mod: str, prefixes) -> bool:
    return any(mod == p or mod.startswith(p + ".") for p in prefixes)


def test_dashboard_import_isolation():
    dash = [m for m in modules() if _under(m, ("titan2.dash",))]
    assert dash, "titan2.dash is missing"
    bad = []
    for name in dash:
        for mod, node in imports(name):
            if mod.split(".")[0] == "kiteconnect" or _under(mod, DASH_FORBIDDEN):
                bad.append(f"{_where(name, node)} imports {mod}")
        for m in sorted(closure(name)):
            if _under(m, DASH_FORBIDDEN):
                bad.append(f"importing {name} runs {m}")
            for mod, node in imports(m, runtime_only=True):
                if mod.split(".")[0] == "kiteconnect":
                    bad.append(f"importing {name} runs {_where(m, node)}: import {mod}")
    assert not bad, "titan2.dash imports no kiteconnect, broker, core or store code (DETAIL §2, §12.1):" + \
        _report(sorted(set(bad)))


def test_dashboard_import_isolation_at_runtime():
    pytest.importorskip("flask")
    code = textwrap.dedent("""
        import json, sys
        import titan2.dash.app
        print(json.dumps(sorted(m for m in sys.modules if m.split(".")[0] in ("titan2", "kiteconnect"))))
    """)
    loaded = json.loads(_python(code).stdout.strip().splitlines()[-1])
    bad = [m for m in loaded if m.split(".")[0] == "kiteconnect" or _under(m, DASH_FORBIDDEN)]
    assert not bad, f"importing titan2.dash.app loaded {bad}"


# -- 6. strategies are pure -----------------------------------------------------------------------------------------------
STRATEGY_ALLOWED = ("titan2.strategies.base", "titan2.model")


def test_strategies_import_purity():
    names = [m for m in modules() if _under(m, ("titan2.strategies",))]
    assert "titan2.strategies.base" in names
    stdlib = _stdlib()
    bad = []
    for name in names:
        own_package = name == "titan2.strategies"           # the registry imports its strategy modules
        for mod, node in imports(name):
            top = mod.split(".")[0]
            if top in stdlib or not mod:
                continue
            if top == "titan2":
                if mod in ("titan2",) or _under(mod, STRATEGY_ALLOWED):
                    continue
                if own_package and _under(mod, ("titan2.strategies",)):
                    continue
                if mod.rsplit(".", 1)[0] in STRATEGY_ALLOWED:            # from titan2.model import X
                    continue
            bad.append(f"{_where(name, node)} imports {mod}")
        for m in sorted(closure(name)):
            if not (_under(m, ("titan2.strategies",)) or m in ("titan2", "titan2.model")):
                bad.append(f"importing {name} runs {m}")
    assert not bad, ("strategies/* import only titan2.strategies.base, titan2.model and the stdlib "
                     "(DETAIL §2, §10.1):" + _report(sorted(set(bad))))


# -- 7. no paper branches in core -------------------------------------------------------------------------------------------
CORE_FORBIDDEN_IMPORTS = ("titan2.broker.kite", "titan2.broker.paper", "titan2.market.feed", "titan2.dash")


def test_core_has_no_paper_branches():
    core = [m for m in modules() if _under(m, ("titan2.core",))]
    assert "titan2.core.engine" in core
    bad = []
    for name in core:
        t = tree(name)
        for mod, node in imports(name):
            if mod.split(".")[0] == "kiteconnect" or _under(mod, CORE_FORBIDDEN_IMPORTS):
                bad.append(f"{_where(name, node)} imports {mod}")
        for ident, node in identifiers(t):
            if ident in PAPER_INTERNALS:
                bad.append(f"{_where(name, node)} references the paper exchange's {ident!r}")
        if name != "titan2.core.events":                    # where the snapshot sources are enumerated
            for text, node in strings(t):
                if text == "SIM" or text.startswith("SIM:"):
                    bad.append(f"{_where(name, node)} tests for the paper source {text!r}")
        if name in ORDER_PATH:
            for node in ast.walk(t):
                if isinstance(node, ast.Attribute) and node.attr in ("PAPER", "SHADOW") and \
                        isinstance(node.value, ast.Name) and node.value.id == "Account":
                    bad.append(f"{_where(name, node)} branches on Account.{node.attr}")
                if isinstance(node, ast.Compare):
                    for side in [node.left, *node.comparators]:
                        if isinstance(side, ast.Constant) and side.value in ("PAPER", "SHADOW", "SIM"):
                            bad.append(f"{_where(name, node)} compares with {side.value!r}")
    assert not bad, ("the core handles paper and LIVE alike: paper-sim answers with the same events (DETAIL §2, "
                     "§6):" + _report(sorted(set(bad))))


# -- 8. store writes from the core thread only --------------------------------------------------------------------------------
def _class(t: ast.Module, name: str) -> ast.ClassDef:
    for node in t.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {f.name: f for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _self_calls(fn: ast.AST, *, via: str | None = None) -> set[str]:
    """Names of self.<x>(...) calls (via='store': self.store.<x>(...)) in fn."""
    out = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func.value
        if via is None and isinstance(target, ast.Name) and target.id == "self":
            out.add(node.func.attr)
        elif via is not None and isinstance(target, ast.Attribute) and target.attr == via and \
                isinstance(target.value, ast.Name) and target.value.id == "self":
            out.add(node.func.attr)
    return out


def _writers(methods: dict[str, ast.AST], seeds: set[str], via: str | None = None,
             external: frozenset[str] = frozenset()) -> set[str]:
    """Methods that reach a write: they call a seed (self.<seed>, or self.<via>.<external>) directly or through
    other methods of the class."""
    calls = {n: _self_calls(f) for n, f in methods.items()}
    ext = {n: (_self_calls(f, via=via) & external) if via else set() for n, f in methods.items()}
    writers = {n for n in methods if calls[n] & seeds or ext[n]}
    while True:
        more = {n for n in methods if n not in writers and calls[n] & writers}
        if not more:
            return writers
        writers |= more


def test_store_writers_core_only():
    from titan2 import store as store_mod
    from titan2.core.engine import Engine

    store_methods = _methods(_class(tree("titan2.store"), "Store"))
    for prim in ("_w", "_insert", "_upsert", "_update_diff"):
        assert "_check_write" in _self_calls(store_methods[prim]) or \
            _self_calls(store_methods[prim]) & {"_w", "_insert"}, f"Store.{prim} must go through _check_write"
    store_writers = _writers(store_methods, set(STORE_WRITE_PRIMITIVES))
    public_store_writers = sorted(n for n in store_writers if not n.startswith("_"))
    assert {"insert_order", "update_order", "insert_fill", "insert_trade", "add_halt", "tx"} <= \
        set(public_store_writers), public_store_writers
    bad = [f"Store.{n}" for n in public_store_writers
           if not getattr(getattr(store_mod.Store, n), "__core_only__", False)]

    engine_methods = _methods(_class(tree("titan2.core.engine"), "Engine"))
    engine_writers = _writers(engine_methods, set(), via="store", external=frozenset(public_store_writers))
    public_engine_writers = sorted(n for n in engine_writers if not n.startswith("_"))
    assert "handle" in public_engine_writers, public_engine_writers
    for n in public_engine_writers:
        attr = Engine.__dict__.get(n)
        fn = attr.__func__ if isinstance(attr, (classmethod, staticmethod)) else attr
        if isinstance(attr, property) or isinstance(attr, classmethod):
            continue                                       # Engine.load builds a new engine before the loop runs
        if not getattr(fn, "__core_only__", False):
            bad.append(f"Engine.{n}")
    assert not bad, "every Store and Engine writer is @core_only (DETAIL §3.4, I1):" + _report(bad)

    importers = []
    for name in modules():
        if _under(name, ("titan2.dash", "titan2.strategies", "titan2.market", "titan2.broker")):
            for mod, node in imports(name):
                if _under(mod, ("titan2.store",)):
                    importers.append(f"{_where(name, node)} imports {mod}")
    assert not importers, "titan2.db is written by the core only; these packages may not import titan2.store:" + \
        _report(importers)


# -- 9, 10. the registry lock and the semantic constants hash --------------------------------------------------------------
def test_registry_lock_current():
    from titan2 import settings, strategies

    violations, stale = strategies.lock_status(settings.DEFAULTS)
    assert not violations, "REGISTRY.lock: a version bump is missing:" + _report(violations)
    assert not stale, ("REGISTRY.lock is stale; refresh it with "
                       f"{strategies.base.UPDATE_LOCK_CMD}:" + _report(stale))
    pinned = json.loads(strategies.LOCK_PATH.read_text(encoding="utf-8"))
    assert set(pinned) == set(strategies.REGISTRY) | {"_engine"}


def test_semantic_constants_hash_current():
    from titan2 import settings, strategies

    text = json.dumps(settings.SEMANTIC_CONSTANTS, sort_keys=True, separators=(",", ":"), default=repr)
    assert settings.SEMANTIC_HASH == hashlib.sha256(text.encode("utf-8")).hexdigest()
    for key, value in settings.SEMANTIC_CONSTANTS.items():
        assert getattr(settings, key) == value, f"SEMANTIC_CONSTANTS[{key!r}] is a stale copy of settings.{key}"
    required = {"EXIT_BOUNDS", "BANDS", "MAX_EXIT_DISCOUNT", "THIN_BOOK_PENALTY", "ORDER_POLL_AFTER_S",
                "ORDER_CANCEL_AFTER_S", "ORDER_STUCK_AFTER_S", "LOOKUP_AT_S", "FRESH_FILL_GRACE_S",
                "POSITIONS_POST_FILL_LAG_S", "RECON_CONFIRM_GAP_S", "POSITIONS_WAIT_MAX_S", "EXIT_CAP_PRIOR_READ_S",
                "BROKER_READS_FAIL_UNCAP_S", "VERIFY_FLAT_RECHECK_S", "VERIFY_FLAT_TIMEOUT_S",
                "QUOTE_REQUEST_TIMEOUT_S", "INTENT_MAX_PER_MIN", "TIMER_DT"}
    required |= {n for n in vars(settings) if n.startswith("G1_") and n.isupper()}
    missing = sorted(required - set(settings.SEMANTIC_CONSTANTS))
    assert not missing, f"constants that change trading but are not in SEMANTIC_CONSTANTS (DETAIL §8.4): {missing}"
    engine = json.loads(strategies.LOCK_PATH.read_text(encoding="utf-8"))["_engine"]
    assert engine["engine_semantics_version"] == settings.ENGINE_SEMANTICS_VERSION
    assert engine["semantic_constants_hash"] == settings.SEMANTIC_HASH, (
        "SEMANTIC_CONSTANTS changed: bump ENGINE_SEMANTICS_VERSION in titan2/settings.py (every strategy gets a "
        f"new version and a new G1 trial), then refresh REGISTRY.lock with {strategies.base.UPDATE_LOCK_CMD}")


def test_semantic_constants_are_never_copied():
    """A SEMANTIC_CONSTANTS name is bound at module level only in titan2/settings.py, or in the module settings imports
    it from (clock.OPENING_QUOTE_GRACE_S). Anywhere else it is a copy the semantic hash never sees: changing it would
    change trading with no new version (the review of the B1 batch found recon's own EXIT_CAP_PRIOR_READ_S)."""
    from titan2 import settings

    keys = set(settings.SEMANTIC_CONSTANTS)
    homes = {tuple(dotted.rsplit(".", 1)) for dotted, _ in imports("titan2.settings")
             if dotted.startswith("titan2.") and dotted.rsplit(".", 1)[-1] in keys}
    problems = []
    for name in modules():
        if name == "titan2.settings":
            continue
        for node in tree(name).body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            else:
                continue
            problems += [f"{_where(name, node)}: {n.id} is a SEMANTIC_CONSTANTS entry; import it from titan2.settings"
                         for t in targets for n in ast.walk(t)
                         if isinstance(n, ast.Name) and n.id in keys and (name, n.id) not in homes]
    assert ("titan2.clock", "OPENING_QUOTE_GRACE_S") in homes
    assert not problems, "semantic constants copied outside titan2/settings.py:" + _report(problems)


# -- 11. Python 3.10 ---------------------------------------------------------------------------------------------------------
PY311_IMPORTS = {
    "typing": {"Self", "Never", "LiteralString", "Required", "NotRequired", "Unpack", "TypeVarTuple",
               "assert_never", "assert_type", "reveal_type", "dataclass_transform", "override", "get_overloads",
               "clear_overloads", "TypeAliasType"},
    "enum": {"StrEnum", "ReprEnum", "verify", "member", "nonmember", "global_enum", "EnumCheck", "FlagBoundary"},
    "datetime": {"UTC"},
    "asyncio": {"TaskGroup", "timeout", "timeout_at", "Runner"},
    "itertools": {"batched"},
    "hashlib": {"file_digest"},
    "contextlib": {"chdir"},
    "math": {"cbrt", "exp2"},
}
PY311_MODULES = {"tomllib", "wsgiref.types"}
PY311_NAMES = {"ExceptionGroup", "BaseExceptionGroup"}


def _py310_files() -> list[Path]:
    return [p for p in sorted(PKG.rglob("*.py")) + sorted(TESTS.rglob("*.py")) if "__pycache__" not in p.parts]


def _pep701_fstrings(src: str) -> list[int]:
    """Lines of f-strings only Python 3.12 accepts (an inner string reusing the outer quote, or a backslash in
    a replacement field). Older parsers reject them on their own."""
    if sys.version_info < (3, 12):
        return []
    bad = []
    stack: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.FSTRING_START:
            quote = tok.string.lstrip("rRfFbBuU")
            if stack and quote[0] == stack[-1][0]:
                bad.append(tok.start[0])
            stack.append(quote)
        elif tok.type == tokenize.FSTRING_END:
            stack.pop()
        elif stack and tok.type == tokenize.STRING:
            quote = tok.string.lstrip("rRfFbBuU")
            if quote[0] == stack[-1][0]:
                bad.append(tok.start[0])
            if "\\" in tok.string:
                bad.append(tok.start[0])
    return bad


def test_python310_syntax():
    bad = []
    for path in _py310_files():
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        try:
            t = ast.parse(src, filename=str(path), feature_version=(3, 10))
        except SyntaxError as exc:
            bad.append(f"{rel}:{exc.lineno}: {exc.msg}")
            continue
        bad += [f"{rel}:{line}: f-string syntax needs Python 3.12" for line in _pep701_fstrings(src)]
        for node in ast.walk(t):
            if isinstance(node, ast.ImportFrom) and node.module in PY311_IMPORTS:
                for a in node.names:
                    if a.name in PY311_IMPORTS[node.module]:
                        bad.append(f"{rel}:{node.lineno}: from {node.module} import {a.name} needs Python 3.11+")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                bad += [f"{rel}:{node.lineno}: import {m} needs Python 3.11+" for m in mods if m in PY311_MODULES]
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
                    node.attr in PY311_IMPORTS.get(node.value.id, ()):
                bad.append(f"{rel}:{node.lineno}: {node.value.id}.{node.attr} needs Python 3.11+")
            if isinstance(node, ast.Name) and node.id in PY311_NAMES:
                bad.append(f"{rel}:{node.lineno}: {node.id} needs Python 3.11+")
    assert not bad, "titan2 and its tests run on Python 3.10 (DETAIL §0.5):" + _report(bad)


# -- 12. importable on Windows ------------------------------------------------------------------------------------------------
_BLOCKER = textwrap.dedent("""
    import importlib, importlib.abc, json, sys, traceback

    BLOCKED = ("twisted", "fcntl")
    attempts = []

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                attempts.append(name)
                raise ModuleNotFoundError(f"No module named {name!r} (blocked: not on Windows)", name=name)
            return None

    for m in [m for m in sys.modules if m.split(".")[0] in BLOCKED]:
        del sys.modules[m]
    sys.meta_path.insert(0, Block())
    failed = {}
    for name in NAMES:
        try:
            importlib.import_module(name)
        except BaseException:
            failed[name] = traceback.format_exc(limit=3)
    print(json.dumps({"failed": failed, "attempts": sorted(set(attempts))}))
""")


def _python(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc


def test_windows_importable():
    names = sorted(m for m in modules() if m not in WINDOWS_EXEMPT)
    assert "titan2.broker.kite" in names and "titan2.dash.app" in names
    out = _python(f"NAMES = {names!r}\n" + _BLOCKER).stdout.strip().splitlines()[-1]
    result = json.loads(out)
    assert not result["failed"], ("every module except titan2.runtime and titan2.market.feed imports without "
                                  "twisted or fcntl (DETAIL §0.5):" +
                                  _report([f"{n}:\n{tb}" for n, tb in result["failed"].items()]))
