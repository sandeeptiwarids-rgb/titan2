"""Capture the golden fixtures from v1-final (commit b4694f3). Run by hand from agentic_trader/; pytest never runs it.

    python tests/golden/capture_v1.py                  # v1 source through read-only `git show b4694f3:...`
    python tests/golden/capture_v1.py --rev v1-final   # the same commit, once the tag exists (spec §15)
    python tests/golden/capture_v1.py --v1-root DIR    # DIR = agentic_trader/ of a v1-final worktree
    python tests/golden/capture_v1.py --check          # recapture and compare with tests/golden/fixtures/

No v1 module is imported. Each v1 class, method or inline block is cut out of the v1 source by its AST and run on
the scenarios below, with a fake clock and the literal config values of the same v1 tree. So v1's heavy imports
(kiteconnect, scipy, state_db, thesis_validator) never load, and nothing reaches a network, a broker or a state file.
The capture refuses any source whose files differ from the pinned v1-final blobs.

Each fixture holds the scenario inputs, v1's outputs, and the scenario author's expectation for titan2 relative to
v1: "SAME", or the id of an intended difference documented in test_golden.INTENDED_DIFFERENCES.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

V1_REV = "b4694f3"                 # the commit the v1-final tag points at (spec §15)
V1_FINAL_BLOBS = {                 # git blob ids at b4694f3; test_golden.py pins the same values
    "options_trader.py": "45eab1b6eb822a0c6da67c300fa44b093f0c3143",
    "zerodha_tools.py": "91d327b195c9d08ed68bbb0604abf8e2edfbf12b",
    "dashboard.py": "811ef481ce7b38532ebfa2ae89458cc485ca9e84",
    "exit_manager.py": "034e34964817bc5b3475ec82e5423d97755e4ec9",
    "kite_ticker.py": "f9280cae71b2b4bde71d9576318a2d6be9812996",
    "config.py": "606a4ed7d4bc916f4fcf326e3a6a0bd0cf2e9043",
    "settings_manager.py": "cb031250fad783934f82da0f1086cf7be82c46a0",
    "titan_settings.json": "634ff2dbb06f7afe160f473212811af739e545c3",
}
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPO = HERE.parents[2]             # tests/golden -> tests -> agentic_trader -> the repository root
IST = timezone(timedelta(hours=5, minutes=30), "IST")
SESSION = date(2026, 10, 12)       # a Monday; v1 code sees naive IST wall times on it, as on the EC2 host
FORMAT = 1
SAME = "SAME"


class CaptureError(RuntimeError):
    """The v1 source is not the code these scenarios were written against."""


def _silent(*_args: Any, **_kwargs: Any) -> None:
    """Stands in for print() inside v1 code."""


# ---------------------------------------------------------------------------------------------------------------
# Reading v1
# ---------------------------------------------------------------------------------------------------------------
class V1Source:
    """v1's agentic_trader files at a git revision (read-only `git show`) or in a directory such as a worktree."""

    def __init__(self, rev: str | None = V1_REV, root: str | Path | None = None) -> None:
        self.rev = rev
        self.root = Path(root) if root is not None else None
        self._cache: dict[str, tuple[str, str]] = {}

    def read(self, name: str) -> tuple[str, str]:
        """(text, git blob id) of one file; raises CaptureError unless it is the v1-final file."""
        if name not in self._cache:
            if self.root is not None:
                data = (self.root / name).read_bytes().replace(b"\r\n", b"\n")   # what git stores (core.autocrlf)
            else:
                done = subprocess.run(["git", "-C", str(REPO), "show", f"{self.rev}:agentic_trader/{name}"],
                                      capture_output=True, check=False)
                if done.returncode != 0:
                    raise CaptureError(f"git show {self.rev}:agentic_trader/{name} failed: "
                                       f"{done.stderr.decode(errors='replace').strip()}")
                data = done.stdout
            blob = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
            if blob != V1_FINAL_BLOBS[name]:
                raise CaptureError(f"{name} is blob {blob}, not the v1-final blob {V1_FINAL_BLOBS[name]}")
            self._cache[name] = (data.decode("utf-8-sig"), blob)
        return self._cache[name]


