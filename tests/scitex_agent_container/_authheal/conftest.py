"""Real temp state for the login-expired auto-restart suites — no mocks.

Every fixture hands the pass REAL state it can write to and a test can read
back: an on-disk restart-history file and an on-disk scitex-todo store.
Detection is capture-driven (injected panes), so — unlike the reconcile
suite — no state.db or fleet registry is needed here.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path: Path) -> str:
    """A real (initially absent) scitex-todo store path — no mocks."""
    return str(tmp_path / "tasks.yaml")


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """The restart-history file. Absent = the first run ever."""
    return tmp_path / "login-expired-history.json"


@pytest.fixture
def denied_history(tmp_path: Path):
    """A history path the REAL writer genuinely cannot create. No mocks.

    The parent dir is read-only, so ``_prove_writable`` fails the way it
    would on a revoked mount — the world says no; nothing is injected. This
    is what drives the BUDGET-UNKNOWN refusal.
    """
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        yield readonly / "history.json"
    finally:
        readonly.chmod(0o755)
