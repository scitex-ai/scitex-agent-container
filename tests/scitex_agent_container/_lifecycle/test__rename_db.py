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
    # An ``INSERT INTO definitions`` row was here until 2026-08-28, and the
    # identity + spec-path assertions below were aimed at it. That table was
    # deleted from state.db for having no writer in any code path, ever, so
    # the seed would raise. The ``instances`` row below already carries both
    # halves it stood for — ``name`` is a ``NAME_COLUMNS`` pair and
    # ``workdir`` is the surviving ``PATH_COLUMNS`` pair — and it is the row
    # a real agent actually leaves behind.
    (
        "INSERT INTO instances (id, name, host, scope, started_at, workdir, "
        "ended_at, spawned_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("i1", OLD, "h", "user", "t0", f"/home/u/proj/{OLD}", "t1", "cli"),
    ),
    # An ``INSERT INTO comms_nodes`` row was here until 2026-08-28. The
    # ADR-0014 directory moved to PostgreSQL, so SQLite has no such table and
    # the seed would raise on every test in this file.
    (
        "INSERT INTO lineage (child_name, parent_name, created_at) "
        "VALUES (?, ?, ?)",
        ("child-a", OLD, 1.0),
    ),
    # The history half. It was ``INSERT INTO turns`` until 2026-08-28, when
    # the diary trio left SQLite for per-host PostgreSQL; then ``INSERT INTO
    # attempts`` for the rest of that day, until ``attempts`` was deleted for
    # having zero writers. ``channel_events.target`` is the history column
    # still in ``_rename_db.NAME_COLUMNS`` AND still a real SQLite table, so
    # it is what the history-follows-the-agent tests below now exercise.
    (
        "INSERT INTO channel_events (target, source, kind, content, "
        "meta_json, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (OLD, None, "message", "hi", "{}", 1.0),
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
    # Arrange — ``comms_nodes.name`` was the key here until 2026-08-28, then
    # ``definitions.name`` for the rest of that day; both tables left SQLite.
    # ``instances.name`` is the identity column production code writes.
    key = "instances.name"
    # Act
    counts = count_rows(seeded, OLD)
    # Assert
    assert counts[key] == 1


def test_count_rows_counts_the_history_row(seeded: Path):
    """History follows the agent: a renamed agent is the SAME agent."""
    # Arrange
    key = "channel_events.target"
    # Act
    counts = count_rows(seeded, OLD)
    # Assert
    assert counts[key] == 1


def test_count_rows_counts_the_path_column(seeded: Path):
    # Arrange — ``definitions.yaml_path`` until 2026-08-28; that table left
    # SQLite, and ``instances.workdir`` is the only ``PATH_COLUMNS`` pair
    # remaining, so it is what --dry-run can still report a count for.
    key = "instances.workdir"
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


# ``test_rename_moves_the_comms_node_row`` was here until 2026-08-28, under
# the docstring "Miss this and the A2A directory still advertises the dead
# name." That sentence is still true and the test could no longer prove it:
# the directory moved to PostgreSQL, SQLite has no ``comms_nodes``, and
# ``rename_rows`` SKIPS tables absent from sqlite_master — so the test would
# have passed forever while reaching nothing. Same ruling, and the same
# hazard, as the ACL-policy test noted below: a green test whose name claims
# a property it can no longer reach is worse than a red one.
#
# DELETED, NOT EDITED UNTIL IT PASSED. The property is measured where it now
# lives — ``_state/test_state_db_comms_nodes.py::test_rename_moves_the_
# routing_tuple_onto_the_new_name`` and ``::test_rename_withdraws_the_old_
# name`` — and ``_rename.apply_plan`` runs the move as its own
# ``comms-directory`` step with its inverse on the undo stack.


# ``test_rename_moves_the_acl_policy_row`` was here until 2026-08-28. It
# asserted that ``rename_rows`` UPDATEs ``node_comms_policy.name``, and the
# migration of that table to PostgreSQL killed the premise outright: SQLite
# no longer has the table, and ``rename_rows`` SKIPS tables absent from
# sqlite_master. Left in place it would have passed forever while reaching
# nothing — a green test whose name claims a property it can no longer test,
# which is worse than a red one because nothing forces anyone to look.
#
# DELETED, NOT EDITED UNTIL IT PASSED. The property it named is real and
# still holds; it is measured where it now lives —
# ``_state/test_state_db_acl_policy.py::test_rename_carries_the_policy_to_
# the_new_name`` and ``::test_rename_retires_the_old_name``, against the
# store that actually holds the row. ``_rename.apply_plan`` runs that move
# as its own ``acl-policy`` step, with its inverse on the undo stack.


def test_rename_moves_the_lineage_parent_edge(seeded: Path):
    # Arrange
    sql = "SELECT parent_name FROM lineage WHERE child_name = 'child-a'"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql) == NEW


def test_rename_moves_the_history_rows(seeded: Path):
    # Arrange
    sql = "SELECT COUNT(*) FROM channel_events WHERE target = ?"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql, NEW) == 1


# ``test_rename_rewrites_the_spec_path_component`` was here until
# 2026-08-28. It asserted ``rename_rows`` rewrites the ``<name>`` component
# of ``definitions.yaml_path``, and that table left SQLite the same day for
# having no writer in any code path.
#
# DELETED RATHER THAN RE-POINTED, and this one is the opposite case from the
# two departures above: not because the property died, but because
# re-pointing it would have produced a byte-for-byte duplicate of
# ``test_rename_rewrites_the_instance_workdir_component`` immediately below.
# ``instances.workdir`` is the ONLY ``PATH_COLUMNS`` pair left, so the
# property "a rename rewrites the agent-name component of a stored path" now
# has exactly one place to be measured, and it is measured there. Two
# identical tests would not double the coverage; they would only make the
# next person wonder which one is the real one.


def test_rename_rewrites_the_instance_workdir_component(seeded: Path):
    # Arrange
    sql = "SELECT workdir FROM instances WHERE id = 'i1'"
    # Act
    rename_rows(seeded, OLD, NEW)
    # Assert
    assert _one(seeded, sql) == f"/home/u/proj/{NEW}"


def test_rename_leaves_no_row_behind_under_the_old_name(seeded: Path):
    # Arrange
    sql = "SELECT COUNT(*) FROM instances WHERE name = ?"
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
    sql = "SELECT COUNT(*) FROM instances WHERE name = ?"
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _one(seeded, sql, OLD) == 1


def test_undo_restores_the_rewritten_path(seeded: Path):
    # Arrange — ``definitions.yaml_path`` until 2026-08-28; re-pointed at the
    # surviving PATH column rather than deleted, because the property (an
    # undo puts a rewritten path back) is real and nothing else covers it.
    undo = rename_rows(seeded, OLD, NEW)
    sql = "SELECT workdir FROM instances WHERE id = 'i1'"
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _one(seeded, sql) == f"/home/u/proj/{OLD}"


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
            "INSERT INTO channel_events (target, source, kind, content, "
            "meta_json, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (NEW, None, "stranger", "hi", "{}", 2.0),
        )
    conn.close()
    undo = rename_rows(seeded, OLD, NEW)
    sql = "SELECT target FROM channel_events WHERE kind = 'stranger'"
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _one(seeded, sql) == NEW
