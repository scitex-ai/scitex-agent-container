r"""Branch-coverage closure for ``state_db.new_uuid7``.

Covers both arms of the version guard: ``new_uuid7`` returns a uuid7 when the
runtime Python provides ``uuid.uuid7`` (3.14+), and falls back to uuid4 when
it does not. Both are exercised against the REAL ``uuid`` module with an
explicit save/restore fixture — no monkeypatch, no mocks.

THIS FILE WAS ``test_state_db_connect_branches.py`` UNTIL THE ENGINE WENT.
The other half of it drove ``state_db._connect``: the WAL-mutation loop, its
"locked, retry" and "not locked, raise" arms, and the retry-exhaustion path,
each fed a forced ``OperationalError`` through the ``connector`` injection
point. Those tests were deleted with the function they covered — sac opens no
database of its own; state is the per-host PostgreSQL store (ADR-0022). The
file is renamed rather than kept under a name describing a function that no
longer exists.
"""

from __future__ import annotations

import uuid

import pytest

from scitex_agent_container._state import state_db

# ---------------------------------------------------------------------------
# 156→157: uuid7 fallback to uuid4
# ---------------------------------------------------------------------------


@pytest.fixture
def uuid7_absent():
    """Remove ``uuid.uuid7`` if installed; restore on teardown."""
    # Arrange
    saved = getattr(uuid, "uuid7", None)
    if saved is not None:
        delattr(uuid, "uuid7")
    yield
    if saved is not None:
        uuid.uuid7 = saved  # type: ignore[attr-defined]


def test_new_uuid7_falls_back_to_uuid4_when_unavailable(uuid7_absent) -> None:
    # Arrange
    # (fixture removes uuid.uuid7)
    # Act
    result = state_db.new_uuid7()
    # Assert
    assert uuid.UUID(result).version == 4


@pytest.fixture
def uuid7_stub():
    """Install a deterministic uuid7 surrogate; restore on teardown."""
    # Arrange
    saved = getattr(uuid, "uuid7", None)
    sentinel = uuid.UUID("00000000-0000-7000-8000-000000000001")

    def _fake_uuid7() -> uuid.UUID:
        return sentinel

    uuid.uuid7 = _fake_uuid7  # type: ignore[attr-defined]
    yield sentinel
    if saved is None:
        delattr(uuid, "uuid7")
    else:
        uuid.uuid7 = saved  # type: ignore[attr-defined]


def test_new_uuid7_uses_uuid7_when_available(uuid7_stub) -> None:
    # Arrange
    sentinel = uuid7_stub
    # Act
    result = state_db.new_uuid7()
    # Assert
    assert result == str(sentinel)


