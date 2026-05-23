"""Tests for ``state_db_instances`` family-tree columns (Rule B/D).

The sac-agent-spawn design adds ``bound_port`` / ``remote`` /
``spawned_by`` to the ``instances`` table so every start — local or
cross-host dispatch — records its bound port, locality and lineage as
an intrinsic side-effect. These tests use a real on-disk SQLite
state.db (isolated per test via the ``SCITEX_AGENT_CONTAINER_STATE_DB``
env override) — no mocks. Each test carries AAA markers and a single
assertion.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env (explicit save/restore)."""
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _active_row(name: str) -> dict:
    from scitex_agent_container._state.state_db import list_active_instances

    return [r for r in list_active_instances() if r["name"] == name][0]


# ---------------------------------------------------------------------------
# Schema — fresh DB carries the family-tree columns.
# ---------------------------------------------------------------------------


def test_fresh_instances_table_has_bound_port_column(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import init_schema

    # Act
    init_schema()
    # Assert
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    assert "bound_port" in cols


def test_fresh_instances_table_has_remote_column(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import init_schema

    # Act
    init_schema()
    # Assert
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    assert "remote" in cols


def test_fresh_instances_table_has_spawned_by_column(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import init_schema

    # Act
    init_schema()
    # Assert
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    assert "spawned_by" in cols


# ---------------------------------------------------------------------------
# record_instance_start — writes the new columns.
# ---------------------------------------------------------------------------


def test_record_instance_start_persists_remote_flag(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    # Act
    record_instance_start(name="rem-1", host="peer-x", a2a_port=19001, remote=True)
    # Assert
    assert _active_row("rem-1")["remote"] == 1


def test_record_instance_start_persists_spawned_by(db_path: Path):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    # Act
    record_instance_start(name="sb-1", host="peer-x", spawned_by="parent-agent")
    # Assert
    assert _active_row("sb-1")["spawned_by"] == "parent-agent"


def test_record_instance_start_defaults_bound_port_to_a2a_port(db_path: Path):
    # Arrange — caller passes only the resolved a2a_port (no explicit
    # bound_port); the helper must mirror it into bound_port.
    from scitex_agent_container._state.state_db import record_instance_start

    # Act
    record_instance_start(name="bp-1", host="peer-x", a2a_port=19042)
    # Assert
    assert _active_row("bp-1")["bound_port"] == 19042


def test_record_instance_start_keeps_explicit_bound_port(db_path: Path):
    # Arrange — an explicit bound_port wins over the a2a_port default.
    from scitex_agent_container._state.state_db import record_instance_start

    # Act
    record_instance_start(name="bp-2", host="peer-x", a2a_port=None, bound_port=19077)
    # Assert
    assert _active_row("bp-2")["bound_port"] == 19077


def test_record_instance_start_defaults_remote_to_zero(db_path: Path):
    # Arrange — a local start omits remote; column defaults to 0.
    from scitex_agent_container._state.state_db import record_instance_start

    # Act
    record_instance_start(name="loc-1", host="this-host", a2a_port=19003)
    # Assert
    assert _active_row("loc-1")["remote"] == 0


# ---------------------------------------------------------------------------
# Migration — an instances table created before the columns gets them.
# ---------------------------------------------------------------------------


def _create_pre_family_tree_instances(db: Path) -> None:
    """Build an ``instances`` table WITHOUT the family-tree columns."""
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE instances (
                id TEXT PRIMARY KEY, definition_id TEXT, name TEXT NOT NULL,
                host TEXT NOT NULL, scope TEXT NOT NULL, pid INTEGER,
                ppid INTEGER, screen TEXT, workdir TEXT, a2a_port INTEGER,
                started_at TEXT NOT NULL, last_heartbeat_at TEXT,
                ended_at TEXT, exit_reason TEXT, iter_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0
            );
            INSERT INTO instances (id, name, host, scope, started_at, a2a_port)
            VALUES ('old1', 'legacy', 'h', 'global', '2026-01-01T00:00:00Z', 18888);
            """
        )
        conn.commit()


def test_migration_adds_bound_port_to_pre_existing_table(db_path: Path):
    # Arrange — a DB whose instances table predates the columns.
    _create_pre_family_tree_instances(db_path)
    from scitex_agent_container._state.state_db import init_schema

    # Act — init_schema runs the idempotent ADD COLUMN migration.
    init_schema()
    # Assert
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    assert "bound_port" in cols


def test_migration_preserves_legacy_row_on_pre_existing_table(db_path: Path):
    # Arrange
    _create_pre_family_tree_instances(db_path)
    from scitex_agent_container._state.state_db import init_schema

    # Act
    init_schema()
    # Assert — the legacy row survives the ADD COLUMN migration.
    assert _active_row("legacy")["a2a_port"] == 18888


def test_migration_is_idempotent_on_replay(db_path: Path):
    # Arrange — a fresh DB already has the columns from the DDL.
    from scitex_agent_container._state.state_db import init_schema

    init_schema()
    # Act — a second init_schema must not raise (ADD COLUMN guarded).
    init_schema()
    # Assert — reaching here means the replay did not raise.
    assert db_path.exists()
