"""Real temp state for the reconcile suites — no mocks anywhere.

Every fixture here hands the pass REAL state it can write to and a test can
read back: an on-disk ``state.db``, an on-disk fleet registry of v3 specs,
an on-disk sac event log, an on-disk history file.

The ``db_path`` fixture redirected BOTH handles — the env var AND the
already-baked ``DEFAULT_DB_PATH`` module constant — because the constant was
computed at import and an env-only fixture was too late; that is the trap
``tests/conftest.py`` documents at length, and the one that once landed test
agent names in the live production fleet database. The constant was deleted
with the storage engine on 2026-08-30, so only the env var is redirected now.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest



@pytest.fixture
def db_path(tmp_path: Path):
    # Arrange — an isolated per-test value for the env var.
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    try:
        yield db
    finally:
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    # Arrange — a tmp fleet registry of real spec.yaml files, passed to the
    # pass explicitly via specs_dir (no env override needed).
    reg = tmp_path / "agents"
    reg.mkdir()
    return reg


@pytest.fixture
def events(tmp_path: Path) -> Path:
    """A real (initially absent) sac event-log path — no mocks.

    Nothing creates it until a pass records something, so ``events.exists()``
    is itself the assertion "this pass recorded nothing at all".
    """
    return tmp_path / "sac-events.jsonl"


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """The restart-history file. Absent = the first run ever."""
    return tmp_path / "history.json"


@pytest.fixture
def unwritable(tmp_path: Path):
    """An event-log path the REAL writer genuinely cannot write. No mocks.

    The parent dir is read-only, so the append fails the way it would on a
    broken host — the world says no; nothing is injected.
    """
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        yield readonly / "sac-events.jsonl"
    finally:
        readonly.chmod(0o755)
