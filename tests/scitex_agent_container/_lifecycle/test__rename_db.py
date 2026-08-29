"""``_rename_db`` REACHES NOTHING on a state.db sac creates today.

This file used to prove that a rename carries an agent's rows across
``state.db`` and that the undo puts them back exactly. It cannot prove that
any more, and pretending otherwise is the one outcome worse than saying so:
as of 2026-08-28 sac's ``init_schema`` issues ZERO ``CREATE TABLE``
statements, so there is no table in a fresh state.db for ``rename_rows`` to
touch and nothing this file could seed.

WHY THE ASSERTIONS BELOW ARE EMPTINESS AND NOT COVERAGE
=======================================================
``rename_rows`` SKIPS a table absent from ``sqlite_master`` — deliberately,
so a fleet that has never started an agent does not block a rename. That
skip is also what makes a stale ``NAME_COLUMNS`` pair a SILENT NO-OP rather
than a crash, which is why every table that left SQLite had its pairs
removed rather than left as reassuring decoration. ``instances`` was the
last of them, and with it gone every remaining pair names a table
``init_schema`` no longer creates.

So the honest measurement is: the module is reachable, it is called, and it
correctly finds nothing. That is asserted here, with a POSITIVE CONTROL
(:func:`test_the_module_still_declares_pairs_to_look_for`) so an emptiness
that came from an empty constant cannot pass as an emptiness that came from
an empty database.

RE-SEEDING WAS CONSIDERED AND REJECTED. The tables could be hand-created in
a fixture to keep the old assertions running, and they would then be
measuring a schema this file wrote — a legacy shape production no longer
defines, drifting from the moment it is typed. ``make_state_db`` exists
precisely so these suites use sac's own DDL and cannot drift; opting out of
it here to keep a green tick would be the pretence this file is written to
avoid.

WHERE THE PROPERTIES WENT — each is measured against the store that now
holds the rows, and each runs as its own step in ``_rename.apply_plan``
with its own inverse on the undo stack:

* identity, lineage edge and workdir path
  ``_state/test_state_db_instances_rename.py`` (``rename_instance_rows``)
* channel history        ``_state/test_state_db_channel_rename.py``
* A2A directory          ``_state/test_state_db_comms_nodes.py``
* ACL policy             ``_state/test_state_db_acl_policy.py``
* spawn DAG              ``_state/test_state_db_lineage_rename.py``

THE ROWID-SCOPED UNDO — the trap this file was built around, where a naive
``UPDATE … SET name = old WHERE name = new`` also clobbers rows that ALREADY
held the new name — is not lost with it. Every replacement above captures the
identities it touched BEFORE touching them and inverts key-by-key, and each
has its own does-not-clobber-a-stranger test.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._rename_db import (
    NAME_COLUMNS,
    PATH_COLUMNS,
    count_rows,
    rename_rows,
    undo_rename_rows,
)
from scitex_agent_container._lifecycle._rename_plan import Layout

from .._helpers.fleet_root import make_state_db

OLD = "scitex-todo"
NEW = "scitex-cards"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A real state.db built by sac's OWN ``init_schema``.

    Not a hand-rolled schema, for the reason in the module docstring: the
    point of these tests is what PRODUCTION creates, and today production
    creates nothing.
    """
    layout = Layout(root=tmp_path / "fleet")
    return make_state_db(layout)


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# The control, first — without it every assertion below is vacuous
# ---------------------------------------------------------------------------


def test_the_module_still_declares_pairs_to_look_for() -> None:
    """POSITIVE CONTROL. Empty constants would make the emptiness meaningless.

    ``count_rows`` returns ``{}`` both when the DECLARED tables are missing
    and when nothing is declared at all, and only the first is the finding
    this file reports. If this ever fails, the module has no work left to
    describe and should be deleted along with its ``state-db`` step rather
    than kept as a loop over an empty tuple.
    """
    # Arrange
    declared = list(NAME_COLUMNS) + list(PATH_COLUMNS)
    # Act
    tables = {table for table, _column in declared}
    # Assert
    assert tables, (
        "NAME_COLUMNS and PATH_COLUMNS are both empty, so _rename_db can "
        "never touch anything. Delete the module and its rename step — a "
        "loop over an empty tuple is the reassuring decoration this package "
        "removes everywhere else."
    )


