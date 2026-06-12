"""Tests for the ``comms_node_groups`` join table (Theme 15).

Covers:

* DDL is wired into :func:`state_db.init_schema` — a fresh DB has the
  table with the expected columns and the index on ``group_name``.
* ``KNOWN_TABLES`` includes ``comms_node_groups`` so ``sac db query
  --table=comms_node_groups`` is whitelisted.
* :func:`migrate_node_groups_split` is idempotent — re-running it on a
  populated DB makes no changes.
* :func:`migrate_node_groups_split` backfills any non-reserved
  ``node_comms_policy.lineage_group`` value as a single ``comms_node_groups``
  row; reserved values (``''``, ``'solitary'``) are excluded.
* :func:`has_shared_group` returns True iff two nodes share an explicit
  group row; falls back to a singleton synthesised from
  ``node_comms_policy.lineage_group`` when no explicit rows exist for
  the node, preserving the pre-PR ``derive_group`` result.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scitex_agent_container._state.state_db import KNOWN_TABLES, init_schema
from scitex_agent_container._state.state_db_groups import (
    migrate_node_groups_split,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a freshly-initialised state.db path."""
    p = tmp_path / "state.db"
    init_schema(p)
    return p


def test_known_tables_includes_comms_node_groups():
    # Arrange / Act / Assert: the new table is whitelisted for sac db query.
    assert "comms_node_groups" in KNOWN_TABLES


def test_init_schema_creates_comms_node_groups_table(db_path: Path):
    # Arrange / Act: fresh DB.
    with sqlite3.connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info('comms_node_groups')").fetchall()
        }
    # Assert: schema matches the design (node_name, group_name, created_at).
    assert cols == {"node_name", "group_name", "created_at"}


def test_init_schema_creates_group_name_index(db_path: Path):
    # Arrange / Act.
    with sqlite3.connect(db_path) as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='comms_node_groups'"
        ).fetchall()
    names = {row[0] for row in idx}
    # Assert: the by-group index is present.
    assert "idx_comms_node_groups_group" in names


def test_migrate_node_groups_split_idempotent(db_path: Path):
    # Arrange: snapshot the table count after a first migration.
    with sqlite3.connect(db_path) as conn:
        migrate_node_groups_split(conn)
        first = conn.execute("SELECT COUNT(*) FROM comms_node_groups").fetchone()[0]
        # Act: run again.
        migrate_node_groups_split(conn)
        second = conn.execute("SELECT COUNT(*) FROM comms_node_groups").fetchone()[0]
    # Assert: second run is a no-op.
    assert first == second


def test_migrate_node_groups_split_backfills_legacy_lineage_group(
    db_path: Path,
):
    # Arrange: seed a node_comms_policy row with a custom group label.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node_comms_policy "
            "(name, lineage_group, updated_at) VALUES (?, ?, ?)",
            ("alice", "ops", 100.0),
        )
        conn.commit()
        # Act.
        migrate_node_groups_split(conn)
        rows = conn.execute(
            "SELECT node_name, group_name FROM comms_node_groups WHERE node_name = ?",
            ("alice",),
        ).fetchall()
    # Assert: the legacy single label was promoted to a group row.
    assert rows == [("alice", "ops")]


def test_migrate_node_groups_split_excludes_reserved_discriminants(
    db_path: Path,
):
    # Arrange: empty + solitary stay out of the join table.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node_comms_policy "
            "(name, lineage_group, updated_at) VALUES (?, ?, ?)",
            ("derive", "", 100.0),
        )
        conn.execute(
            "INSERT INTO node_comms_policy "
            "(name, lineage_group, updated_at) VALUES (?, ?, ?)",
            ("solo", "solitary", 100.0),
        )
        conn.commit()
        # Act.
        migrate_node_groups_split(conn)
        rows = conn.execute("SELECT node_name FROM comms_node_groups").fetchall()
    names = {r[0] for r in rows}
    # Assert: neither reserved discriminant promoted.
    assert "derive" not in names and "solo" not in names


def test_has_shared_group_true_when_both_in_same_explicit_group(
    db_path: Path,
):
    # Arrange: seed both nodes with the explicit "ops" group.
    from scitex_agent_container._state.state_db_groups import (
        has_shared_group,
    )

    with sqlite3.connect(db_path) as conn:
        for n in ("alice", "bob"):
            conn.execute(
                "INSERT INTO comms_node_groups "
                "(node_name, group_name, created_at) VALUES (?, ?, ?)",
                (n, "ops", 100.0),
            )
        conn.commit()
    # Act / Assert.
    assert has_shared_group(a="alice", b="bob", db_path=db_path) is True


def test_has_shared_group_false_when_groups_disjoint(db_path: Path):
    # Arrange.
    from scitex_agent_container._state.state_db_groups import (
        has_shared_group,
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO comms_node_groups "
            "(node_name, group_name, created_at) VALUES (?, ?, ?)",
            ("alice", "ops", 100.0),
        )
        conn.execute(
            "INSERT INTO comms_node_groups "
            "(node_name, group_name, created_at) VALUES (?, ?, ?)",
            ("bob", "dev", 100.0),
        )
        conn.commit()
    # Act / Assert.
    assert has_shared_group(a="alice", b="bob", db_path=db_path) is False


def test_has_shared_group_synthesises_singleton_from_legacy_policy(
    db_path: Path,
):
    # Arrange: pre-migration: alice has node_comms_policy.lineage_group="ops"
    # but no comms_node_groups row. has_shared_group must still match bob
    # when bob also lives in the same singleton via legacy column.
    from scitex_agent_container._state.state_db_groups import (
        has_shared_group,
    )

    with sqlite3.connect(db_path) as conn:
        for n in ("alice", "bob"):
            conn.execute(
                "INSERT INTO node_comms_policy "
                "(name, lineage_group, updated_at) VALUES (?, ?, ?)",
                (n, "ops", 100.0),
            )
        conn.commit()
    # Act / Assert: legacy column still drives the answer.
    assert has_shared_group(a="alice", b="bob", db_path=db_path) is True
