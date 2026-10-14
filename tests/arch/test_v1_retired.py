"""U23, retire v1 (spec §15, DETAIL §15): the v1 files are gone, the `_*` research scripts and the research data are
untouched, nothing in titan2 or its tests imports a deleted v1 module, and git ignores secrets, databases and logs.

v1_retired.json is the record of the deletion: the spec's deletion_list, what the guardrails kept from it and why, the
git-tracked files deleted, the `_*` scripts with their git blob ids, and the tracked research data and docs kept. It
was built from commit b4694f3 (tag v1-final), which keeps every deleted file recoverable:
    git worktree add ../titan_v1 v1-final
"""

from __future__ import annotations

import ast
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.arch

ROOT = Path(__file__).resolve().parents[2]          # agentic_trader/
REPO = ROOT.parent                                  # manifest paths are relative to the repository root
MANIFEST = Path(__file__).with_name("v1_retired.json")
SCANNED = (ROOT / "titan2", ROOT / "tests")

# Never deleted, whatever deletion_list says (DETAIL §15 and the user's rules). `_*` files, *.db and logs are
# covered by name in _guardrail().
NEVER_DELETED = (
    "agentic_trader/.kite_access_token", "agentic_trader/dhan_config.json",     # leaked tokens: cleanup deferred
    "_titan_dashboard.html", "agentic_trader/CHANGELOG.md", "agentic_trader/TITAN_INSTRUCTIONS.md",
    "agentic_trader/docs/", "agentic_trader/trade_ledger/", "agentic_trader/daily_summaries/",
    "agentic_trader/ml_models/data/", "agentic_trader/ml_models/oi_snapshots/",
    "agentic_trader/ml_models/saved_models/", "agentic_trader/ml_models/direction/",
    "agentic_trader/ml_models/reports/",
)
# What stays in the tree: DETAIL §15 "Keep", plus the model folders the user protects.
KEEP = (
    "agentic_trader/trade_ledger/", "agentic_trader/daily_summaries/", "agentic_trader/all_trades_dump.json",
    "agentic_trader/sniper_trades_dump.json", "agentic_trader/speed_gate_log.jsonl",
    "agentic_trader/ml_models/data/", "agentic_trader/ml_models/oi_snapshots/",
    "agentic_trader/ml_models/saved_models/", "agentic_trader/ml_models/direction/",
    "agentic_trader/ml_models/reports/", "agentic_trader/CHANGELOG.md", "agentic_trader/TITAN_INSTRUCTIONS.md",
    "agentic_trader/docs/v1/TECHNICAL_DOCUMENTATION.md", "agentic_trader/docs/v1/MISSED_TRADE_DIAGNOSTIC_PLAYBOOK.md",
)
UNDERSCORE_DIRECT = 202                             # DETAIL §15: "the 202 tracked under agentic_trader/"
GITIGNORED = (".env", "*.env", ".env.*", "*.pem", "kite_session.json", ".kite_access_token", "dhan_config.json",
              "*.db", "*.db-wal", "*.db-shm", "*.db-journal", "*.log", "logs/")


@lru_cache(maxsize=None)
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _under(path: str, entry: str) -> bool:
    """path is entry, or lies inside it when entry is a directory (trailing /)."""
    return path == entry or (entry.endswith("/") and path.startswith(entry))


def _guardrail(path: str) -> str | None:
    """Why path may never be deleted, or None."""
    name = path.rsplit("/", 1)[-1]
    if name.startswith("_"):
        return "a _* file"
    if name.endswith((".db", ".db-wal", ".db-shm", ".db-journal", ".log")) or "/logs/" in f"/{path}":
        return "a database or log"
    hit = next((e for e in NEVER_DELETED if _under(path, e)), None)
    return f"under {hit}" if hit else None


def _repo_checkout() -> None:
    """The `_*` scripts and the research data exist in a checkout of the repository, not in a release tree."""
    if not (REPO / ".git").exists():
        pytest.skip(f"{REPO} is not a git checkout (a release tree has no research files)")


def _report(items: list[str]) -> str:
    more = f"\n  ... and {len(items) - 40} more" if len(items) > 40 else ""
    return "\n  " + "\n  ".join(items[:40]) + more


