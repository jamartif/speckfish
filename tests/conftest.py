"""Shared fixtures."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from speckfish import cli as C


@pytest.fixture
def fake_agent(monkeypatch):
    """CLI tests must not depend on which agent CLIs the machine has: always resolve a fake harness."""
    monkeypatch.setattr(C, "get_harness", lambda cfg, name=None: NS(name="fake"))


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """The runner looks at its timers every second by default; tests do not need to wait for that."""
    monkeypatch.setenv("SPECKFISH_POLL_SECONDS", "0.1")