class V1File:
    """One parsed v1 Python file. Cuts classes, methods and inline blocks out by AST and records their line spans."""

    def __init__(self, source: V1Source, name: str) -> None:
        self.name = name
        self.text, self.blob = source.read(name)
        self.lines = self.text.splitlines()
        self.tree = ast.parse(self.text)
        self.spans: dict[str, list[int]] = {}

    def _node(self, dotted: str) -> Any:
        node: Any = self.tree
        for part in dotted.split("."):
            hits = [n for n in node.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == part]
            if len(hits) != 1:
                raise CaptureError(f"{self.name}: expected one {dotted}, found {len(hits)}")
            node = hits[0]
        return node

    def _cut(self, key: str, start: int, end: int) -> str:
        self.spans[key] = [start, end]
        return textwrap.dedent("\n".join(self.lines[start - 1:end])) + "\n"

    def source(self, dotted: str) -> str:
        """A class or function, decorators included, dedented to column 0."""
        node = self._node(dotted)
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        return self._cut(dotted, start, node.end_lineno)

    def block(self, func: str, first: str, last_test: str) -> str:
        """The statements of func from the one whose source is `first` through the next `if` whose test contains
        `last_test`: an inline v1 block, such as a depth walk inside a long method."""
        for parent in ast.walk(self._node(func)):
            for attr in ("body", "orelse", "finalbody"):
                stmts = getattr(parent, attr, None)
                if not isinstance(stmts, list):
                    continue
                for i, stmt in enumerate(stmts):
                    if ast.get_source_segment(self.text, stmt) != first:
                        continue
                    for last in stmts[i:]:
                        if isinstance(last, ast.If) and last_test in ast.get_source_segment(self.text, last.test):
                            return self._cut(f"{func}:block", stmt.lineno, last.end_lineno)
        raise CaptureError(f"{self.name}: no block {first!r} .. if {last_test!r} in {func}")

    def literal(self, name: str) -> Any:
        """The value of a module-level `NAME = <literal>`."""
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [name]:
                self.spans[name] = [node.lineno, node.end_lineno]
                return ast.literal_eval(node.value)
        raise CaptureError(f"{self.name}: no module-level literal {name}")

    def init_literals(self, cls: str) -> dict[str, Any]:
        """{attr: value} for every `self.attr = <literal>` in cls.__init__ (locks, paths and calls are skipped)."""
        init = self._node(f"{cls}.__init__")
        out: dict[str, Any] = {}
        for node in ast.walk(init):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute)
                    and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id == "self"):
                try:
                    out[node.targets[0].attr] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
        self.spans[f"{cls}.__init__"] = [init.lineno, init.end_lineno]
        return out

    def provenance(self) -> dict:
        return {"blob": self.blob, "spans": dict(sorted(self.spans.items()))}


def _run(code: str, ns: dict, label: str) -> None:
    # dont_inherit: v1 code must not pick up this file's `from __future__ import annotations` (v1 never used it)
    exec(compile(code, f"<v1 {label}>", "exec", dont_inherit=True), ns)


def _block_function(block: str, name: str, params: tuple[str, ...]) -> Callable[..., dict]:
    """Wrap an inline v1 block in a function that returns the block's local variables."""
    src = f"def {name}({', '.join(params)}):\n{textwrap.indent(block, '    ')}    return dict(locals())\n"
    ns: dict = {"__name__": f"v1_{name}"}
    _run(src, ns, name)
    return ns[name]


@contextlib.contextmanager
def _module_stub(name: str, attrs: dict) -> Iterator[None]:
    """sys.modules[name] holds attrs for the duration (v1's `from config import ...`); the old entry is restored."""
    old = sys.modules.get(name)
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    try:
        yield
    finally:
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


def _origin(files: list[V1File], extra: dict | None = None) -> dict:
    out = {"rev": V1_REV, "tag": "v1-final", "files": {f.name: f.provenance() for f in files}}
    out["files"].update(extra or {})
    return out


