"""Docs checks (U24): TITAN_INSTRUCTIONS.md, CHANGELOG.md, docs/RUNBOOK.md and docs/v1/.

The docs are what an operator reads at 15:22 with a position open, so the facts they act on are checked against the
code: the settings table (rendered from titan2.settings.LIMITS), the halt table (risk.HALT_RULES), the entry gates
(risk.GATES), the exit reasons (model.EXIT_REASONS), every alert code the code can raise, the self-check rows and
the CLI verbs. A failure says which doc to fix and, for the settings table, prints the block to paste.

Every unit is built, so a quoted path, a systemd unit or the CLI module that is missing is a plain failure (the
"awaiting <unit>" xfails of the build waves are gone: they would now hide a deleted file).

Run from agentic_trader/: python -m pytest tests/unit/test_docs.py -q -p no:cacheprovider
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest

import titan2
from titan2 import g1, selfcheck, strategies
from titan2 import settings as settings_mod
from titan2.core import engine as engine_mod
from titan2.core import risk
from titan2.model import EXIT_REASONS
from titan2.strategies.base import UPDATE_LOCK_CMD
from titan2.strategies.price_spike_down import PriceSpikeDown
from titan2.strategies.price_spike_up import PriceSpikeUp

ROOT = Path(__file__).resolve().parents[2]          # agentic_trader/
REPO = ROOT.parent
PACKAGE = ROOT / "titan2"
INSTRUCTIONS = ROOT / "TITAN_INSTRUCTIONS.md"
CHANGELOG = ROOT / "CHANGELOG.md"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
V1_DIR = ROOT / "docs" / "v1"
V1_README = V1_DIR / "README.md"
CLI_MAIN = PACKAGE / "__main__.py"
LAPTOP = ROOT / "docs" / "LAPTOP.md"

# The docs this unit wrote (the archived v1 docs under docs/v1/ are history and are not link-checked).
OWN_DOCS = (INSTRUCTIONS, RUNBOOK, V1_README, CHANGELOG)

# Phase 2 is designed, not built (TITAN_INSTRUCTIONS §16): its paths may be named while they do not exist.
PLANNED = ("titan2/multileg/", "tests/multileg/", "titan2/migrations/0002_multileg.sql")

INSTRUCTIONS_SECTIONS = (
    "1. What titan2 is",
    "2. Rules for changing titan2",
    "3. Market facts",
    "4. Architecture",
    "5. Module map",
    "6. How a trade happens",
    "7. Exits",
    "8. Risk, sizing and settings",
    "9. Halts and operator commands",
    "10. Reconciliation and restarts",
    "11. Strategies, G1 and LIVE",
    "12. Server layout, data and files",
    "13. Daily timeline",
    "14. Operating titan2",
    "15. Tests",
    "16. Phase 2: multi-leg (designed, not built)",
    "17. v1 history",
)
RUNBOOK_SECTIONS = (
    "1. Ground rules",
    "2. Quick reference",
    "3. Daily routine",
    "4. Playbooks",
    "5. Alert codes",
    "6. Halts",
    "7. Morning self-check",
    "8. Cutover stages",
    "9. Drills",
    "10. [V] verification checklist and results",
)
# DETAIL §13.12, in order; the runbook may add more after these.
SPEC_PLAYBOOKS = (
    "P1. Bot down with positions open",
    "P2. MISMATCH or EXTERNAL_CLOSE",
    "P3. FOREIGN position (a manual trade)",
    "P4. SHORT_DETECTED not explained by the bot",
    "P5. Token failure",
    "P6. Feed dead",
    "P7. Not flat at 15:22",
    "P8. Bot misbehaving but positions need managing",
    "P9. Deploy failed",
    "P10. DB corruption",
)
V_ROWS = tuple(f"V{i}" for i in range(1, 14)) + tuple(f"L{i}" for i in range(1, 8))   # §17.2 items + late facts
V_STATUSES = ("PENDING", "PARTIAL", "VERIFIED", "FAILED")
V1_CHANGELOG_HEADINGS = ("2026-03-09", "2026-03-08", "2026-03-06", "2026-03-02", "Pre-March 2026")
V1_MOVED = ("TECHNICAL_DOCUMENTATION.md", "MISSED_TRADE_DIAGNOSTIC_PLAYBOOK.md")

SETTINGS_START = "<!-- settings-table:start (generated from titan2.settings.LIMITS; tests/unit/test_docs.py) -->"
SETTINGS_END = "<!-- settings-table:end -->"


# ---------------------------------------------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"(?<!\\)!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SPAN = re.compile(r"(`+)(.+?)\1")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_REPO_PATH = re.compile(r"(?:titan2|tests|ops|docs)/[\w.-][\w./-]*")
_ROOT_FILES = frozenset({"TITAN_INSTRUCTIONS.md", "CHANGELOG.md", "pyproject.toml", "requirements.lock",
                         "requirements-dev.txt", ".github/workflows/ci.yml"})
# `titan2 <verb>` as a command: at the start of a snippet, after && or ;, or as `python -m titan2 <verb>`
# (not `pyflakes titan2 tests`, where titan2 is an argument).
_VERB = re.compile(r"(?:^|&&\s*|;\s*|-m\s+)titan2 ([a-z][a-z0-9-]*)")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _split(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """(prose lines, fenced-code lines), each as (1-based line number, text)."""
    prose, code, fenced = [], [], False
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        (code if fenced else prose).append((n, line))
    return prose, code


def _strip_spans(line: str) -> str:
    return _SPAN.sub("", line)


def _headings(text: str) -> list[tuple[int, str, int]]:
    """(level, title, line number) of every heading outside fenced code."""
    out = []
    for n, line in _split(text)[0]:
        m = _HEADING.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2), n))
    return out


def _slug(title: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped (word chars, '-' and ' ' kept), spaces -> '-'."""
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)       # a link in a heading keeps its text
    plain = plain.replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", plain).replace(" ", "-")


def _anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for _, title, _ in _headings(text):
        base = _slug(title)
        k = seen.get(base, 0)
        out.add(base if k == 0 else f"{base}-{k}")
        seen[base] = k + 1
    return out


def _section(text: str, title: str) -> str:
    """The body of the heading `title` (any level) up to the next heading of the same or a higher level."""
    lines = text.splitlines()
    heads = _headings(text)
    for i, (level, t, n) in enumerate(heads):
        if t == title:
            end = next((m for lv, _, m in heads[i + 1:] if lv <= level), len(lines) + 1)
            return "\n".join(lines[n:end - 1])
    raise AssertionError(f"heading {title!r} not found")


def _table_rows(block: str) -> list[list[str]]:
    """Cells of every markdown table row in block (header and separator rows dropped)."""
    rows = []
    for line in block.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rows.append(cells)
    return rows


def _ticked(cell: str) -> set[str]:
    return {m.group(2).strip() for m in _SPAN.finditer(cell)}


