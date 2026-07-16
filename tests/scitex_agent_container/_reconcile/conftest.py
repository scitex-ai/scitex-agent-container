"""Real temp state for the reconcile suites — no mocks anywhere.

Every fixture here hands the pass REAL state it can write to and a test can
read back: an on-disk ``state.db``, an on-disk fleet registry of v3 specs,
an on-disk scitex-todo store, an on-disk history file.

The ``db_path`` fixture redirects BOTH handles (the env var AND the
already-baked ``DEFAULT_DB_PATH`` module constant), because the constant is
computed at import and an env-only fixture is too late — the trap
``tests/conftest.py`` documents at length, and the one that once landed test
agent names in the live production fleet database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._state import state_db


@pytest.fixture
def db_path(tmp_path: Path):
    # Arrange — an isolated on-disk state.db, both handles redirected.
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
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
def store(tmp_path: Path) -> str:
    """A real (initially absent) scitex-todo store path — no mocks."""
    return str(tmp_path / "tasks.yaml")


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """The restart-history file. Absent = the first run ever."""
    return tmp_path / "history.json"


@pytest.fixture
def unwritable(tmp_path: Path):
    """A store path the REAL writer genuinely cannot write. No mocks.

    The parent dir is read-only, so ``scitex_todo``'s own write fails the
    way it would on a broken host — the world says no; nothing is injected.
    """
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        yield str(readonly / "tasks.yaml")
    finally:
        readonly.chmod(0o755)