# ---------------------------------------------------------------------------------------------------------------
# 1. Strike selection: options_trader.py OptionChainFetcher.select_strike -> titan2 instruments.pick_strike
# ---------------------------------------------------------------------------------------------------------------
STRIKE_CHAINS = {
    "STOCK_STEP50": [2300.0 + 50.0 * i for i in range(9)],                     # 2300 .. 2700
    "NIFTY_STEP50": [24800.0 + 50.0 * i for i in range(9)],                    # 24800 .. 25200
    "STOCK_MIXED_STEP": [90.0, 92.5, 95.0, 97.5, 100.0, 105.0, 110.0, 115.0, 120.0],   # the step widens at 100
}
EDGE, OUTSIDE, TIE = "D_ITM1_EDGE_REFUSED", "D_SPOT_OUTSIDE_REFUSED", "D_ATM_TIE_PE_MORE_ITM"
STRIKE_COMBOS = (("CE", "ATM"), ("CE", "ITM1"), ("PE", "ATM"), ("PE", "ITM1"))
S4 = (SAME,) * 4
STRIKE_SPOTS = (                   # chain, spot, expectation for each of STRIKE_COMBOS
    ("STOCK_STEP50", 2463.40, S4),
    ("STOCK_STEP50", 2512.00, S4),
    ("STOCK_STEP50", 2549.90, S4),
    ("STOCK_STEP50", 2401.00, S4),
    ("STOCK_STEP50", 2624.95, S4),
    ("STOCK_STEP50", 2475.00, (SAME, SAME, TIE, TIE)),        # midway between 2450 and 2500
    ("STOCK_STEP50", 2310.00, (SAME, EDGE, SAME, SAME)),      # ATM 2300 is the lowest strike: no CE ITM1
    ("STOCK_STEP50", 2290.00, (SAME, EDGE, SAME, SAME)),      # below the lowest strike, within half a step
    ("STOCK_STEP50", 2695.00, (SAME, SAME, SAME, EDGE)),      # ATM 2700 is the highest strike: no PE ITM1
    ("STOCK_STEP50", 2712.00, (SAME, SAME, SAME, EDGE)),
    ("STOCK_STEP50", 2270.00, (OUTSIDE,) * 4),                # 30 below the lowest strike (step 50)
    ("STOCK_STEP50", 2740.00, (OUTSIDE,) * 4),
    ("NIFTY_STEP50", 25012.35, S4),
    ("NIFTY_STEP50", 24987.65, S4),
    ("NIFTY_STEP50", 25025.00, (SAME, SAME, TIE, TIE)),
    ("NIFTY_STEP50", 24815.00, (SAME, EDGE, SAME, SAME)),
    ("NIFTY_STEP50", 25190.00, (SAME, SAME, SAME, EDGE)),
    ("STOCK_MIXED_STEP", 99.10, S4),
    ("STOCK_MIXED_STEP", 101.20, S4),
    ("STOCK_MIXED_STEP", 103.00, S4),
    ("STOCK_MIXED_STEP", 102.50, (SAME, SAME, TIE, TIE)),     # midway between 100 and 105
    ("STOCK_MIXED_STEP", 96.25, (SAME, SAME, TIE, TIE)),      # midway between 95 and 97.5
    ("STOCK_MIXED_STEP", 91.00, (SAME, EDGE, SAME, SAME)),
    ("STOCK_MIXED_STEP", 124.00, (OUTSIDE,) * 4),             # 4 above the highest strike (last step 5)
)


def capture_select_strike(src: V1Source) -> dict:
    f = V1File(src, "options_trader.py")
    ns: dict = {"__name__": "v1_options_trader", "Enum": Enum, "dataclass": dataclass, "field": field,
                "List": List, "Dict": Dict, "Optional": Optional, "Tuple": Tuple, "Any": Any,
                "datetime": datetime, "date": date}
    for name in ("OptionType", "StrikeSelection", "OptionContract", "OptionChain", "OptionChainFetcher.select_strike"):
        _run(f.source(name), ns, name)
    opt_type, sel, contract, chain_cls, select = (
        ns[k] for k in ("OptionType", "StrikeSelection", "OptionContract", "OptionChain", "select_strike"))
    expiry = date(2026, 10, 27)
    cases = []
    for chain_id, spot, expects in STRIKE_SPOTS:
        contracts = [contract(symbol=f"NFO:{chain_id}{k:g}{t}", underlying=f"NSE:{chain_id}", strike=k,
                              option_type=opt_type[t], expiry=expiry, lot_size=1, ltp=0.0, bid=0.0, ask=0.0,
                              volume=0, oi=0, iv=0.0)
                     for k in STRIKE_CHAINS[chain_id] for t in ("CE", "PE")]
        chain = chain_cls(underlying=f"NSE:{chain_id}", spot_price=spot, expiries=[expiry], contracts=contracts,
                          timestamp=datetime.combine(SESSION, dtime(10, 0)))
        for (t, pref), expect in zip(STRIKE_COMBOS, expects, strict=True):
            atm = select(None, chain, opt_type[t], sel.ATM)
            got = select(None, chain, opt_type[t], sel.ATM if pref == "ATM" else sel.ITM_1)
            cases.append({"id": f"{chain_id}@{spot:g}/{t}/{pref}", "chain": chain_id, "spot": spot, "opt_type": t,
                          "pref": pref, "expect": expect,
                          "v1": {"strike": None if got is None else got.strike,
                                 "atm_strike": None if atm is None else atm.strike}})
    return {"area": "select_strike", "format": FORMAT, "v1": _origin([f]),
            "driver": "v1 select_strike(chain, CE|PE, ATM|ITM_1) on a one-expiry chain built from the listed strikes",
            "chains": STRIKE_CHAINS, "cases": cases}