def _code_snippets(text: str) -> list[tuple[int, str]]:
    """Every inline code span and every fenced code line, with its line number."""
    prose, code = _split(text)
    out = [(n, m.group(2)) for n, line in prose for m in _SPAN.finditer(line)]
    return out + code


def _code_tokens(text: str) -> list[tuple[int, str]]:
    """Whitespace-separated tokens of the code snippets, with their line numbers."""
    return [(n, tok) for n, snippet in _code_snippets(text) for tok in snippet.split()]


def _resolve(rel: str, base: Path) -> Path:
    """A repo-relative path: agentic_trader/ first, then the repo root (for .github/)."""
    p = base / rel
    return p if p.exists() or not (REPO / rel).exists() else REPO / rel


def _changelog_v2(text: str) -> str:
    """The v2.0.0 entry: from its heading to the next level-2 heading."""
    heads = [(lv, t) for lv, t, _ in _headings(text) if lv == 2]
    assert heads, "CHANGELOG.md has no '## ' entries"
    return _section(text, heads[0][1])


# ---------------------------------------------------------------------------------------------------------------
# The settings table, rendered from the code (paste the printed block into TITAN_INSTRUCTIONS.md §8 when it changes)
# ---------------------------------------------------------------------------------------------------------------
def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dtime):
        return v.strftime("%H:%M") if v.second == 0 else v.strftime("%H:%M:%S")
    if isinstance(v, (tuple, list)):
        return "[" + ", ".join(map(str, v)) + "]"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{int(v):,}" if v.is_integer() else repr(v)
    return str(v)


def _allowed(lim: settings_mod.Limit) -> str:
    if lim.kind == "list":
        return "NSE symbol names"
    if lim.kind == "bool":
        return "false, true"
    if lim.fixed:
        return f"{_fmt(lim.lo)} only"
    if lim.lo is not None and lim.hi is not None:
        return f"{_fmt(lim.lo)} – {_fmt(lim.hi)}"
    return f"≥ {_fmt(lim.lo)}" if lim.lo is not None else f"≤ {_fmt(lim.hi)}"


_SAFER = {settings_mod.DOWN: "lower", settings_mod.UP: "higher", settings_mod.BAND: "fixed window",
          settings_mod.SUBSET: "any list only restricts"}
FIXED_TEXT = "neither: fixed in code"       # a key with one allowed value (the paper calibration, paper model 2)


def render_settings_table() -> str:
    rows = ["| Key | Default | Allowed | Safer | Versioned |", "|---|---|---|---|---|"]
    for key, lim in settings_mod.LIMITS.items():
        safer = "true" if lim.kind == "bool" else FIXED_TEXT if lim.fixed else _SAFER[lim.tighten]
        rows.append(f"| `{key}` | {_fmt(lim.default)} | {_allowed(lim)} | {safer} | "
                    f"{'yes' if lim.versioned else 'no'} |")
    return "\n".join(rows)


# ---------------------------------------------------------------------------------------------------------------
# Code facts
# ---------------------------------------------------------------------------------------------------------------
_SEVERITIES = ("CRITICAL", "WARN", "INFO")
_CODE_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
# An alert is raised by any call shaped (severity, CODE, text, ...): Alert(...), send_now(...), or a helper such as
# send()/submit()/_alert() called with a literal severity first; plus critical_now(text, code=CODE).
_ANY_SEVERITY_CALLS = frozenset({"Alert", "send_now"})