def test_a_fresh_state_db_has_none_of_the_declared_tables(db: Path) -> None:
    """THE DOCUMENTED LIMITATION, asserted rather than described.

    Every ``(table, column)`` pair the module still declares names a table
    ``init_schema`` stopped creating. This is what makes the module a no-op
    on any database sac makes today, and stating it as an assertion is what
    stops it being rediscovered as a bug.
    """
    # Arrange
    declared = {table for table, _column in list(NAME_COLUMNS) + list(PATH_COLUMNS)}
    # Act
    present = _tables(db)
    # Assert
    assert not (declared & present), (
        f"state.db unexpectedly holds {sorted(declared & present)}. If a "
        "table came back, these tests should seed it and assert the real "
        "rename behaviour again rather than its absence."
    )


def test_a_fresh_state_db_holds_no_tables_at_all(db: Path) -> None:
    """``init_schema`` issues ZERO ``CREATE TABLE``, and it still opens clean.

    The stronger statement behind the one above, and the one worth keeping
    visible: the emptiness is not "these particular tables went", it is that
    sac's SQLite schema is now empty. A future ``CREATE TABLE`` slipping back
    in fails here as well as in
    ``tests/develop/test_sqlite_footprint_frozen.py``.
    """
    # Arrange
    expected: set[str] = set()
    # Act
    present = {t for t in _tables(db) if not t.startswith("sqlite_")}
    # Assert
    assert present == expected


# ---------------------------------------------------------------------------
# count_rows — what --dry-run prints
# ---------------------------------------------------------------------------


def test_count_rows_reports_nothing_from_state_db(db: Path) -> None:
    """The SQLite half of the dry-run count is permanently empty.

    NOT the whole report. ``_rename_plan.build_plan`` merges
    ``count_instance_rename_rows`` into this dict under the same
    ``table.column`` keys, so the operator still sees a count — see
    ``_lifecycle/test__rename.py::
    test_the_plan_counts_the_rows_a_rename_would_touch``. Were that merge
    ever removed, the dry run would print ``0 column(s)`` for an agent with
    hundreds of recorded lifetimes.
    """
    # Arrange
    expected: dict[str, int] = {}
    # Act
    counts = count_rows(db, OLD)
    # Assert
    assert counts == expected


def test_count_rows_is_empty_when_the_db_does_not_exist(tmp_path: Path) -> None:
    """A fleet that never started an agent has no state.db. Not an error."""
    # Arrange
    missing = tmp_path / "nope" / "state.db"
    # Act
    counts = count_rows(missing, OLD)
    # Assert
    assert counts == {}


# ---------------------------------------------------------------------------
# rename_rows / undo — a no-op that must stay a SAFE no-op
# ---------------------------------------------------------------------------


def test_rename_is_a_no_op_on_a_current_state_db(db: Path) -> None:
    """It must find nothing, and it must not RAISE finding nothing.

    ``_rename.apply_plan`` runs this as a step and any exception aborts and
    unwinds the whole rename, so "skips an absent table" is a live
    requirement rather than a leftover convenience.
    """
    # Arrange
    expected = 0
    # Act
    undo = rename_rows(db, OLD, NEW)
    # Assert
    assert undo.total == expected


def test_rename_is_a_no_op_on_a_missing_db(tmp_path: Path) -> None:
    # Arrange
    missing = tmp_path / "nope" / "state.db"
    # Act
    undo = rename_rows(missing, OLD, NEW)
    # Assert
    assert undo.total == 0


def test_the_undo_of_a_no_op_is_itself_a_no_op(db: Path) -> None:
    """The rollback path runs on EVERY failed rename, including this one."""
    # Arrange
    undo = rename_rows(db, OLD, NEW)
    before = _tables(db)
    # Act
    undo_rename_rows(undo)
    # Assert
    assert _tables(db) == before