# ---------------------------------------------------------------------------------------------------------------
# 2. Depth walk: zerodha_tools.py _check_option_liquidity and dashboard.py pnl_live -> titan2 model.walk and
#    core.exits.realizable_bid
# ---------------------------------------------------------------------------------------------------------------
BOOK_A = ((31.70, 1200, 4), (31.65, 800, 3), (31.60, 500, 2), (31.55, 300, 1), (31.50, 900, 5))   # 3,700 shown
DEPTH_CASES = (                    # id, side crossed, shown levels, qty, ltp, expected realizable-bid relation
    ("sell_inside_best_level", "SELL", BOOK_A, 500, 31.80, SAME),
    ("sell_sweeps_three_levels", "SELL", BOOK_A, 2400, 31.80, SAME),
    ("sell_exactly_the_whole_book", "SELL", BOOK_A, 3700, 31.80, SAME),
    ("sell_book_covers_96pct", "SELL", BOOK_A, 3850, 31.80, "D_WALK_95PCT_RULE"),
    ("sell_book_covers_62pct", "SELL", BOOK_A, 6000, 31.80, "D_WALK_NO_LTP_FALLBACK"),
    ("sell_zero_padded_two_levels", "SELL", ((12.40, 450, 2), (12.35, 900, 3)), 900, 12.45, SAME),
    ("sell_empty_bid_side", "SELL", (), 450, 12.50, "D_WALK_NO_LTP_FALLBACK"),
    ("sell_four_levels_hindpetro_like", "SELL", ((8.15, 1350, 3), (8.10, 2025, 4), (8.05, 1350, 2), (8.00, 2700, 6)),
     6750, 8.20, SAME),
    ("sell_cheap_contract_deep_book", "SELL",
     ((1.15, 4300, 2), (1.10, 8600, 5), (1.05, 4300, 1), (1.00, 12900, 7), (0.95, 4300, 1)), 25800, 1.20, SAME),
    ("buy_sweeps_three_asks", "BUY", ((32.10, 300, 1), (32.15, 500, 2), (32.20, 400, 1)), 1000, 32.00, None),
    ("buy_asks_run_out", "BUY", ((32.10, 300, 1), (32.15, 500, 2)), 1000, 32.00, None),
)


def _kite_depth(levels: tuple) -> list[dict]:
    """Kite's 5-level depth: missing levels are padded with zero rows, which v1 skipped inside the walk."""
    rows = [{"price": p, "quantity": q, "orders": o} for p, q, o in levels]
    return rows + [{"price": 0, "quantity": 0, "orders": 0} for _ in range(5 - len(rows))]


def capture_depth_walk(src: V1Source) -> dict:
    zt, db = V1File(src, "zerodha_tools.py"), V1File(src, "dashboard.py")
    tools = _block_function(zt.block("ZerodhaTools._check_option_liquidity", "remaining = qty", "qty * 0.95"),
                            "tools_walk", ("book", "qty", "side", "ltp", "max_slip", "result"))
    dash = _block_function(db.block("pnl_live", "remaining = qty", "qty * 0.95"),
                           "dashboard_walk", ("book", "qty", "ltp", "realizable"))
    cases = []
    for cid, side, levels, qty, ltp, expect in DEPTH_CASES:
        book = _kite_depth(levels)
        # max_slip inf: the walk always runs to its end; the slippage verdict itself is not a golden subject
        t = tools(copy.deepcopy(book), qty, side, ltp, math.inf, {})
        if "filled" not in t:
            raise CaptureError(f"{cid}: the v1 tools block returned early")
        v1: dict = {"tools": {"filled": t["filled"], "weighted": t["weighted"], "levels_crossed": t["levels_crossed"],
                              "avg_fill": t.get("avg_fill")}}
        if side == "SELL":
            d = dash(copy.deepcopy(book), qty, ltp, ltp)        # dashboard 4248: realizable = ltp before the walk
            v1["dashboard"] = {"filled": d["filled"], "weighted": d["weighted"], "realizable": d["realizable"]}
        cases.append({"id": cid, "side": side, "book": [[r["price"], r["quantity"], r["orders"]] for r in book],
                      "qty": qty, "ltp": ltp, "expect": expect, "v1": v1})
    return {"area": "depth_walk", "format": FORMAT, "v1": _origin([zt, db]),
            "driver": "the two inline v1 walk blocks, each wrapped in a function returning its locals; tools runs "
                      "with max_slip = inf, dashboard with realizable = ltp as at line 4248",
            "cases": cases}


