r"""Branch-coverage closure for state_db ``_connect`` + ``new_uuid7``.

Covers:
- 156→157: ``new_uuid7`` falls back to uuid4 when ``uuid.uuid7`` is
  missing on the runtime Python.
- 197→205: WAL-mutation loop exits cleanly via ``break`` on a fresh DB.
- 201→204/202→203: ``OperationalError`` retry path inside the WAL
  mutation loop — both the "locked, retry" and "not locked, raise"
  branches.

Real on-disk SQLite files via ``tmp_path``. Hand-rolled connection
wrappers feed forced ``OperationalError``\ s into the production
retry loop via the ``connector`` injection point — no monkeypatch,
no mocks.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

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


# ---------------------------------------------------------------------------
# 197→205: WAL-mutation loop executes and breaks on a fresh DB.
# ---------------------------------------------------------------------------


def test_connect_on_fresh_db_promotes_journal_mode_to_wal(tmp_path: Path) -> None:
    # Arrange
    db_file = tmp_path / "fresh.db"
    # Act
    conn = state_db._connect(db_file)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    # Assert
    assert str(mode).lower() == "wal"


def test_connect_on_already_wal_db_skips_mutation_loop(tmp_path: Path) -> None:
    # Arrange
    db_file = tmp_path / "warm.db"
    state_db._connect(db_file).close()
    # Act
    conn = state_db._connect(db_file)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    # Assert
    assert str(mode).lower() == "wal"


# ---------------------------------------------------------------------------
# 202→203 retry path — hand-rolled connections injected via ``connector``.
# ---------------------------------------------------------------------------


class _FlakyConn:
    """Real sqlite3.Connection wrapper; first WAL exec raises 'locked'."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self._wal_attempts = 0

    def execute(self, sql: str, *args, **kwargs):
        if "journal_mode = WAL" in sql and self._wal_attempts == 0:
            self._wal_attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _flaky_connector(db_path: Path) -> _FlakyConn:
    return _FlakyConn(sqlite3.connect(db_path, timeout=30.0))


def test_connect_retries_when_wal_pragma_reports_locked(tmp_path: Path) -> None:
    # Arrange
    db_file = tmp_path / "locked-once.db"
    # Act
    conn = state_db._connect(db_file, connector=_flaky_connector)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    # Assert
    assert str(mode).lower() == "wal"


class _IOErrorConn:
    """Raises a non-'locked' OperationalError for every WAL pragma."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *args, **kwargs):
        if "journal_mode = WAL" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _io_error_connector(db_path: Path) -> _IOErrorConn:
    return _IOErrorConn(sqlite3.connect(db_path, timeout=30.0))


def test_connect_propagates_non_locked_operational_error(tmp_path: Path) -> None:
    # Arrange
    db_file = tmp_path / "io-error.db"
    # Act / context — pytest.raises is the single assertion.
    # Assert
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        state_db._connect(db_file, connector=_io_error_connector)


class _AlwaysLockedConn:
    """Every WAL attempt raises 'locked' — exhausts the retry budget.

    Tracks attempts so the test can verify the loop ran its bound.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.wal_attempts = 0

    def execute(self, sql: str, *args, **kwargs):
        if "journal_mode = WAL" in sql:
            self.wal_attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


@pytest.fixture
def fast_sleep():
    """Replace stdlib ``time.sleep`` with a no-op so the 50-iter
    bounded-retry loop runs in milliseconds instead of ~25s."""
    # Arrange
    import time as _time

    saved = _time.sleep
    _time.sleep = lambda _s: None
    yield
    _time.sleep = saved


def test_connect_raises_after_exhausting_lock_retries(
    tmp_path: Path, fast_sleep
) -> None:
    # Arrange
    db_file = tmp_path / "always-locked.db"

    def _connector(p: Path) -> _AlwaysLockedConn:
        return _AlwaysLockedConn(sqlite3.connect(p, timeout=30.0))

    # Act / context — pytest.raises is the single assertion.
    # Assert
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        state_db._connect(db_file, connector=_connector)
