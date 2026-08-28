"""The ``instances`` rename step, and its key-scoped inverse.

WHY THIS IS A STEP AND NOT THREE MORE ``NAME_COLUMNS`` PAIRS
============================================================
``_rename_db.rename_rows`` SKIPS a table absent from ``sqlite_master``. So
after ``instances`` moved to PostgreSQL, leaving ``("instances", "name")``,
``("instances", "spawned_by")`` and ``("instances", "workdir")`` in its
tables would have made the rename report SUCCESS while every record kept the
old name — the same silent no-op that retired the ``comms_nodes`` and
``node_comms_policy`` pairs before them, with a worse consequence: the start
PREFLIGHT reads these records, so an agent renamed that way would look
un-started under its new name and a second copy would be launched.

THE INVERSE IS KEY-SCOPED, NOT "the same verb with the arguments swapped"
=========================================================================
``rename_comms_node``'s undo can be the verb reversed because the NAME is
that record's identity. Here the identity is ``{id, host}`` and the name is
data, so running the rename backwards would also rewrite records that
legitimately held the NEW name before this rename ran — the recorded
lifetimes of a previously deleted agent by that name. ``_rename_db`` captures
rowids for exactly this reason; this captures identities.

Needs a real PostgreSQL: ``pg_schema`` is the shared opt-in fixture.
"""

from __future__ import annotations

from scitex_agent_container._state.state_db_instances import (
    last_known_instance,
    read_instance,
    record_instance_start,
)
from scitex_agent_container._state.state_db_instances_rename import (
    count_instance_rename_rows,
    rename_instance_rows,
    undo_rename_instance_rows,
)