# ---------------------------------------------------------------------------------------------------------------
# 3. Ratchet: exit_manager.py _check_hard_sl, _apply_breakeven, _apply_trailing_stop -> titan2 core.exits.decide
# ---------------------------------------------------------------------------------------------------------------
RATCHET = {"symbol": "NFO:GOLDSTK26OCT2500CE", "entry": 100.0, "qty": 500, "sl_pct": 15.0, "start": "10:00:00",
           "step_s": 1.0}
ARM, FLOOR, ANCHOR, DEBOUNCE = "D_RATCHET_ARMS_LATER", "D_RATCHET_FLOOR", "D_RATCHET_BE_ANCHOR", "D_STOP_DEBOUNCE"
_R1_PX = (100.00, 101.00, 102.50, 103.40, 103.80, 104.00, 104.20, 104.50, 106.00, 108.00, 110.00, 112.50, 114.00,
          111.00, 110.20, 109.70, 109.60)
_R1_ZERO = (SAME, SAME, SAME, ARM, ARM, FLOOR, FLOOR, SAME, SAME, SAME, SAME, SAME, SAME, SAME, SAME, SAME, None)
_R1_REAL = (SAME, SAME, SAME, ARM, ARM, ARM, ARM, FLOOR, ANCHOR, ANCHOR, ANCHOR, ANCHOR, ANCHOR, ANCHOR, ANCHOR,
            ANCHOR, None)
RATCHET_PATHS = (
    # id, charges, [(bid = price, titan2 stop vs v1 SL after the tick; None once v1 has exited)], exit relation
    ("R1_trail_then_lock_stop", "zero", tuple(zip(_R1_PX, _R1_ZERO)), DEBOUNCE),
    ("R1_trail_then_lock_stop_real_charges", "real", tuple(zip(_R1_PX, _R1_REAL)), DEBOUNCE),
    ("R2_trail_then_gap_through_stop", "zero",
     ((100.00, SAME), (102.50, SAME), (104.50, SAME), (107.00, SAME), (110.00, SAME), (103.50, SAME)), SAME),
    ("R3_hard_stop_touched", "zero",
     ((100.00, SAME), (98.00, SAME), (95.00, SAME), (90.00, SAME), (86.00, SAME), (85.00, SAME), (84.90, None)),
     DEBOUNCE),
    ("R4_hard_stop_gap", "zero", ((100.00, SAME), (97.00, SAME), (81.50, SAME)), SAME),
)
_RATCHET_CONSTANTS = ("trailing_start_r", "trailing_run_pct", "trailing_harvest_r", "trailing_harvest_pct",
                      "breakeven_trigger_r", "vix_trail_retain_reduce")