def _severity(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and node.value in _SEVERITIES:
        return node.value
    if isinstance(node, ast.Attribute) and node.attr in _SEVERITIES:
        return node.attr
    if isinstance(node, ast.Name) and node.id in _SEVERITIES:
        return node.id
    return "?"                                   # decided at run time


def alert_codes() -> dict[str, tuple[str, str]]:
    """{code: (severity or '?', 'file:line')} for every alert raised with a constant code anywhere in titan2/."""
    out: dict[str, tuple[str, str]] = {}
    rank = {"CRITICAL": 0, "?": 1, "WARN": 2, "INFO": 3}
    for p in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(_read(p), filename=str(p))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            if name == "critical_now":                           # alerts.critical_now(text, code="...")
                sev, code = "CRITICAL", kw.get("code")
            elif len(node.args) >= 2:
                sev, code = _severity(node.args[0]), node.args[1]
                if sev == "?" and name not in _ANY_SEVERITY_CALLS:
                    continue
            else:
                continue
            if not (isinstance(code, ast.Constant) and isinstance(code.value, str)
                    and _CODE_RE.fullmatch(code.value) and code.value not in _SEVERITIES):
                continue
            where = f"{p.relative_to(ROOT).as_posix()}:{node.lineno}"
            old = out.get(code.value)
            if old is None or rank[sev] < rank[old[0]]:
                out[code.value] = (sev, where)
    return out


def cli_verbs_in_code() -> set[str]:
    tree = ast.parse(_read(CLI_MAIN), filename=str(CLI_MAIN))
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# ---------------------------------------------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("doc", ["instructions", "runbook", "changelog", "v1"])
def test_docs_sections(doc: str) -> None:
    if doc == "instructions":
        text = _read(INSTRUCTIONS)
        h2 = [t for lv, t, _ in _headings(text) if lv == 2]
        assert h2 == list(INSTRUCTIONS_SECTIONS), f"TITAN_INSTRUCTIONS.md sections must be {INSTRUCTIONS_SECTIONS}"
        h1 = [t for lv, t, _ in _headings(text) if lv == 1]
        assert len(h1) == 1 and "titan2" in h1[0].lower(), "TITAN_INSTRUCTIONS.md needs one '# ' title naming titan2"
        assert titan2.__version__ in text.split("\n## ", 1)[0], "the header must state titan2.__version__"
        facts = _section(text, "3. Market facts")
        for fact in ("15:40", "09:20", "09:15:30", "Tuesday", "NIFTY 65", "BANKNIFTY 30", "10 orders", "[V]"):
            assert fact in facts, f"§3 Market facts must mention {fact!r}"
        assert UPDATE_LOCK_CMD in _section(text, "15. Tests"), "§15 must quote strategies.base.UPDATE_LOCK_CMD"
    elif doc == "runbook":
        text = _read(RUNBOOK)
        h2 = [t for lv, t, _ in _headings(text) if lv == 2]
        assert h2 == list(RUNBOOK_SECTIONS), f"docs/RUNBOOK.md sections must be {RUNBOOK_SECTIONS}"
        h3 = [t for lv, t, _ in _headings(_section(text, "4. Playbooks")) if lv == 3]
        assert tuple(h3[:len(SPEC_PLAYBOOKS)]) == SPEC_PLAYBOOKS, f"playbooks must start with {SPEC_PLAYBOOKS}"
        assert all(re.match(rf"P{i}\. ", t) for i, t in enumerate(h3, 1)), f"playbooks must be numbered P1..Pn: {h3}"
        rows = {r[0]: r for r in _table_rows(_section(text, "10. [V] verification checklist and results"))}
        missing = [k for k in V_ROWS if k not in rows]
        assert not missing, f"[V] table rows missing: {missing}"
        for k in V_ROWS:
            status = [c for c in rows[k] if c.split(" ")[0].strip("*") in V_STATUSES]
            assert status, f"[V] row {k} needs a status cell starting with one of {V_STATUSES}: {rows[k]}"
    elif doc == "changelog":
        text = _read(CHANGELOG)
        h2 = [t for lv, t, _ in _headings(text) if lv == 2]
        assert f"v{titan2.__version__}" in h2[0], f"the newest CHANGELOG entry must be v{titan2.__version__}: {h2[0]!r}"
        v2 = _changelog_v2(text)
        for k in ("Added", "Removed", "Safety", "Operations", "Docs"):
            assert f"### {k}" in v2, f"the v2.0.0 entry needs a '### {k}' part"
        for old in V1_CHANGELOG_HEADINGS:
            assert old in h2, f"CHANGELOG.md is appended, never rewritten: v1 entry {old!r} is gone"
    else:
        for name in V1_MOVED:
            assert (V1_DIR / name).is_file(), f"docs/v1/{name} missing (git mv from agentic_trader/)"
            assert not (ROOT / name).exists(), f"{name} must live only in docs/v1/"
        assert (V1_DIR / "TITAN_INSTRUCTIONS_v1.md").is_file()
        readme = _read(V1_README)
        for name in V1_MOVED + ("TITAN_INSTRUCTIONS_v1.md", "v1-final", "b4694f3"):
            assert name in readme, f"docs/v1/README.md must mention {name}"


def test_docs_links() -> None:
    """Every relative markdown link in our docs resolves to a file or directory, and every #anchor to a heading."""
    problems = []
    for doc in OWN_DOCS:
        text = _read(doc)
        body = _changelog_v2(text) if doc == CHANGELOG else text
        for n, line in _split(body)[0]:
            for m in _LINK.finditer(_strip_spans(line)):
                target = m.group(1)
                if re.match(r"[a-z][a-z0-9+.-]*:", target):           # http:, https:, mailto:
                    continue
                path, _, anchor = target.partition("#")
                dest = (doc.parent / path).resolve() if path else doc
                where = f"{doc.relative_to(ROOT).as_posix()}:{n}: {target}"
                if not dest.exists():
                    problems.append(f"{where}: no such file")
                    continue
                if anchor and dest.suffix == ".md" and anchor not in _anchors(_read(dest)):
                    problems.append(f"{where}: no heading with anchor #{anchor}")
    assert not problems, "broken links:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------------------------------------------
# Extra checks: the docs agree with the code
# ---------------------------------------------------------------------------------------------------------------
def test_docs_code_paths_exist() -> None:
    """Every repo path quoted in our docs (`titan2/...`, `ops/...`, `tests/...`, `docs/...`) exists."""
    hard = []
    for doc in OWN_DOCS:
        text = _read(doc)
        body = _changelog_v2(text) if doc == CHANGELOG else text
        for n, tok in _code_tokens(body):
            tok = tok.strip("`'\",;:()")
            if not (_REPO_PATH.fullmatch(tok) or tok in _ROOT_FILES):
                continue
            if _resolve(tok, ROOT).exists() or any(tok == p.rstrip("/") or tok.startswith(p) for p in PLANNED):
                continue
            hard.append(f"{doc.relative_to(ROOT).as_posix()}:{n}: {tok}")
    assert not hard, "docs quote paths that do not exist:\n" + "\n".join(hard)


def test_docs_inline_code_closed_per_line() -> None:
    """An inline code span never wraps to the next line (the checks here read spans line by line)."""
    bad = [f"{doc.relative_to(ROOT).as_posix()}:{n}" for doc in OWN_DOCS
           for n, line in _split(_read(doc))[0] if "`" in _strip_spans(line.replace("\\`", ""))]
    assert not bad, "inline code spans broken across lines at:\n" + "\n".join(bad)


def test_docs_module_map_covers_package() -> None:
    """Every file of the titan2 package appears in TITAN_INSTRUCTIONS §5, so a new module cannot go undocumented."""
    documented = set()
    for r in _table_rows(_section(_read(INSTRUCTIONS), "5. Module map")):
        documented |= _ticked(r[0])
    on_disk = {p.relative_to(ROOT).as_posix() for p in PACKAGE.rglob("*")
               if p.is_file() and "__pycache__" not in p.parts
               and p.suffix in (".py", ".sql", ".json", ".lock", ".html")}
    missing = sorted(on_disk - documented)
    assert not missing, f"TITAN_INSTRUCTIONS.md §5 module map lacks {missing}"


def test_docs_settings_table_matches_code() -> None:
    text = _read(INSTRUCTIONS)
    assert SETTINGS_START in text and SETTINGS_END in text, "TITAN_INSTRUCTIONS.md lost its settings-table markers"
    block = text.split(SETTINGS_START, 1)[1].split(SETTINGS_END, 1)[0].strip()
    expected = render_settings_table()
    assert block == expected, ("TITAN_INSTRUCTIONS.md §8 settings table differs from titan2.settings.LIMITS; "
                               "replace the block between the markers with:\n" + expected)


def test_docs_halts_gates_and_exit_reasons_match_code() -> None:
    runbook = _read(RUNBOOK)
    rows = [r for r in _table_rows(_section(runbook, "6. Halts")) if _ticked(r[0])]
    by_kind = {next(iter(_ticked(r[0]))): r for r in rows}
    assert set(by_kind) == set(risk.HALT_RULES), (
        f"RUNBOOK §6 halt table vs risk.HALT_RULES: missing {sorted(set(risk.HALT_RULES) - set(by_kind))}, "
        f"unknown {sorted(set(by_kind) - set(risk.HALT_RULES))}")
    header = _table_rows(_section(runbook, "6. Halts"))[0]
    col = header.index("Clears")
    for kind, rule in risk.HALT_RULES.items():
        assert str(rule.clears) in by_kind[kind][col], f"RUNBOOK §6: {kind} clears by {rule.clears}"

    text = _read(INSTRUCTIONS)
    gates = {(r[0], next(iter(_ticked(r[1])), r[1])) for r in _table_rows(text) if re.fullmatch(r"G\d\d", r[0])}
    missing = [g for g in risk.GATES if g not in gates]
    assert not missing, f"TITAN_INSTRUCTIONS.md gate table lacks {missing}"
    exits = _section(text, "7. Exits")
    missing = [r for r in EXIT_REASONS if f"`{r}`" not in exits]
    assert not missing, f"TITAN_INSTRUCTIONS.md §7 lacks exit reasons {missing}"


def test_docs_eod_timeline_matches_code() -> None:
    """TITAN_INSTRUCTIONS §13 shows every default EOD time, and the post-square-off offsets the engine uses."""
    s = settings_mod.Settings()
    square_off = datetime.combine(date(2026, 1, 1), s.BROKER_SQUAREOFF_AT)
    offsets = [secs for _, secs in engine_mod.EOD_AFTER_SQUAREOFF_S]
    times = [s.ENTRY_START, s.NO_NEW_AFTER, s.EOD_FLATTEN, s.EOD_BACKSTOP, s.STOP_SENDING, s.BROKER_SQUAREOFF_AT]
    times += [(square_off + timedelta(seconds=secs)).time() for secs in offsets]
    section = _section(_read(INSTRUCTIONS), "13. Daily timeline")
    missing = [_fmt(t) for t in times if _fmt(t) not in section]
    assert not missing, f"TITAN_INSTRUCTIONS.md §13 lacks the EOD times {missing}"
    assert all(secs % 60 == 0 for secs in offsets), "the §13 text states the offsets in whole minutes"
    stated = ", ".join(f"+{secs // 60}" for secs in offsets) + " minutes"
    assert stated in section, f"TITAN_INSTRUCTIONS.md §13 must state the offsets as ({stated})"


def test_docs_state_both_bounds_of_the_late_recon() -> None:
    """A start after BROKER_SQUAREOFF_AT keeps its late recon until LATE_RECON_UNTIL, or LATE_RECON_MIN_S after READY
    when that is later (Engine._mark_late_start). RUNBOOK P1 said "until 16:00 at most", which is wrong for a start
    after 15:59 (review round 2 follow-up)."""
    assert engine_mod.LATE_RECON_MIN_S == 60.0, "the docs say 'a minute after READY'"
    until = f"until {engine_mod.LATE_RECON_UNTIL:%H:%M}"
    for name, text in (("RUNBOOK.md P1", _section(_read(RUNBOOK), "P1. Bot down with positions open")),
                       ("TITAN_INSTRUCTIONS.md §10", _section(_read(INSTRUCTIONS), "10. Reconciliation and restarts"))):
        flat = " ".join(text.split())
        assert until in flat and "a minute after READY when later" in flat, \
            f"{name}: the late recon runs {until}, or a minute after READY when later"


def test_docs_alert_codes_documented() -> None:
    """Every CRITICAL, WARN or run-time-severity alert code in titan2/ has a row in RUNBOOK §5."""
    documented = set()
    for r in _table_rows(_section(_read(RUNBOOK), "5. Alert codes")):
        documented |= _ticked(r[0])
    codes = alert_codes()
    assert codes, "the alert-code scan found nothing: the scan is broken"
    missing = [f"{c} ({sev}, {where})" for c, (sev, where) in sorted(codes.items())
               if sev != "INFO" and c not in documented]
    assert not missing, "alert codes without a RUNBOOK §5 row:\n" + "\n".join(missing)


def test_docs_selfcheck_rows_documented() -> None:
    documented = set()
    for r in _table_rows(_section(_read(RUNBOOK), "7. Morning self-check")):
        documented |= _ticked(r[0])
    missing = [c.name for c in selfcheck.CHECKS if c.name not in documented]
    assert not missing, f"RUNBOOK §7 lacks self-check rows {missing}"


def test_docs_units_and_schedules_match_ops() -> None:
    """Every systemd unit in ops/systemd/ has a row in RUNBOOK §2, and every timer's OnCalendar time is in that row."""
    unit_dir = ROOT / "ops" / "systemd"
    units = sorted(p for p in unit_dir.glob("titan2-*") if p.suffix in (".service", ".timer"))
    assert units, "ops/systemd/ holds no titan2-*.service|.timer unit"
    rows = [r for r in _table_rows(_section(_read(RUNBOOK), "2. Quick reference")) if _SPAN.search(r[0])]
    problems = []
    for unit in units:
        row = next((r for r in rows if _SPAN.search(r[0]).group(2).startswith(unit.stem + ".")), None)
        if row is None:
            problems.append(f"{unit.name}: no row in the RUNBOOK §2 units table")
            continue
        for hhmm in re.findall(r"^OnCalendar=.*?(\d{2}:\d{2})", _read(unit), re.M):
            if hhmm not in " ".join(row):
                problems.append(f"{unit.name}: OnCalendar {hhmm} is not in its RUNBOOK §2 row {row}")
    assert not problems, "\n".join(problems)


def test_docs_laptop_deadman_check_fits_a_start_by_hand() -> None:
    """Review round 5: the laptop's docs gave the server's dead-man schedule (first tick 08:45), which rang the phone
    for every start after about 08:46 and for every early stop. docs/LAPTOP.md section 2 and the Windows env example
    now give the laptop's own, alerts.LAPTOP_DEADMAN_SCHEDULE (from 09:15; tests/unit/test_alerts_token_selfcheck.py
    plays a day on it), with its grace, and say to pause the check on NSE holidays and when stopping early, as
    section 4 does too. Review round 6: they said to pause it before an early stop, but titan2 pings while it stops
    (through the Ctrl+C flatten, and once more as it exits) and a ping resumes a paused check, which then rang 3 to 4
    minutes after the exit. They now say to pause it after titan2 has stopped, within a minute of the window saying
    so (the same test plays that stop on the schedule, with this advice read from section 4)."""
    from titan2 import alerts, laptop as laptop_mod, runtime

    laptop = _read(LAPTOP)
    (row,) = [" ".join(r) for r in _table_rows(_section(laptop, "2. Fill in the env file (your secrets)"))
              if r[0] == "`HEALTHCHECK_URL`"]
    env = _flat(_read(ROOT / "ops" / "windows" / "titan2.env.example").replace("#", " "))
    assert "titan2.alerts.LAPTOP_DEADMAN_SCHEDULE" in env and "45-59 8" not in row + env
    for line in alerts.LAPTOP_DEADMAN_SCHEDULE:
        assert f"`{line}`" in row and line in env, line
    assert f"grace {alerts.DEADMAN_GRACE_MIN} minutes" in row and f"{alerts.DEADMAN_GRACE_MIN} minutes grace" in env
    for text in (_flat(row), env):
        assert "a few minutes after you start it" in text and "09:10" in text
        assert "Pause the check on NSE holidays" in text and "its next ping resumes it" in text
        assert ("when you stop titan2 before 15:33 pause it after titan2 has stopped, within a minute (not before: "
                "titan2 pings it while it stops") in text
        assert "before you stop titan2 before 15:33" not in text           # the advice that rang the phone
    stop = _flat(_section(laptop, "4. How to stop safely"))
    assert ("**The dead-man check.** When you stop titan2 before 15:33, pause its check on healthchecks.io (the "
            "check's Pause button) after titan2 has stopped: once the window says `titan2 stopped cleanly`, within a "
            "minute") in stop
    assert "Not before: titan2 pings the check while it stops" in stop and "a ping resumes a paused check" in stop
    assert "Before you stop titan2 before 15:33, pause" not in stop
    session = laptop_mod.Session()                                     # the line the advice waits for
    session.request_stop("CTRL_C")
    assert laptop_mod._ending(runtime.EXIT_OK, session, ROOT) == "titan2 stopped cleanly (CTRL_C)"


def _decisions() -> str:
    """RUNBOOK §10's decisions record (the '### Decisions (2026-10-11)' block)."""
    return _section(_read(RUNBOOK), "Decisions (2026-10-11)")


def test_docs_record_decision_b2() -> None:
    """Decision B2 (2026-10-11): the 30 s late-accept window stays, and nobody shortens it without LIVE evidence. The
    RUNBOOK §10 decisions record names it, its value and the rule; the code comment above the constant cites it."""
    from titan2.core import orders
    assert "Decisions (2026-10-11)" in [t for _, t, _ in _headings(_section(_read(RUNBOOK),
                                                                          "10. [V] verification checklist and results"))]
    text = _decisions()
    b2 = " ".join(next(line for line in text.split("\n- ") if "**B2" in line).split())
    assert "`orders.LATE_ACCEPT_S`" in b2 and "30 s" in b2 and "naked short" in b2
    assert "Do not shorten it without that evidence" in b2 and "ORDER_PLACE_RESULT" in b2
    assert orders.LATE_ACCEPT_S == 30.0
    src = _read(PACKAGE / "core" / "orders.py").splitlines()
    i = next(n for n, line in enumerate(src) if line.startswith("LATE_ACCEPT_S = "))
    comment = []
    for line in reversed(src[:i]):
        if not line.startswith("#"):
            break
        comment.append(line)
    assert any("Decision B2 (2026-10-11)" in line for line in comment), "orders.py must cite decision B2 above it"


def test_docs_record_decision_b1() -> None:
    """Decision B1 (2026-10-11): the strict exit cap and the paper read delay are recorded next to B2, with where the
    burn-in measures them (the daily report row and the EXIT_ATTEMPT field), the per-retry read, the failure-path
    fallback and what is left of the naked-short risk. The constant lives in settings, inside SEMANTIC_CONSTANTS."""
    assert settings_mod.EXIT_CAP_PRIOR_READ_S == 0 and "EXIT_CAP_PRIOR_READ_S" in settings_mod.SEMANTIC_CONSTANTS
    b1 = " ".join(next(line for line in _decisions().split("\n- ") if "**B1" in line).split())
    for fact in ("`settings.EXIT_CAP_PRIOR_READ_S`", "`SEMANTIC_CONSTANTS`", "`PAPER_READ_LATENCY_MEDIAN_MS`",
                 "`exit_decision_to_submit_ms`", "decision_to_order_ms", "Each retry", "`EXIT_CAP_BEFORE_DECISION`"):
        assert fact in b1, fact
    stage2 = _section(_read(RUNBOOK), "8. Cutover stages")
    assert "`exit_decision_to_submit_ms`" in stage2 and "`decision_to_order_ms`" in stage2


def test_docs_cli_verbs_exist_in_cli() -> None:
    """Every `titan2 <verb>` the docs tell an operator to run is a string the CLI module defines."""
    verbs = {m.group(1) for doc in (INSTRUCTIONS, RUNBOOK) for _, snippet in _code_snippets(_read(doc))
             for m in _VERB.finditer(snippet)}
    assert {"status", "pause", "flatten-all", "kill", "ack-halt"} <= verbs, f"docs barely cover the CLI: {verbs}"
    assert CLI_MAIN.exists(), "titan2/__main__.py (the CLI) is missing"
    missing = sorted(verbs - cli_verbs_in_code())
    assert not missing, f"docs name CLI verbs that titan2/__main__.py does not define: {missing}"


# ---------------------------------------------------------------------------------------------------------------
# price_spike_down (2026-10-12, a PAPER experiment): what the docs say it changes for the others, against the code
# ---------------------------------------------------------------------------------------------------------------
PSD_CHANGELOG = "price_spike_down: a PAPER put experiment (2026-10-12)"


def _flat(text: str) -> str:
    """Whitespace collapsed, so that a phrase matches across line wraps."""
    return " ".join(text.split())


def _psd_docs() -> dict[str, str]:
    """The passages on price_spike_down: docs/LAPTOP.md sections 4 and 8, TITAN_INSTRUCTIONS §11 and its laptop part
    of §14, and the CHANGELOG entry."""
    laptop, instructions = _read(LAPTOP), _read(INSTRUCTIONS)
    return {"LAPTOP.md §4": _flat(_section(laptop, "4. How to stop safely")),
            "LAPTOP.md §8": _flat(_section(laptop, "8. What PAPER means")),
            "TITAN_INSTRUCTIONS.md §11": _flat(_section(instructions, "11. Strategies, G1 and LIVE")),
            "TITAN_INSTRUCTIONS.md §14": _flat(_section(instructions, "Laptop mode (Windows, PAPER only)")),
            "CHANGELOG.md": _flat(_section(_read(CHANGELOG), PSD_CHANGELOG))}


def test_docs_price_spike_down_shares_the_paper_accounts_g1_controls() -> None:
    """Review, 2026-10-12: adding price_spike_down left the other two strategies' code and versions alone, but not
    their G1 trials, because the three share the PAPER account's G1 controls (tests/scenarios/test_price_spike_down.py
    runs each one). The docs say so, with the code's times and limits, and never that it changed nothing for them."""
    s = settings_mod.Settings()
    eod, last_entry = _fmt(s.EOD_FLATTEN), _fmt(s.NO_NEW_AFTER)
    gap_s, frac = settings_mod.G1_IMPAIRED_GAP_S, settings_mod.G1_MAX_IMPAIRED_FRAC
    assert (gap_s, frac) == (600.0, 0.10)
    # the clean stop with nothing open: the last heartbeat before it (<= HEARTBEAT_EVERY_S earlier) to NO_NEW_AFTER
    #   must not exceed G1_IMPAIRED_GAP_S of the entry window
    clean_from = (datetime.combine(date(2026, 10, 12), s.NO_NEW_AFTER) - timedelta(seconds=gap_s)
                  + timedelta(seconds=settings_mod.HEARTBEAT_EVERY_S)).time()
    for kind in ("MISMATCH", "UNKNOWN_POSITION"):          # account-wide, until a human acks
        rule = risk.HALT_RULES[kind]
        assert rule.scopes == ("ACCOUNT",) and str(rule.clears) == "ACK", (kind, rule)
    assert callable(getattr(engine_mod.Engine, "_operator_impaired", None))
    docs = _psd_docs()
    for where, text in docs.items():
        for claim in ("changed nothing", "trials are untouched", "nothing for the other two"):
            assert claim not in text, f"{where} says {claim!r}, but the three share the PAPER account's G1 controls"
    laptop4, laptop8 = docs["LAPTOP.md §4"], docs["LAPTOP.md §8"]
    for fact in ("for every strategy, not only for the one holding a position",
                 f"Ctrl+C while any position is open (before the {eod} close)",
                 f"stays stopped for more than {gap_s / 60:.0f} minutes of entry time ({_fmt(s.ENTRY_START)} to "
                 f"{last_entry})", "Open positions panel is empty", f"press Ctrl+C at {_fmt(clean_from)} or later",
                 f"After {eod} a stop never marks the day", "A pause before"):
        assert fact in laptop4, f"LAPTOP.md §4 must say {fact!r}"
    for fact in ("A Ctrl+C while any position is open (a put, its call, or any other)",
                 "in all three strategies' G1 tests", f"even after {last_entry}", f"held until the {eod} close",
                 "MISMATCH or UNKNOWN_POSITION halt", "stops new entries for all three",
                 f"left more than {gap_s / 60:.0f} minutes of entry time, it marks the day IMPAIRED for all three",
                 "for `opening_drive` if that strategy traded the same put",
                 f"more than {frac:.0%} of the days it traded"):
        assert fact in laptop8, f"LAPTOP.md §8 must say {fact!r}"
    for fact in ("while any structure is open, its PE or CE placebo included", "`Engine._operator_impaired`",
                 "until EOD_FLATTEN", "`ACCOUNT:PAPER` with ACK", f"G1_IMPAIRED_GAP_S ({gap_s:.0f} s)",
                 "`g1.count_mismatches`", f"exceed {frac:.0%} of the sample's sessions"):
        assert fact in docs["TITAN_INSTRUCTIONS.md §11"], f"TITAN_INSTRUCTIONS.md §11 must say {fact!r}"
    assert ("with anything open (any book's, placebos included) it marks every PAPER/LIVE strategy's session "
            "IMPAIRED at once, until EOD_FLATTEN") in docs["TITAN_INSTRUCTIONS.md §14"]
    for fact in (f"session IMPAIRED until {eod}", f"even after {last_entry}", f"after {gap_s:.0f} s of the entry"):
        assert fact in docs["CHANGELOG.md"], f"the CHANGELOG entry must say {fact!r}"


def test_docs_price_spike_down_shares_no_cooldown_with_price_spike_up() -> None:
    """Review, 2026-10-12: each spike strategy has its own per-stock cooldown (v1 had one per stock), so a stock can
    get price_spike_up's CE and price_spike_down's PE seconds apart or on one tick (tests/unit/test_strategies.py,
    tests/scenarios/test_price_spike_down.py). The docs say so, with the code's cooldown."""
    cd = PriceSpikeUp.PARAMS["cooldown_s"]
    assert cd == PriceSpikeDown.PARAMS["cooldown_s"] == 90
    docs = _psd_docs()
    for fact in (f"v1 paused a stock for {cd} seconds after any spike, up or down",
                 f"each spike strategy keeps its own {cd}-second pause", "rarely on the very same tick",
                 "up to four positions on one stock", "not exactly the trades v1's down-spikes took"):
        assert fact in docs["LAPTOP.md §8"], f"LAPTOP.md §8 must say {fact!r}"
    for fact in (f"Each spike strategy keeps its own {cd} s per-stock cooldown", "up to four structures on one "
                 "underlying", "same tick", "does not sample v1's PRICE_SPIKE_DOWN trades"):
        assert fact in docs["TITAN_INSTRUCTIONS.md §11"], f"TITAN_INSTRUCTIONS.md §11 must say {fact!r}"
    assert f"keep separate {cd} s cooldowns where v1 had one per stock" in docs["CHANGELOG.md"]


def test_docs_price_spike_down_volume_rule() -> None:
    """Review, 2026-10-12: the volume rule only skips a trigger tick that traded under vol_confirm_mult (half) of the
    recent average; volume need not be rising (tests/unit/test_strategies.py pins a fall on shrinking volume)."""
    assert PriceSpikeDown.PARAMS["vol_confirm_mult"] == PriceSpikeUp.PARAMS["vol_confirm_mult"] == 0.5
    docs = _psd_docs()
    assert "A fall on an unusually quiet tick (less than half the recent average volume per tick) is ignored" in (
        docs["LAPTOP.md §8"])
    assert "on a trigger tick with at least half the recent average volume" in docs["CHANGELOG.md"]
    for where, text in docs.items():
        assert "rising volume" not in text, f"{where}: the rule never asks for rising volume"


def test_docs_price_spike_down_g1_family_size() -> None:
    """Review, 2026-10-12: G1's family size m = max(trials ever registered, G1_FAMILY_FLOOR) is shared by every
    strategy, so the docs quote what the third strategy costs the others, computed here from titan2.g1: one version
    each keeps m at the floor, a change that re-versions every strategy now registers len(REGISTRY) trials, and from
    its third version on each price_spike_down version raises m on its own.

    Review, 2026-10-13: LAPTOP.md §8 quotes the current figure instead. That day the risk caps and then
    MAX_ENTRIES_PER_DAY 3 each gave every strategy a new version, so the trials are the distinct (strategy, version)
    pairs of the three settings those versions ran with, computed here with strategies.version."""

    class Trials:                                          # the only Store method family_m reads
        def __init__(self, n: int) -> None:
            self.n = n

        def trial_count(self) -> int:
            return self.n

    def g1_at(trials: int) -> tuple[int, float, int]:
        m = g1.family_m(Trials(trials))
        return m, g1.alpha_look(m), g1.boot_size(g1.alpha_look(m))

    def exact(alpha: float) -> str:                       # 0.3125%, 0.2083%, 0.25%
        return f"{alpha * 100:.4f}".rstrip("0") + "%"

    def about(alpha: float) -> str:                       # 0.31%, 0.21%, 0.25%, 0.14%
        return f"{alpha * 100:.2f}%"

    n = len(strategies.REGISTRY)
    (m1, a1, b1), (m3, a3, _), (m2, a2, b2) = g1_at(n), g1_at(n + 2), g1_at(2 * n)
    assert (n, m1, m3, m2) == (3, 4, 5, 6)
    # the trials so far: the settings of 2026-10-13 (before the risk caps, with them, and with three entries a day,
    #   today's DEFAULTS) with engine semantics 1 and paper model 1, then today's DEFAULTS with the release of
    #   2026-10-14 (engine semantics 2 and paper model 2 together: one new version each)
    caps = replace(settings_mod.DEFAULTS, MAX_ENTRIES_PER_DAY=2)
    before = replace(caps, MAX_LOSS_PER_TRADE_RS=5000.0, MAX_PREMIUM_PER_TRADE_RS=25000.0, DAILY_STOP_RS=10000.0,
                     MAX_WORST_CASE_DAY_LOSS_RS=40000.0)
    ran = {(name, strategies.version(cls, Before1014(s))) for name, cls in strategies.REGISTRY.items()
           for s in (before, caps, settings_mod.DEFAULTS)}
    now = {name: strategies.version(cls, settings_mod.DEFAULTS) for name, cls in strategies.REGISTRY.items()}
    # pinned: the (strategy, version) pairs registered as trials (the 9 the CHANGELOG gave on 2026-10-12/13 and
    #   today's 3). A later versioned default or version bump re-versions every strategy again (15 trials) while
    #   this count stayed 12, and LAPTOP's '12 trials' would go stale unnoticed; changing a version means updating
    #   these, the docs and the m
    assert ran == TRIALS_BEFORE_1014 and now == RELEASE_1014_VERSIONS
    trials = len(ran | set(now.items()))
    m_now, a_now, _ = g1_at(trials)
    assert (trials, m_now) == (4 * n, 12)
    docs = _psd_docs()
    for fact in (f"With one version of each strategy ({n} trials) m stays {m1}: α per look {exact(a1)}, bootstrap "
                 f"B {b1:,}", f"now registers {n} trials instead of {n - 1}: the first gives m = {m2} "
                 f"(α {exact(a2)}, B {b2:,})", f"two bumps: {n + 2} trials, m = {m3}, α {exact(a3)}"):
        assert fact in docs["TITAN_INSTRUCTIONS.md §11"], f"TITAN_INSTRUCTIONS.md §11 must say {fact!r}"
    laptop8 = docs["LAPTOP.md §8"]
    for fact in ("adds three trials instead of two", "a new `price_spike_down` version alone adds one",
                 f"there are {trials} trials and the chance is about {about(a_now)} per look, down from about "
                 f"{about(a1)} with {n}"):
        assert fact in laptop8, f"LAPTOP.md §8 must say {fact!r}"
    for old in (about(a2), about(a3), about(g1_at(3 * n)[1])):   # what it quoted before 2026-10-13, and on 10-13
        assert old not in laptop8, f"LAPTOP.md §8 still quotes {old}, a G1 figure from before 2026-10-14"
    assert (f"registers {n} trials instead of {n - 1} (the first gives m = {m2}, α per look {exact(a2)} instead of "
            f"{exact(a1)})") in docs["CHANGELOG.md"]


class Before1014:
    """A Settings stand-in whose version inputs say engine semantics 1 and paper model 1: what strategies.version
    hashed until the release of 2026-10-14 (version_inputs reads only version_basis())."""

    def __init__(self, s: settings_mod.Settings) -> None:
        self.s = s

    def version_basis(self) -> dict:
        return {**self.s.version_basis(), "engine": 1, "paper_model": 1}


TRIALS_BEFORE_1014 = {
    ("opening_drive", "1.03a618af25"), ("opening_drive", "1.8acb18e334"), ("opening_drive", "1.54ba54e096"),
    ("price_spike_up", "1.a2a5683926"), ("price_spike_up", "1.ffdfbbebc8"), ("price_spike_up", "1.1882405ca0"),
    ("price_spike_down", "1.1199b091c0"), ("price_spike_down", "1.ef56e0791a"), ("price_spike_down", "1.6d19c4137a"),
}
# the versions that ran last before the release, and the release's (engine semantics 2 with paper model 2)
LAST_BEFORE_1014 = {"opening_drive": "1.54ba54e096", "price_spike_up": "1.1882405ca0",
                    "price_spike_down": "1.6d19c4137a"}
RELEASE_1014_VERSIONS = {"opening_drive": "1.b230b83102", "price_spike_up": "1.3a030f3e26",
                         "price_spike_down": "1.9f039e44a7"}
PAPER_MODEL_V2_CHANGELOG = "Paper model v2 (2026-10-14)"
WAIT_CHANGELOG = "Signals wait for unfinished orders (2026-10-14)"


def test_docs_paper_model_v2() -> None:
    """Review round 2 (2026-10-14): the paper model's recalibration is recorded with the code's facts. The CHANGELOG
    entry gives every strategy's old and new version, G1's family size before and after (computed with titan2.g1)
    and the cancel reasons the paper exchange now gives; TITAN_INSTRUCTIONS §4 names those reasons too, and the
    errata line in the spec gives the version.

    The review of paper model 2 (the same day): the keys that only calibrate the paper exchange are fixed at their
    defaults (the CHANGELOG and the errata list them with the code's values, and say a lower or higher value is no
    tightening), the decision time travels with the order, the square-off takes a crossed book's worse side, and
    every doc says a lone one-lot level fills with probability = participation, not always as on LIVE."""
    from titan2.broker import paper

    assert settings_mod.PAPER_MODEL_VERSION == 2
    reasons = (paper.MSG_NO_DEPTH, paper.MSG_CROSSED_BOOK, paper.MSG_OLDER_BOOK)
    assert reasons == ("SIM: no fresh depth", "SIM: crossed or locked book", "SIM: book older than the decision")

    class Trials:                                          # the only Store method family_m reads
        def __init__(self, k: int) -> None:
            self.k = k

        def trial_count(self) -> int:
            return self.k

    m_old, m_new = g1.family_m(Trials(9)), g1.family_m(Trials(12))       # three new trials (test above)
    a_old, a_new = g1.alpha_look(m_old), g1.alpha_look(m_new)
    entry = _flat(_section(_read(CHANGELOG), PAPER_MODEL_V2_CHANGELOG))
    for name, old in LAST_BEFORE_1014.items():             # one new version with the release's engine change
        new = RELEASE_1014_VERSIONS[name]
        assert f"`{name}` {old} → {new}" in entry, f"the CHANGELOG must give {name}'s versions"
    for fact in (f"m goes from {m_old} to {m_new}", f"α per look {a_new * 100:.4f}% instead of {a_old * 100:.4f}%",
                 f"bootstrap B {g1.boot_size(a_new):,} instead of {g1.boot_size(a_old):,}",
                 "count only for the old versions' trials", "`PAPER_MODEL_VERSION` goes from 1 to 2",
                 *(f"`{r}`" for r in reasons[1:])):
        assert fact in entry, f"the CHANGELOG entry must say {fact!r}"
    paper_fills = _flat(_section(_read(INSTRUCTIONS), "4. Architecture"))
    assert all(f"`{r}`" in paper_fills for r in reasons), "TITAN_INSTRUCTIONS.md §4 must name the SIM reasons"
    fixed = {k: lim.default for k, lim in settings_mod.LIMITS.items() if lim.fixed}
    assert fixed == {"PAPER_PARTICIPATION": 0.5, "PAPER_LATENCY_MEDIAN_MS": 250.0, "PAPER_LATENCY_SIGMA": 0.6,
                     "PAPER_READ_LATENCY_MEDIAN_MS": 250.0, "PAPER_READ_LATENCY_SIGMA": 0.85,
                     "PAPER_ADVERSE_TICKS": 0, "PAPER_PRICE_BAND_PCT": 20.0, "PAPER_BOOK_MAX_AGE_S": 1.0}
    assert all(settings_mod.LIMITS[k].lo == settings_mod.LIMITS[k].hi == v for k, v in fixed.items())
    named = [f"`{k}` {_fmt(v)}" for k, v in fixed.items()]
    for fact in ("fills with probability `PAPER_PARTICIPATION`, about half the time", "not always as on LIVE",
                 "`OrderRequest.decision_exch_wall`", "`titan2 settings unset KEY`", *named):
        assert fact in entry, f"the CHANGELOG entry must say {fact!r}"
    assert "fills part of the time, as on the exchange" not in entry, "a lone lot is no longer claimed fixed"
    errata = next(line for line in _read(ROOT / "docs" / "spec" / "FINAL_SPEC.md").splitlines()
                  if line.startswith("- **Paper model v2"))
    for fact in ("`PAPER_MODEL_VERSION` 2", "m = 12", "`SIM: crossed or locked book`",
                 "`SIM: book older than the decision`", "probability = participation", "not \"always, as LIVE\"",
                 "`OrderRequest.decision_exch_wall`", "never at the no-book ₹0.05", *named):
        assert fact in errata, f"the spec's errata line must say {fact!r}"
    for claim in ("never better than LIVE", "allowed ≥250 / ≥0.6", "recorded at `place()`"):
        assert claim not in errata, f"the spec's errata line still says {claim!r}"
    assert "fills with probability 0.5 (`PAPER_PARTICIPATION`), about half the time" in paper_fills
    assert "a lone one-lot bid fills on paper only about half the time" in _flat(
        _section(_read(LAPTOP), "8. What PAPER means"))


def test_docs_signals_wait_for_unfinished_orders() -> None:
    """2026-10-14: a G09 refusal due only to pending structures waits, one REST quote retry, and the release's engine
    version (ENGINE_SEMANTICS_VERSION 2 with paper model 2: one new version each). The CHANGELOG entry gives the 09:20
    case, the versions and G1's m (computed with titan2.g1); the spec's errata, DETAIL, TITAN_INSTRUCTIONS §6 and the
    RUNBOOK's RUNAWAY row say what the code does (tests/unit/test_wait_pending.py)."""
    from titan2.broker import kite
    from titan2.core import gateway
    from titan2.strategies import base

    assert settings_mod.ENGINE_SEMANTICS_VERSION == 2 and gateway.WAIT == "WAIT"
    assert base.read_lock(strategies.LOCK_PATH)["_engine"]["engine_semantics_version"] == 2

    class Trials:                                          # the only Store method family_m reads
        def trial_count(self) -> int:
            return 12

    m = g1.family_m(Trials())
    a = g1.alpha_look(m)
    entry = _flat(_section(_read(CHANGELOG), WAIT_CHANGELOG))
    for name, old in LAST_BEFORE_1014.items():
        new = RELEASE_1014_VERSIONS[name]
        assert f"`{name}` {old} → {new}" in entry, f"the CHANGELOG must give {name}'s versions"
    for fact in (f"m = {m} (α per look {a * 100:.4f}%, bootstrap B {g1.boot_size(a):,})",
                 "`ENGINE_SEMANTICS_VERSION` goes from 1 to 2", "`POSITIONS: 2/2`", "09:20:00.212", "09:20:02.192",
                 "09:20:02.248", "TCS waits 2.038 s", "`INTENT_WAITING`", "`waited_s`", "`titan2 ack-halt`",
                 "never raises the RUNAWAY halt", f"`{kite.RETRIED_NOTE.strip()}`",
                 "the paper exchange's own depth reads"):
        assert fact in entry, f"the CHANGELOG entry must say {fact!r}"
    errata = {line.split(":**")[0]: line for line in _read(ROOT / "docs" / "spec" / "FINAL_SPEC.md").splitlines()
              if line.startswith(("- **Waiting for pending entries", "- **One REST quote retry",
                                  "- **Engine semantics 2"))}
    assert len(errata) == 3, f"the spec needs its three errata lines: {sorted(errata)}"
    wait, retry, version = (next(v for k, v in errata.items() if k.startswith(p))
                            for p in ("- **Waiting", "- **One REST", "- **Engine"))
    for fact in ("`INTENT_WAITING{book, gate, detail, pending, ttl_s}`", "`waited_s` and `waited_for`",
                 "raises no `HALT(RUNAWAY)`", "deferring the one attempt is not a retry", "never waits on its own",
                 "`ENTRY_ORDERS: <n> in 60 s + <r> reserved/<MAX_ENTRY_ORDERS_PER_MIN>`"):
        assert fact in wait, f"the errata line on waiting must say {fact!r}"
    assert kite.DROPPED in retry and f"`{kite.RETRIED_NOTE.strip()}`" in retry
    # the review of the retry: it names exactly what is retried, and says a timeout while sending is not
    assert all(f"`{c.__name__}`" in retry for c in kite.CLOSED_BEFORE_ANSWER)
    assert "while the request is being sent" in retry
    for name, new in RELEASE_1014_VERSIONS.items():
        assert f"`{name}` {LAST_BEFORE_1014[name]} → {new}" in version, f"the errata must give {name}'s versions"
    assert "`ENGINE_SEMANTICS_VERSION` 2" in version and f"m = {m}" in version
    detail = _read(ROOT / "docs" / "spec" / "FINAL_SPEC_DETAIL.md")
    assert "INTENT_WAITING{book, gate, detail, pending, ttl_s}" in detail and "`ENGINE_SEMANTICS_VERSION=2`" in detail
    how = _flat(_section(_read(INSTRUCTIONS), "6. How a trade happens"))
    for fact in ("the intent waits instead, holding nothing, for at most its TTL", "INTENT_WAITING",
                 "waited_s", "refused without the halt", "is made once more at once"):
        assert fact in how, f"TITAN_INSTRUCTIONS.md §6 must say {fact!r}"
    (row,) = [r for r in _table_rows(_section(_read(RUNBOOK), "6. Halts")) if r and r[0] == "`RUNAWAY`"]
    assert "waited_s" in row[-1] and "without the halt" in row[-1] and "`ENTRY_ORDERS: … reserved/3`" in row[-1], row


# ---------------------------------------------------------------------------------------------------------------
# Self-tests of the helpers (cheap, keep the checks above honest)
# ---------------------------------------------------------------------------------------------------------------
def test_helpers_slug_links_and_tables() -> None:
    assert _slug("10. [V] verification checklist and results") == "10-v-verification-checklist-and-results"
    assert _slug("P2. MISMATCH or EXTERNAL_CLOSE") == "p2-mismatch-or-external_close"
    assert _slug("8. Risk, sizing and settings") == "8-risk-sizing-and-settings"
    md = "# A\n## B b\n```\n## not a heading\n```\n## B b\n"
    assert _anchors(md) == {"a", "b-b", "b-b-1"}
    assert [m.group(1) for m in _LINK.finditer("see [x](docs/RUNBOOK.md#p1) and ![i](a.png)")] == \
        ["docs/RUNBOOK.md#p1", "a.png"]
    assert _table_rows("| a | b |\n|---|:-:|\n| `c` | d |") == [["a", "b"], ["`c`", "d"]]
    assert _REPO_PATH.fullmatch("titan2/core/exits.py") and not _REPO_PATH.fullmatch("tests/unit/test_<m>.py")
    assert _fmt(25000.0) == "25,000" and _fmt(1.5) == "1.5" and _fmt(dtime(9, 20)) == "09:20" and _fmt(False) == "false"