def _blob_id(data: bytes) -> str:
    """git's object id for a file with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# -- the record --------------------------------------------------------------------------------------------------
def test_manifest_applies_the_guardrails():
    """Every deleted file comes from deletion_list and no guardrail covers it; every entry was deleted or kept."""
    m = manifest()
    entries, protected, deleted = m["deletion_list"], m["protected"], m["deleted"]
    assert m["baseline"] == "b4694f3" and len(entries) == len(set(entries)) == 134
    bad = [f"{p}: not on deletion_list" for p in deleted if not any(_under(p, e) for e in entries)]
    bad += [f"{p}: deleted, but {_guardrail(p)}" for p in deleted if _guardrail(p)]
    bad += [f"{p}: kept without a guardrail" for p in protected if not _guardrail(p)]
    bad += [f"{e}: neither deleted nor kept by a guardrail" for e in entries
            if e not in protected and not any(_under(p, e) for p in deleted)]
    kept = set(m["kept"]) | set(m["underscore_scripts"])
    bad += [f"{p}: both deleted and kept" for p in sorted(kept & set(deleted))]
    assert not bad, "v1_retired.json breaks the deletion guardrails:" + _report(bad)


# -- the four checks -------------------------------------------------------------------------------------------------
def test_deleted_absent():
    """No file U23 deleted is back: v1 runs only from a v1-final worktree, never from this tree."""
    back = [p for p in manifest()["deleted"] if (REPO / p).exists()]
    assert not back, "deleted v1 files are back in the tree:" + _report(back)


def test_underscore_202_unchanged():
    """The `_*` research scripts are byte-for-byte the v1-final files (user rule: never modified or deleted): the 202
    tracked `_*` files directly under agentic_trader/, the two in trade_ledger/ and the repository-root `_*.py`."""
    _repo_checkout()
    scripts = manifest()["underscore_scripts"]
    direct = [p for p in scripts if p.startswith("agentic_trader/") and p.count("/") == 1]
    assert len(direct) == UNDERSCORE_DIRECT, len(direct)
    assert {"agentic_trader/trade_ledger/_analyze_today.py", "agentic_trader/trade_ledger/_deep_loss_analysis.py"} \
        <= set(scripts)
    assert any("/" not in p for p in scripts), "the repository-root _*.py scripts are missing from the record"
    missing, changed = [], []
    for rel, blob in sorted(scripts.items()):
        path = REPO / rel
        if not path.is_file():
            missing.append(rel)
            continue
        data = path.read_bytes()
        # a checkout with core.autocrlf=true has CRLF where git stores LF
        if blob not in (_blob_id(data), _blob_id(data.replace(b"\r\n", b"\n"))):
            changed.append(rel)
    assert not missing, "_* research scripts were deleted:" + _report(missing)
    assert not changed, "_* research scripts were modified (restore them from b4694f3):" + _report(changed)


def test_research_data_kept():
    """The research data, the protected model folders and the docs that stay are all still in the tree."""
    _repo_checkout()
    kept = manifest()["kept"]
    assert not [p for p in kept if not any(_under(p, k) for k in KEEP)]
    assert not [k for k in KEEP if not any(_under(p, k) for p in kept)]
    missing = [p for p in kept if not (REPO / p).is_file()]
    assert not missing, "kept files are missing from the tree:" + _report(missing)


def deleted_modules() -> frozenset[str]:
    """Dotted names of the deleted v1 modules: agentic_trader/config.py -> config, agentic_trader/ml_models/trainer.py
    -> ml_models.trainer, agentic_trader/deploy/auto_auth.py -> deploy.auto_auth."""
    prefix = "agentic_trader/"
    return frozenset(p[len(prefix):-len(".py")].replace("/", ".") for p in manifest()["deleted"]
                     if p.startswith(prefix) and p.endswith(".py"))


def _imported_names(tree: ast.AST):
    """(dotted name, line) of every absolute import, and of importlib.import_module / __import__ of a literal."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.module, node.lineno
            yield from ((f"{node.module}.{a.name}", node.lineno) for a in node.names if a.name != "*")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if called in ("import_module", "__import__"):
                yield node.args[0].value, node.lineno


def test_no_deleted_v1_module_imported():
    """titan2 and its tests import no module U23 deleted, nor anything from a v1 package whose code it deleted
    (ml_models, deploy): titan2 does not depend on v1."""
    mods = deleted_modules()
    assert {"config", "zerodha_tools", "autonomous_trader", "trade_query", "watcher_pipeline", "ml_models.predictor",
            "deploy.auto_auth"} <= mods
    packages = {m.split(".")[0] for m in mods if "." in m}
    bad = []
    for base in SCANNED:
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name, line in _imported_names(tree):
                parts = name.split(".")
                if parts[0] in packages or any(".".join(parts[:i]) in mods for i in range(1, len(parts) + 1)):
                    bad.append(f"{path.relative_to(ROOT).as_posix()}:{line} imports {name}")
    assert not bad, "titan2 never depends on v1 (DETAIL §1 rule 5):" + _report(bad)


def test_gitignore_secrets_db_logs():
    """The repository's .gitignore keeps secrets, SQLite files and logs out of git (U23 step 4)."""
    path = REPO / ".gitignore"
    if not path.is_file():
        pytest.skip("no repository .gitignore here (a release tree)")
    lines = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()}
    missing = [p for p in GITIGNORED if p not in lines]
    assert not missing, f"{path} must ignore {missing}"
