"""Deterministic card-hygiene validator (blocker-validity)."""

from scitex_agent_container._lifecycle._card_hygiene import (
    BLOCKED_NO_BLOCKER,
    SELF_BLOCK,
    VOID_BLOCKER,
    audit_tasks,
)


def test_self_owned_blocker_is_flagged_as_self_block():
    # Arrange
    tasks = [
        {"id": "a", "assignee": "me", "status": "in_progress", "depends_on": ["b"]},
        {"id": "b", "assignee": "me", "status": "in_progress"},
    ]
    # Act
    rules = {v.rule for v in audit_tasks(tasks) if v.card_id == "a"}
    # Assert
    assert SELF_BLOCK in rules


def test_blocker_owned_by_other_agent_is_clean():
    # Arrange
    tasks = [
        {"id": "a", "assignee": "me", "status": "blocked", "depends_on": ["b"]},
        {"id": "b", "assignee": "other", "status": "in_progress"},
    ]
    # Act
    violations = audit_tasks(tasks)
    # Assert
    assert violations == []


def test_terminal_blocker_is_flagged_as_void():
    # Arrange
    tasks = [
        {"id": "a", "assignee": "me", "status": "blocked", "depends_on": ["b"]},
        {"id": "b", "assignee": "other", "status": "cancelled"},
    ]
    # Act
    rules = {v.rule for v in audit_tasks(tasks) if v.card_id == "a"}
    # Assert
    assert VOID_BLOCKER in rules


def test_blocked_status_without_any_blocker_is_flagged():
    # Arrange
    tasks = [{"id": "a", "assignee": "me", "status": "blocked"}]
    # Act
    rules = {v.rule for v in audit_tasks(tasks)}
    # Assert
    assert BLOCKED_NO_BLOCKER in rules


def test_agent_filter_limits_to_that_owner():
    # Arrange
    tasks = [
        {"id": "a", "assignee": "me", "status": "in_progress", "depends_on": ["x"]},
        {"id": "x", "assignee": "me", "status": "in_progress"},
        {"id": "c", "assignee": "other", "status": "in_progress", "depends_on": ["y"]},
        {"id": "y", "assignee": "other", "status": "in_progress"},
    ]
    # Act
    card_ids = {v.card_id for v in audit_tasks(tasks, agent="me")}
    # Assert
    assert card_ids == {"a"}


def test_free_form_blocker_reason_is_not_treated_as_a_card():
    # Arrange: "operator-decision" is a declared reason, not a card id in the board.
    tasks = [{"id": "a", "assignee": "me", "status": "blocked", "blocker": "operator-decision"}]
    # Act
    violations = audit_tasks(tasks)
    # Assert
    assert violations == []
