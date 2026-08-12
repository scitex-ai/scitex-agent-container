"""Tests for the ``to_home_layers`` sweep plan (``_layers_migration_model``).

The plan IS the dry-run, so these pin what it must not blur: a refusal is a
legitimate named outcome, a malformed edit is a defect, and the two must not
be conflated into one "needs attention" bucket.

STX-NM002: no mocks — pure values in, dataclass out.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._maintenance._layers_migration_model import (
    MigrationPlan,
    SpecEdit,
    count_added_lines,
)


def _edit(agent="a", new_text="x\n", refusal=None, lines_added=1) -> SpecEdit:
    return SpecEdit(
        agent=agent,
        path=Path(f"/specs/{agent}.yaml"),
        layers=("user-shared",),
        new_text=new_text,
        refusal=refusal,
        lines_added=lines_added,
    )


def test_a_clean_edit_will_write() -> None:
    # Arrange
    edit = _edit()
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert plan.writable == (edit,)


def test_a_refused_spec_will_not_write() -> None:
    # Arrange — refused because the editor did not recognise its shape.
    edit = _edit(refusal="no to_home: anchor", new_text=None)
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert plan.writable == ()


def test_a_refused_spec_is_named_not_dropped() -> None:
    # Arrange — silently skipping is how "101 of 102" goes unnoticed.
    edit = _edit(agent="scitex-todo", refusal="no anchor", new_text=None)
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert plan.refused[0].agent == "scitex-todo"


def test_refusals_alone_keep_the_plan_safe_to_apply() -> None:
    # Arrange — a named, counted refusal is a legitimate outcome for a human.
    edit = _edit(refusal="no anchor", new_text=None)
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert plan.safe_to_apply is True


def test_a_multi_line_edit_is_malformed() -> None:
    # Arrange — the editor accepted the shape then produced an unasked-for diff.
    edit = _edit(lines_added=3)
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert plan.malformed == (edit,)


def test_a_malformed_edit_makes_the_plan_unsafe() -> None:
    # Arrange
    edit = _edit(lines_added=3)
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert plan.safe_to_apply is False


def test_a_malformed_edit_is_not_reported_as_a_refusal() -> None:
    # Arrange — conflating them lets a real defect read as "needs attention".
    edit = _edit(lines_added=3)
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert plan.refused == ()


def test_an_unreadable_spec_makes_the_plan_unsafe() -> None:
    # Arrange — the plan does not describe what would actually happen.
    # Act
    plan = MigrationPlan(edits=(), unreadable=("broken",))
    # Assert
    assert plan.safe_to_apply is False


def test_summary_names_the_refused_agent() -> None:
    # Arrange
    edit = _edit(agent="scitex-todo", refusal="no anchor", new_text=None)
    # Act
    plan = MigrationPlan(edits=(edit,))
    # Assert
    assert "scitex-todo" in plan.summary()


def test_a_single_inserted_line_counts_as_one() -> None:
    # Arrange
    before = "a\nb\n"
    after = "a\nNEW\nb\n"
    # Act
    added = count_added_lines(before, after)
    # Assert
    assert added == 1


def test_an_unchanged_file_counts_as_zero() -> None:
    # Arrange
    text = "a\nb\n"
    # Act
    added = count_added_lines(text, text)
    # Assert
    assert added == 0


def test_a_duplicated_line_is_counted_not_ignored() -> None:
    # Arrange — a set difference would call this "no change at all".
    before = "a\nb\n"
    after = "a\na\nb\n"
    # Act
    added = count_added_lines(before, after)
    # Assert
    assert added == 1


def test_a_removed_line_counts_negative() -> None:
    # Arrange — a shrinking file must never look like a clean insert.
    before = "a\nb\n"
    after = "a\n"
    # Act
    added = count_added_lines(before, after)
    # Assert
    assert added == -1
