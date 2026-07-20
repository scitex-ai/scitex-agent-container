"""Real temp state for the login-expired auto-restart suites — no mocks.

Every fixture hands the pass REAL state it can write to and a test can read
back: an on-disk restart-history file, an on-disk sac event log and an
on-disk fleet registry. Detection is capture-driven (injected panes), so no
state.db is needed here — but the ROSTER is real, because the pass now checks
its pane reading against the registry to find the agents that reading failed
to account for.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: The registry override ``sac.fleet-reconcile`` and ``sac agents refresh-acl``
#: already honour — the roster reader reuses it, so redirecting it here points
#: every roster lookup in this package at temp state.
_AGENTS_DIR_ENV = "SCITEX_AGENT_CONTAINER_AGENTS_DIR"


@pytest.fixture(autouse=True)
def roster(tmp_path: Path):
    """A REAL, EMPTY fleet registry — the population every pass is checked against.

    AUTOUSE, so no test in this package can reach the live fleet registry. The
    pass enumerates the roster to find the agents its pane reading failed to
    account for, and a suite pointed at the real one would report on production
    agents and flake on whatever happens to be deployed.

    Empty means READABLE and nobody registered — the neutral roster that leaves
    the injected captures as the whole population, so a test about the restart
    logic stays a test about the restart logic. Tests that need a registered
    agent create its spec dir with :func:`_helpers.register_agents`; a test that
    needs an UNREADABLE roster passes ``specs_dir`` at a path that is not there.
    """
    root = tmp_path / "agents"
    root.mkdir()
    saved = os.environ.get(_AGENTS_DIR_ENV)
    os.environ[_AGENTS_DIR_ENV] = str(root)
    try:
        yield root
    finally:
        if saved is None:
            os.environ.pop(_AGENTS_DIR_ENV, None)
        else:
            os.environ[_AGENTS_DIR_ENV] = saved


@pytest.fixture
def events(tmp_path: Path) -> Path:
    """A real (initially absent) sac event-log path — no mocks.

    Nothing creates it until a pass records something, so ``events.exists()``
    is itself the assertion "this pass recorded nothing at all".
    """
    return tmp_path / "sac-events.jsonl"


@pytest.fixture
def unwritable(tmp_path: Path):
    """An event-log path the REAL writer genuinely cannot write. No mocks.

    The parent dir is read-only, so the append fails the way it would on a
    broken host — the world says no; nothing is injected.
    """
    readonly = tmp_path / "readonly-events"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        yield readonly / "sac-events.jsonl"
    finally:
        readonly.chmod(0o755)


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
