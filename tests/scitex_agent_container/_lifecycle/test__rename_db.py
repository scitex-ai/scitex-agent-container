"""state.db rows follow the agent — and the undo puts them back exactly.

Real SQLite, real schema (``init_schema``), tmp file. The undo is
rowid-scoped, which is the whole point: a naive
``UPDATE … SET name = old WHERE name = new`` would also clobber rows that
ALREADY held the new name — e.g. the history of a previously deleted agent
that happened to be called that. The last test here is that trap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._rename_db import (
    count_rows,
    rename_rows,
    undo_rename_rows,
)
from scitex_agent_container._lifecycle._rename_plan import Layout

from .._helpers.fleet_root import make_state_db, seed_db_rows

OLD = "scitex-todo"
NEW = "scitex-cards"


# The rows a real agent leaves across the identity AND history tables.
_SEED = [
    (
        "INSERT INTO definitions (id, name, yaml_path, yaml_sha256, scope, "
        "first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("d1", OLD, f"/root/agents/{OLD}/spec.yaml", "sha", "user", "t0"),
    ),
    (
        "INSERT INTO instances (id, name, host, scope, started_at, workdir, "
        "ended_at, spawned_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("i1", OLD, "h", "user", "t0", f"/home/u/proj/{OLD}", "t1", "cli"),
    ),
    (
        "INSERT INTO comms_nodes (name, host, a2a_port, registered_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?)",
        (OLD, "h", 9001, 1.0, 1.0),
    ),
    (
        "INSERT INTO node_comms_policy (name, updated_at) VALUES (?, ?)",
        (OLD, 1.0),
    ),
    (
        "INSERT INTO lineage (child_name, parent_name, created_at) "
        "VALUES (?, ?, ?)",
        ("child-a", OLD, 1.0),
    ),
    (
        "INSERT INTO turns (turn_id, name, host, status, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", OLD, "h", "ok", 1.0),
    ),
]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    layout = Layout(root=tmp_path / "fleet")
    return make_state_db(layout)


@pytest.fixture
def seeded(db: Path) -> Path:
    """A DB holding rows for OLD across the identity + history tables."""
    return seed_db_rows(db, _SEED)


def _one(db: Path, sql: str, *args):
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(sql, args).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# count_rows — what --dry-run prints
# ---------------------------------------------------------------------------


def test_count_rows_counts_the_identity_row(seeded: Path):
    # Arrange
    key = "comms_nodes.name"
    # Act
    counts = count_rows(seeded, OLD)
    # Assert
    assert counts[key] == 1


def test_count_rows_counts_the_history_row(seeded: Path):
    """History follows the agent: a renamed agent is the SAME agent."""
    # Arrange
    key = "turns.name"
    # Act
    counts = count_rows(seeded, OLD)
    # Assert
    assert counts[key] == 1


def test_count_rows_counts_the_spec_path_column(seeded: Path):
    # Arrange
    key = "definitions.yaml_path"
    # Act
    counts = count_rows(seeded, OLD)
    # Assert
    assert counts[key] == 1


def test_count_rows_is_empty_when_the_db_does_not_exist(tmp_path: Path):
    """A fleet that never started an agent has no state.db. Not an error."""
    # Arrange
    missing = tmp_path / "nope" / "state.db"
    # Act
    counts = count_rows(missing, OLD)
    # Assert
    assert counts == {}


# ---------------------------------------------------------------------------
# rename_rows
# ---------------------------------------------------------------------------


def test_rename_moves_the_comms_node_row(seeded: Path):
    """Miss this and the A2A directory still advertises the dead name."""
    # Arrange
    sql = "SELECT COUNT(*) FROM comms_nodes WHERE name = ?"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql, NEW) == 1


def test_rename_moves_the_acl_policy_row(seeded: Path):
    """Miss this and the ACL gate has no policy for the live name."""
    # Arrange
    sql = "SELECT COUNT(*) FROM node_comms_policy WHERE name = ?"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql, NEW) == 1


def test_rename_moves_the_lineage_parent_edge(seeded: Path):
    # Arrange
    sql = "SELECT parent_name FROM lineage WHERE child_name = 'child-a'"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql) == NEW


def test_rename_moves_the_history_rows(seeded: Path):
    # Arrange
    sql = "SELECT COUNT(*) FROM turns WHERE name = ?"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql, NEW) == 1


def test_rename_rewrites_the_spec_path_component(seeded: Path):
    # Arrange
    sql = "SELECT yaml_path FROM definitions WHERE id = 'd1'"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql) == f"/root/agents/{NEW}/spec.yaml"


def test_rename_rewrites_the_instance_workdir_component(seeded: Path):
    # Arrange
    sql = "SELECT workdir FROM instances WHERE id = 'i1'"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql) == f"/home/u/proj/{NEW}"


def test_rename_leaves_no_row_behind_under_the_old_name(seeded: Path):
    # Arrange
    sql = "SELECT COUNT(*) FROM comms_nodes WHERE name = ?"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql, OLD) == 0


def test_rename_is_a_no_op_on_a_missing_db(tmp_path: Path):
    # Arrange
    missing = tmp_path / "nope" / "state.db"
    # Act
    undo = rename_rows(missing, OLD, NEW)
    # Assert
    assert undo.total == 0


# ---------------------------------------------------------------------------
# undo — rowid-scoped, so it cannot clobber a pre-existing `new`
# ---------------------------------------------------------------------------


def test_undo_restores_the_identity_row(seeded: Path):
    # Arrange
    undo = rename_rows(seeded, OLD, NEW)
    sql = "SELECT COUNT(*) FROM comms_nodes WHERE name = ?"
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _one(seeded, sql, OLD) == 1


def test_undo_restores_the_rewritten_path(seeded: Path):
    # Arrange
    undo = rename_rows(seeded, OLD, NEW)
    sql = "SELECT yaml_path FROM definitions WHERE id = 'd1'"
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _one(seeded, sql) == f"/root/agents/{OLD}/spec.yaml"


def test_undo_does_not_clobber_a_row_that_already_held_the_new_name(seeded: Path):
    """The trap a `WHERE name = new` undo would fall into.

    A previously deleted agent called ``scitex-cards`` can have left
    history behind. Renaming ``scitex-todo`` -> ``scitex-cards`` and then
    rolling back must NOT drag that stranger's row along.
    """
    # Arrange
    conn = sqlite3.connect(str(seeded))
    with conn:
        conn.execute(
            "INSERT INTO turns (turn_id, name, host, status, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t-stranger", NEW, "h", "ok", 0.5),
        )
    conn.close()
    undo = rename_rows(seeded, OLD, NEW)
    sql = "SELECT name FROM turns WHERE turn_id = 't-stranger'"
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _one(seeded, sql) == NEW
