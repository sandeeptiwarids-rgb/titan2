"""Fixtures for the ops tests; the fake server and its fake commands are in opsharness.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))      # `import opsharness` under any pytest import mode

from opsharness import BASH, BOT_READY_THEN_EXIT0, REL_A, Server, clone_server, make_server  # noqa: E402


@pytest.fixture
def server(tmp_path: Path):
    """An empty fake server (no release yet)."""
    if BASH is None:
        pytest.skip("needs bash (Git for Windows on Windows)")
    srv = make_server(tmp_path)
    yield srv
    srv.unfreeze()                  # releases are chmod -R a-w: let pytest clean the temp dir


@pytest.fixture(scope="session")
def template_a(tmp_path_factory):
    """Built once: a server where release A was installed after hours (the bot reached READY and exited 0)."""
    if BASH is None:
        pytest.skip("needs bash (Git for Windows on Windows)")
    srv = make_server(tmp_path_factory.mktemp("ops-template-a"))
    srv.on_restart("titan2-bot", BOT_READY_THEN_EXIT0)
    r = srv.install(REL_A)
    assert r.returncode == 0, r.stdout + r.stderr
    srv.on_restart("titan2-bot", None)
    yield srv
    srv.unfreeze()


@pytest.fixture
def installed(tmp_path: Path, template_a: Server):
    """A fresh copy of template_a: current -> releases/A, units installed, titan2-bot inactive."""
    srv = clone_server(template_a, tmp_path)
    yield srv
    srv.unfreeze()
