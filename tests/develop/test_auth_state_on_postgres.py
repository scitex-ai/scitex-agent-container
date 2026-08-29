#!/usr/bin/env python3
"""`auth_state` is on PostgreSQL, and an unreachable store must not read as green.

WHY THESE ASSERTIONS AND NOT OTHERS. This table exists to tell a wedged agent
apart from a working one: an agent whose API calls are rejected sits at its
prompt forever while every liveness probe reads GREEN. So the failure mode that
matters is not "the port is slow" — it is "the port returns an EMPTY cache and
every wedged agent renders as verified-green again".

The negative control is therefore the important test: point the store at a DSN
that cannot be reached and assert the read still returns the no-verdict shape
AND says so. A test that only checked the happy path would pass on a port that
silently swallowed every store failure.

LIVES IN tests/develop/ — it asserts on module behaviour that has no
src/<pkg>/.../X.py counterpart of its own name, and the mirror tree is for
files that do.

NO MONKEYPATCH (PA-306 §3): env is saved and restored directly.
"""

from __future__ import annotations

import contextlib
import os

from scitex_agent_container._state import auth_state as A

DEAD_DSN = "postgresql://nobody@127.0.0.1:59999/nothing"


@contextlib.contextmanager
def _dead_store():
    """Point the store at an unreachable DSN, then restore."""
    saved = os.environ.get("SCITEX_STORE_DSN")
    os.environ["SCITEX_STORE_DSN"] = DEAD_DSN
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SCITEX_STORE_DSN", None)
        else:
            os.environ["SCITEX_STORE_DSN"] = saved


def test_the_store_target_is_postgres_not_a_file():
    """The whole point of the port: no SQLite path anywhere."""
    # Arrange
    target = A.auth_state_store_target()
    # Act
    backend = str(target.backend)
    # Assert
    assert "postgres" in backend.lower()


def test_an_unreachable_store_returns_the_no_verdict_shape(caplog):
    """It must not raise — `agent start` calls this with no try/except."""
    # Arrange
    ctx = _dead_store()
    # Act
    with ctx:
        got = A.list_auth_states()
    # Assert
    assert got == {}


def test_an_unreachable_store_SAYS_SO(caplog):
    """NEGATIVE CONTROL — an empty dict must never be silent.

    Without this, the test above would pass on a port that swallowed every
    store failure, which is exactly how a wedged agent goes back to rendering
    green. The empty shape is only acceptable BECAUSE it is announced.
    """
    # Arrange
    caplog.set_level("ERROR")
    # Act
    with _dead_store():
        A.list_auth_states()
    # Assert
    assert "auth cache unreadable" in caplog.text


def test_get_on_an_unreachable_store_returns_None_rather_than_raising():
    """A stopped PostgreSQL must not block every agent start on the host."""
    # Arrange
    name = "__never_recorded__"
    # Act
    with _dead_store():
        got = A.get_auth_state(name)
    # Assert
    assert got is None


def test_the_pure_verdict_helpers_still_need_no_database():
    """`verdict_for` was storage-free before the port and must stay that way."""
    # Arrange
    row = {"name": "x", "auth_failed": True, "checked_at": None,
           "banner": None, "reason": "", "note": ""}
    # Act
    verdict = A.verdict_for(row, started_at=None)
    # Assert
    assert verdict is not None
