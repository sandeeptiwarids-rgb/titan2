"""Shared pytest fixtures for every titan2 test package (spec §16.1).

Nothing here is autouse: unit tests that check the core-thread guard (I1) must see an unset CORE_TID. The harness
itself (FakeClock, ScriptedMarket, FakeKite, FaultInjector, ScenarioRunner) lives in the tests/fakes package so the
scenario, chaos and architecture tests import it explicitly.
"""

from __future__ import annotations

import pytest

from titan2 import store as store_mod


@pytest.fixture
def core():
    """Make the test thread the core thread for the duration of the test (store writes are @core_only)."""
    with store_mod.core_thread():
        yield