def capture_ratchet(src: V1Source) -> dict:
    em = V1File(src, "exit_manager.py")
    ns: dict = {"__name__": "v1_exit_manager", "dataclass": dataclass, "field": field, "asdict": asdict,
                "datetime": datetime, "time": dtime, "Dict": Dict, "List": List, "Optional": Optional,
                "Tuple": Tuple, "logger": logging.getLogger("titan2.golden.v1_exit_manager"), "print": _silent}
    for name in ("ExitSignal", "TradeState", "ExitManager._check_hard_sl", "ExitManager._apply_breakeven",
                 "ExitManager._apply_trailing_stop"):
        _run(em.source(name), ns, name)
    consts = em.init_literals("ExitManager")
    missing = [k for k in _RATCHET_CONSTANTS if k not in consts]
    if missing:
        raise CaptureError(f"ExitManager.__init__ lacks {missing}")
    manager = types.SimpleNamespace(**consts, _persist_single=_silent)   # persistence only
    entry, qty = RATCHET["entry"], RATCHET["qty"]
    initial_sl = round(entry * (1 - RATCHET["sl_pct"] / 100), 2)
    paths = []
    for pid, charges, ticks, expect_exit in RATCHET_PATHS:
        st = ns["TradeState"](symbol=RATCHET["symbol"], side="BUY", entry_price=entry,
                              entry_time=datetime.combine(SESSION, dtime.fromisoformat(RATCHET["start"])),
                              initial_sl=initial_sl, current_sl=initial_sl, target=entry * 10, quantity=qty,
                              highest_price=entry, lowest_price=float("inf"), is_option=True)   # as register_trade
        sl: list[float] = []
        trailing: list[bool] = []
        exit_tick = exit_type = None
        # Ported from agentic_trader/exit_manager.py:323-337, 374-377, 416-420 @b4694f3 — changes: only the steps of
        #   update_trade that feed the ratchet (high-water mark, R multiple, hard-SL check, breakeven, trailing); the
        #   called methods are v1's own code, cut from the v1 source above
        for i, (px, _label) in enumerate(ticks):
            # ExitManager.update_trade 323-337: the R multiple and the high-water mark
            risk = abs(st.entry_price - st.initial_sl)
            if risk == 0:
                risk = st.entry_price * 0.01
            st.highest_price = max(st.highest_price, px)
            r_multiple = (px - st.entry_price) / risk
            st.max_favorable_move = max(st.max_favorable_move, r_multiple)
            signal = ns["_check_hard_sl"](manager, st, px)                  # update_trade 374-377
            if signal is None:
                ns["_apply_breakeven"](manager, st, r_multiple)             # update_trade 416-417
                ns["_apply_trailing_stop"](manager, st, px, r_multiple)    # update_trade 419-420
            sl.append(st.current_sl)
            trailing.append(st.trailing_active)
            if signal is not None:
                exit_tick, exit_type = i, signal.exit_type
                break
        paths.append({"id": pid, "charges": charges, "ticks": [list(t) for t in ticks], "expect_exit": expect_exit,
                      "v1": {"sl": sl, "trailing": trailing, "exit_tick": exit_tick, "exit_type": exit_type}})
    return {"area": "ratchet", "format": FORMAT, "v1": _origin([em]),
            "driver": "per tick, as ExitManager.update_trade 323-337, 374-377 and 416-420: high-water mark and R "
                      "multiple, _check_hard_sl, then _apply_breakeven and _apply_trailing_stop; v1's other exit "
                      "rules (greeks, thesis, target, partial profit, speed gate, time stop) are not ratchet logic "
                      "and are not run",
            **RATCHET, "initial_sl": initial_sl, "v1_constants": {k: consts[k] for k in _RATCHET_CONSTANTS},
            "paths": paths}


# ---------------------------------------------------------------------------------------------------------------
# 4. Spike detection: kite_ticker.py BreakoutWatcher -> titan2 strategies.price_spike_up.PriceSpikeUp
# ---------------------------------------------------------------------------------------------------------------
SPIKE_SYMBOL = "NSE:GOLDSPK"
_UP = {0: 1000.0, 36: 1002.0, 37: 1004.0, 38: 1006.5, 39: 1007.0, 40: 1008.0, 42: 1009.0, 45: 1008.5, 48: 1008.0}
_FAILS = {0: 1000.0, 36: 1004.0, 37: 1006.5, 38: 1009.0, 39: 1012.0, 40: 1010.0, 42: 1007.0, 45: 1005.0, 47: 1003.6}
SPIKE_SCENARIOS = (
    # id, start (IST), {second: ltp} carried forward each second, last second, dry-volume seconds, expectation
    ("S1_monotonic_up", "10:30:00", _UP, 55, (), SAME),
    ("S2_reversal_through_baseline", "10:30:00",
     {0: 1000.0, 36: 1003.0, 37: 1007.0, 38: 1005.0, 39: 1002.0, 40: 999.0, 41: 997.0, 43: 996.0}, 55, (),
     "D_SPIKE_SIGNED_SUSTAIN"),
    ("S3_spike_down", "10:30:00", {0: 1000.0, 36: 997.0, 37: 993.5, 38: 993.0, 40: 992.5, 44: 992.0}, 55, (),
     "D_SPIKE_UP_ONLY"),
    ("S4_early_market_up", "09:39:00",
     {0: 1000.0, 36: 1005.0, 37: 1011.0, 38: 1012.0, 39: 1013.0, 42: 1012.5, 46: 1012.0}, 55, (), SAME),
    ("S5_early_market_small_spike", "09:39:00",
     {0: 1000.0, 36: 1004.0, 37: 1007.0, 38: 1007.5, 41: 1006.0, 45: 1004.0}, 55, (), SAME),
    ("S6_spike_on_dry_volume", "10:30:00", {0: 1000.0, 36: 1003.0, 37: 1006.5, 38: 1003.0}, 55, (37,), SAME),
    ("S7_spike_after_window", "15:00:00", _UP, 55, (), "D_SPIKE_WINDOW"),
    ("S8_retrace_fails", "10:30:00", _FAILS, 55, (), SAME),
    ("S9_spike_inside_cooldown_after_fail", "10:30:00",
     {**_FAILS, 76: 1005.0, 77: 1010.0, 78: 1011.0, 80: 1012.0, 85: 1011.0}, 95, (), "D_SPIKE_COOLDOWN_ON_FAIL"),
    ("S10_peak_on_trigger_tick", "10:30:00", {0: 1000.0, 36: 1004.0, 37: 1012.0, 38: 1004.0}, 55, (),
     "D_SPIKE_PEAK_FROM_TRIGGER"),
    ("S11_held_rounds_up_to_bar", "09:49:00",
     {0: 1000.0, 36: 1003.0, 37: 1006.5, 38: 1008.0, 39: 1009.0, 40: 1009.6}, 55, (), "D_SPIKE_NO_ROUNDING"),
)
_EARLYBIRD = ("EARLYBIRD_COMMON", "EARLYBIRD_A", "EARLYBIRD_B", "EARLYBIRD_C", "EARLYBIRD_D")


