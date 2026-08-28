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
    # An ``INSERT INTO definitions`` row was here until 2026-08-28, and an
    # ``INSERT INTO instances`` row replaced it for part of that day. Both
    # tables have since left state.db — ``definitions`` deleted for having no
    # writer in any code path, ``instances`` moved to the shared PostgreSQL
    # store — so either seed would now raise.
    #
    # THE PATH HALF LEFT WITH ``instances``, and it did not move to another
    # SQLite column because there is none: ``instances.workdir`` was the last
    # entry in ``_rename_db.PATH_COLUMNS``, which is now EMPTY. The property
    # "a rename rewrites the agent-name component of a stored path" is
    # measured against the store that holds it, in
    # ``_state/test_state_db_instances_rename.py``.
    #
    # An ``INSERT INTO comms_nodes`` row was here too. The ADR-0014 directory
    # moved to PostgreSQL, so SQLite has no such table and the seed would
    # raise on every test in this file.
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
    # ``definitions.name``, then ``instances.name``. All three left SQLite
    # that day; ``lineage.parent_name`` is the identity column that remains.
    key = "lineage.parent_name"
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


def test_count_rows_reports_no_path_column_because_there_is_none(
    seeded: Path,
):
    """``PATH_COLUMNS`` is EMPTY, and the count must say so rather than lie.

    It held ``definitions.yaml_path`` and then ``instances.workdir``; both
    tables left SQLite on 2026-08-28. A count that still named one would be
    the reassuring decoration this module's docstring keeps warning about.
    The path rewrite is real and lives in the store — see
    ``_state/test_state_db_instances_rename.py::
    test_the_rename_rewrites_the_workdir_component``.
    """
    # Arrange
    from scitex_agent_container._lifecycle._rename_db import PATH_COLUMNS

    # Act
    counts = count_rows(seeded, OLD)
    # Assert
    assert not PATH_COLUMNS and not any("workdir" in k for k in counts)


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


# ``test_rename_rewrites_the_instance_workdir_component`` was here until
# 2026-08-28, and it was the LAST path test in this file: its predecessor
# ``definitions.yaml_path`` had been deleted earlier the same day as a
# duplicate of it. ``instances`` then moved to the shared PostgreSQL store,
# taking ``PATH_COLUMNS``' last entry with it.
#
# DELETED, NOT EDITED UNTIL IT PASSED. ``rename_rows`` SKIPS tables absent
# from sqlite_master, so a re-pointed version would have passed forever while
# reaching nothing. The property is measured where it now lives —
# ``_state/test_state_db_instances_rename.py::
# test_the_rename_rewrites_the_workdir_component``, plus its
# leaves-a-containing-component-alone sibling — and ``_rename.apply_plan``
# runs the move as its own ``instances`` step with a key-scoped inverse on
# the undo stack.


def test_rename_leaves_no_row_behind_under_the_old_name(seeded: Path):
    # Arrange
    sql = "SELECT COUNT(*) FROM lineage WHERE parent_name = ?"
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
    sql = "SELECT COUNT(*) FROM lineage WHERE parent_name = ?"
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _one(seeded, sql, OLD) == 1


# ``test_undo_restores_the_rewritten_path`` was here until 2026-08-28. It
# followed its subject twice — ``definitions.yaml_path``, then
# ``instances.workdir`` — and both tables left SQLite that day, emptying
# ``PATH_COLUMNS``. The property (an undo puts a rewritten path back) is real
# and is measured against the store that now holds the path:
# ``_state/test_state_db_instances_rename.py::
# test_the_inverse_restores_the_workdir``.


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
