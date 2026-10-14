"""The dual run (spec §16.1, R4): every test in tests/scenarios runs once on PAPER and once on LIVE.

A test takes the `dual` fixture and builds its runners with dual.runner(...). After the test body, the fixture
records the runners' traces (each runner's normalized broker commands and booked trades, see
ScenarioRunner.trace()). The LIVE run then asserts that its trace equals the PAPER run's: the same commands at the
same times (only the account and tag letters differ) and the same P&L. If the PAPER run is not in this session (a
LIVE-only selection), the test body is run once more on PAPER to get it.

A scenario whose behaviour legitimately differs by account (spec §9.6 treats earlier-session leftovers per account;
the WS back-fill exists only for LIVE) calls dual.diverges(reason) and asserts each account's own outcome.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from titan2.model import Account

from fakes.runner import ScenarioRunner, first_diff

_TRACES: dict[str, dict[str, Any]] = {}


class Dual:
    def __init__(self, acct: str, base: Path) -> None:
        self.acct = Account(acct)
        self.base = base
        self.runners: list[ScenarioRunner] = []
        self.divergence: str | None = None

    @property
    def live(self) -> bool:
        return self.acct is Account.LIVE

    def runner(self, **kw: Any) -> ScenarioRunner:
        r = ScenarioRunner(self.base / f"run{len(self.runners)}", self.acct, **kw)
        self.runners.append(r)
        return r

    def diverges(self, reason: str) -> None:
        """This scenario's PAPER and LIVE runs differ by design (reason); parity is not asserted."""
        self.divergence = reason

    def trace(self) -> dict[str, Any]:
        return {"runs": [r.trace() for r in self.runners], "divergence": self.divergence}

    def close(self) -> None:
        for r in self.runners:
            r.close()


def assert_same_path(paper: dict[str, Any], live: dict[str, Any]) -> None:
    assert len(paper["runs"]) == len(live["runs"]), "the PAPER and LIVE runs built a different number of runners"
    for i, (p, lv) in enumerate(zip(paper["runs"], live["runs"])):
        assert p["commands"] == lv["commands"], f"run {i}: broker commands differ: " + first_diff(p["commands"],
                                                                                                 lv["commands"])
        assert p["trades"] == lv["trades"], f"run {i}: booked trades differ: " + first_diff(p["trades"], lv["trades"])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item._scenario_call_passed = rep.passed


@pytest.fixture(params=["PAPER", "LIVE"])
def dual(request, tmp_path_factory):
    d = Dual(request.param, tmp_path_factory.mktemp(request.node.name[:40].replace("[", "_").replace("]", "")))
    yield d
    try:
        if not getattr(request.node, "_scenario_call_passed", False):
            return                                    # the body already failed: no parity noise on top
        tr = d.trace()
    finally:
        d.close()
    key = f"{request.node.module.__name__}::{request.node.originalname}"
    if d.acct is Account.PAPER:
        _TRACES[key] = tr
        return
    if d.divergence is not None:
        return
    paper = _TRACES.get(key)
    if paper is None:                                 # LIVE selected alone: run the body on PAPER for the baseline
        p = Dual("PAPER", tmp_path_factory.mktemp("paper_baseline"))
        try:
            request.function(dual=p)
            paper = p.trace()
        finally:
            p.close()
        shutil.rmtree(p.base, ignore_errors=True)
    if paper.get("divergence") is not None:
        return
    assert_same_path(paper, tr)