def _spike_ticks(points: dict[int, float], last: int, dry: tuple[int, ...]) -> list[list]:
    """One tick a second: [second, ltp carried forward, cumulative volume (+1000 a tick, +100 on a dry tick)]."""
    out, px, cum = [], None, 100_000
    for t in range(last + 1):
        px = points.get(t, px)
        if t:
            cum += 100 if t in dry else 1000
        out.append([t, px, cum])
    return out


class _Clock:
    """What v1's time.time() and datetime.now() return while a scenario runs."""

    def __init__(self) -> None:
        self.wall = datetime.combine(SESSION, dtime(9, 15))   # naive IST
        self.epoch = self.wall.replace(tzinfo=IST).timestamp()

    def set(self, wall: datetime) -> None:
        self.wall, self.epoch = wall, wall.replace(tzinfo=IST).timestamp()


def _watcher_namespace(clock: _Clock, state_dir: str) -> dict:
    class V1Datetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return clock.wall if tz is None else clock.wall.replace(tzinfo=IST).astimezone(tz)

    return {"__name__": "v1_kite_ticker", "__file__": os.path.join(state_dir, "kite_ticker.py"),
            "time": types.SimpleNamespace(time=lambda: clock.epoch), "datetime": V1Datetime,
            "threading": threading, "json": json, "os": os, "defaultdict": defaultdict, "deque": deque,
            "Dict": Dict, "List": List, "Optional": Optional, "Set": Set, "print": _silent}


def _run_watcher(w: Any, clock: _Clock, start: datetime, ticks: list[list]) -> dict:
    detections, sustains, fires = [], [], []
    for i, (t, ltp, cum) in enumerate(ticks):
        clock.set(start + timedelta(seconds=t))
        before = w._pending.get(SPIKE_SYMBOL)
        passed = w._stats["sustain_passed"]
        w.process_tick(SPIKE_SYMBOL, {"last_price": ltp, "volume_traded": cum}, {})
        after = w._pending.get(SPIKE_SYMBOL)
        if before is not None and after is not before:
            sustains.append({"tick": i, "passed": w._stats["sustain_passed"] > passed,
                             "peak_pct": before.get("_peak_move_pct")})
        if after is not None and after is not before:
            detections.append({"tick": i, "type": after["trigger_type"], "baseline": after["baseline_price"],
                               "trigger_price": after["trigger_price"], "move_pct": after["move_pct"]})
        for f in w.drain_queue():
            fires.append({"tick": i, "type": f["trigger_type"], "ltp": f["ltp"], "baseline": f["baseline_price"],
                          "trigger_price": f["trigger_price"], "move_pct": f["move_pct"],
                          "held_pct": f.get("_sustain_held_pct"), "peak_pct": f.get("_peak_move_pct")})
    return {"detections": detections, "sustains": sustains, "fires": fires}