def test_the_rename_moves_the_name(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start("old", host="host-a")
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert read_instance(instance_id)["name"] == "new"


def test_the_renamed_record_is_findable_under_the_new_name(pg_schema: str) -> None:
    # Arrange — the consequence that matters: ``last_known_instance`` and
    # ``list_active_instances`` key on the name, and five readers sit on top.
    record_instance_start("old", host="host-a")
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert last_known_instance("new") is not None


def test_the_renamed_record_is_no_longer_findable_under_the_old_name(
    pg_schema: str,
) -> None:
    # Arrange — a record left behind under the old name is what makes the
    # preflight start a SECOND copy of a running agent.
    record_instance_start("old", host="host-a")
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert last_known_instance("old") is None


def test_the_rename_keeps_the_record_identity(pg_schema: str) -> None:
    # Arrange — a renamed agent is the SAME agent and its recorded lifetimes
    # are the same lifetimes. This is the difference from
    # ``rename_comms_node``, where a rename ends one record and begins another.
    instance_id = record_instance_start("old", host="host-a")
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert read_instance(instance_id) is not None


def test_the_rename_moves_the_lineage_edge_on_a_child(pg_schema: str) -> None:
    # Arrange — THE reason ``spawned_by`` had to stop being IMMUTABLE. Under
    # IMMUTABLE the store keeps the first value and reports a MergeConflict
    # in ``PutResult.conflicts``, which no caller reads — so this assertion
    # would fail while the rename reported success.
    child = record_instance_start("child", host="host-a", spawned_by="old")
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert read_instance(child)["spawned_by"] == "new"


def test_the_rename_rewrites_the_workdir_component(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(
        "old", host="host-a", workdir="/home/u/proj/old"
    )
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert read_instance(instance_id)["workdir"] == "/home/u/proj/new"


def test_the_rename_leaves_a_merely_containing_component_alone(
    pg_schema: str,
) -> None:
    # Arrange — ``sub_path`` replaces WHOLE components, which is what keeps
    # the rewrite from mangling ``…/old-archive/…``. Same helper the SQLite
    # path used, so the behaviour cannot drift between the two.
    instance_id = record_instance_start(
        "old", host="host-a", workdir="/home/u/proj/old-archive"
    )
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert read_instance(instance_id)["workdir"] == "/home/u/proj/old-archive"


def test_an_unrelated_record_is_untouched(pg_schema: str) -> None:
    # Arrange
    other = record_instance_start("other", host="host-a")
    record_instance_start("old", host="host-a")
    # Act
    rename_instance_rows(old="old", new="new")
    # Assert
    assert read_instance(other)["name"] == "other"


def test_a_rename_to_the_same_name_writes_nothing(pg_schema: str) -> None:
    # Arrange
    record_instance_start("old", host="host-a")
    # Act
    undo = rename_instance_rows(old="old", new="old")
    # Assert
    assert undo.total == 0


def test_the_inverse_restores_the_name(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start("old", host="host-a")
    undo = rename_instance_rows(old="old", new="new")
    # Act
    undo_rename_instance_rows(undo)
    # Assert
    assert read_instance(instance_id)["name"] == "old"


def test_the_inverse_restores_the_lineage_edge(pg_schema: str) -> None:
    # Arrange
    child = record_instance_start("child", host="host-a", spawned_by="old")
    undo = rename_instance_rows(old="old", new="new")
    # Act
    undo_rename_instance_rows(undo)
    # Assert
    assert read_instance(child)["spawned_by"] == "old"


def test_the_inverse_restores_the_workdir(pg_schema: str) -> None:
    # Arrange
    instance_id = record_instance_start(
        "old", host="host-a", workdir="/home/u/proj/old"
    )
    undo = rename_instance_rows(old="old", new="new")
    # Act
    undo_rename_instance_rows(undo)
    # Assert
    assert read_instance(instance_id)["workdir"] == "/home/u/proj/old"


def test_the_inverse_leaves_a_prior_holder_of_the_new_name_alone(
    pg_schema: str,
) -> None:
    # Arrange — THE reason the undo is key-scoped. ``incumbent`` already
    # answered to ``new`` before the rename ran (the recorded lifetime of a
    # previously deleted agent by that name). Running the rename backwards
    # would rename it to ``old`` and destroy a record this operation never
    # touched.
    incumbent = record_instance_start("new", host="host-a")
    record_instance_start("old", host="host-a")
    undo = rename_instance_rows(old="old", new="new")
    # Act
    undo_rename_instance_rows(undo)
    # Assert
    assert read_instance(incumbent)["name"] == "new"


def test_undoing_an_empty_rename_is_a_no_op(pg_schema: str) -> None:
    # Arrange — the rollback stack calls the inverse unconditionally when a
    # later step fails, so an empty one must not raise.
    undo = rename_instance_rows(old="ghost", new="phantom")
    # Act
    undo_rename_instance_rows(undo)
    # Assert
    assert undo.total == 0


def test_the_dry_run_count_reports_every_touched_field(pg_schema: str) -> None:
    # Arrange — what ``sac agents rename --dry-run`` prints. Reported under
    # the same ``table.column`` keys the SQLite counts use, so the operator
    # reads ONE list and a zero means the same thing everywhere in it.
    record_instance_start("old", host="host-a", workdir="/home/u/proj/old")
    record_instance_start("child", host="host-a", spawned_by="old")
    # Act
    counts = count_instance_rename_rows(old="old")
    # Assert
    assert counts == {
        "instances.name": 1,
        "instances.spawned_by": 1,
        "instances.workdir": 1,
    }


def test_the_dry_run_count_writes_nothing(pg_schema: str) -> None:
    # Arrange — a preview that mutated would be the worst possible shape for
    # a flag whose whole purpose is "show me what would happen".
    instance_id = record_instance_start("old", host="host-a")
    # Act
    count_instance_rename_rows(old="old")
    # Assert
    assert read_instance(instance_id)["name"] == "old"


def test_the_rename_db_tables_no_longer_name_instances() -> None:
    # Arrange — the pairs had to LEAVE, not merely stop matching:
    # ``rename_rows`` skips an absent table, so a pair left behind is a
    # silent no-op that still reports success.
    from scitex_agent_container._lifecycle._rename_db import (
        NAME_COLUMNS,
        PATH_COLUMNS,
    )

    # Act
    tables = {table for table, _column in (*NAME_COLUMNS, *PATH_COLUMNS)}
    # Assert
    assert "instances" not in tables


def test_the_rename_flow_runs_the_instances_step() -> None:
    # Arrange — a step that exists but is never called is the same silent
    # no-op wearing a different hat.
    from scitex_agent_container._lifecycle import _rename

    # Act
    ordered = list(_rename.STEPS)
    # Assert
    assert _rename.STEP_INSTANCES in ordered