def capture_spike(src: V1Source) -> dict:
    kt, cfg, sm = V1File(src, "kite_ticker.py"), V1File(src, "config.py"), V1File(src, "settings_manager.py")
    settings_text, settings_blob = src.read("titan_settings.json")
    settings = json.loads(settings_text)
    config_py = {name: cfg.literal(name) for name in ("BREAKOUT_WATCHER",) + _EARLYBIRD}
    watcher_keys = sm.literal("_WATCHER_KEYS")
    clock = _Clock()
    scenarios, effective = [], None
    with tempfile.TemporaryDirectory() as state_dir, _module_stub("config", config_py):
        ns = _watcher_namespace(clock, state_dir)
        for name in ("PriorityTriggerQueue", "BreakoutWatcher"):
            _run(kt.source(name), ns, name)
        for sid, start, points, last, dry, expect in SPIKE_SCENARIOS:
            start_wall = datetime.combine(SESSION, dtime.fromisoformat(start))
            clock.set(start_wall)
            config = copy.deepcopy(config_py["BREAKOUT_WATCHER"])
            w = ns["BreakoutWatcher"](config)
            # settings_manager.apply_all() ran after the watcher was built (autonomous_trader 133 vs 340): the JSON
            # values land in the same dict, so they reach only what the watcher reads from it at run time
            for key in watcher_keys:
                if f"watcher_{key}" in settings:
                    config[key] = settings[f"watcher_{key}"]
            w.save_state = _silent          # persistence only: no watcher_state.json is written
            if effective is None:
                effective = {
                    "spike_pct_open": config["price_spike_pct_open"], "open_until": config["price_spike_open_until"],
                    "spike_pct": w._spike_pct, "sustain_s": w._sustain_secs,
                    "hold_pct": w._sustain_recheck_pct_spike_up, "early_end": config["early_market_end"],
                    "early_min_hold_pct": config["early_market_min_sustain_pct"],
                    "retrace_max_pct": config["sustain_retrace_max_pct"], "cooldown_s": w._cooldown_secs,
                    "json_never_applied": {"sustain_seconds": settings["watcher_sustain_seconds"],
                                           "cooldown_seconds": settings["watcher_cooldown_seconds"]}}
            ticks = _spike_ticks(points, last, dry)
            scenarios.append({"id": sid, "start": start, "expect": expect, "ticks": ticks,
                              "v1": _run_watcher(w, clock, start_wall, ticks)})
    return {"area": "spike", "format": FORMAT,
            "v1": _origin([kt, cfg, sm], {"titan_settings.json": {"blob": settings_blob, "spans": {}}}),
            "driver": "BreakoutWatcher built from config.py's BREAKOUT_WATCHER, then titan_settings.json's watcher_* "
                      "values written into the same dict (as settings_manager.apply_all did after construction); "
                      "process_tick per tick with a fake time.time()/datetime.now(); fires = drain_queue()",
            "symbol": SPIKE_SYMBOL, "session": SESSION.isoformat(), "v1_effective": effective,
            "scenarios": scenarios}


# ---------------------------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------------------------
CAPTURES: dict[str, Callable[[V1Source], dict]] = {
    "select_strike": capture_select_strike, "depth_walk": capture_depth_walk, "ratchet": capture_ratchet,
    "spike": capture_spike,
}


def dumps(obj: Any, level: int = 0) -> str:
    """JSON with each case, tick or short object on one line, so fixture diffs stay readable."""
    flat = json.dumps(obj, allow_nan=False)
    if len(flat) + 2 * level <= 118 or not isinstance(obj, (dict, list)) or not obj:
        return flat
    pad, inner = "  " * level, "  " * (level + 1)
    if isinstance(obj, dict):
        body = ",\n".join(f"{inner}{json.dumps(k)}: {dumps(v, level + 1)}" for k, v in obj.items())
        return "{\n" + body + "\n" + pad + "}"
    return "[\n" + ",\n".join(inner + dumps(x, level + 1) for x in obj) + "\n" + pad + "]"


def capture_all(src: V1Source) -> dict[str, str]:
    """{area: fixture JSON text}."""
    return {area: dumps(fn(src)) + "\n" for area, fn in CAPTURES.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture the titan2 golden fixtures from v1-final.")
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--rev", default=V1_REV, help="git revision holding v1 (default %(default)s, tag v1-final)")
    where.add_argument("--v1-root", help="a directory holding v1's agentic_trader files, e.g. a v1-final worktree")
    ap.add_argument("--check", action="store_true", help="compare with the committed fixtures and write nothing")
    args = ap.parse_args(argv)
    texts = capture_all(V1Source(rev=args.rev, root=args.v1_root))
    if args.check:
        stale = []
        for area, text in texts.items():
            path = FIXTURES / f"{area}.json"
            if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != json.loads(text):
                stale.append(area)
        print("fixtures match v1-final" if not stale else f"stale fixtures: {', '.join(stale)}")
        return 1 if stale else 0
    FIXTURES.mkdir(exist_ok=True)
    for area, text in texts.items():
        (FIXTURES / f"{area}.json").write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {FIXTURES / (area + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
