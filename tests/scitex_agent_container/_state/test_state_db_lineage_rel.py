#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sender_target_relationship`` — the classifier, now over the store.

These tests lived in ``test_state_db_acl_policy.py`` until 2026-08-28, when
``node_comms_policy`` moved to PostgreSQL and this function did not. They
moved here because it read ``lineage``, a different table with a different
owner, still on the old backend at the time.

THAT SENTENCE IS NOW DISCHARGED. ``lineage`` moved the same day, so the
function no longer takes ``db_path`` and no longer reads a file. The tests
stay in this file — the classification is still a distinct question from
the policy that consumes it — but they take ``pg_schema`` now, and the
"measured where the property still lives" note they carried was about a
table that no longer exists.

Real PostgreSQL, no mocks. These SKIP on a host with no writable database.
"""

from __future__ import annotations

from scitex_agent_container._state.state_db_nodes import (
    record_lineage,
    sender_target_relationship,
)


def test_two_children_of_one_parent_are_siblings(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="cap-a", parent="root")
    record_lineage(child="cap-b", parent="root")
    # Act
    rel = sender_target_relationship(sender="cap-a", target="cap-b")
    # Assert
    assert rel == "sibling"


def test_a_child_addressing_its_parent_is_parent(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="cap-a", parent="root")
    # Act
    rel = sender_target_relationship(sender="cap-a", target="root")
    # Assert
    assert rel == "parent"


def test_a_parent_addressing_its_child_is_child(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="cap-a", parent="root")
    # Act
    rel = sender_target_relationship(sender="root", target="cap-a")
    # Assert
    assert rel == "child"


def test_unrelated_nodes_are_other(pg_schema: str) -> None:
    # Arrange — no lineage edges.
    # Act
    rel = sender_target_relationship(sender="cap-a", target="cap-z")
    # Assert
    assert rel == "other"


def test_a_node_addressing_itself_is_self(pg_schema: str) -> None:
    # Arrange
    # Act
    rel = sender_target_relationship(sender="cap-a", target="cap-a")
    # Assert
    assert rel == "self"


def test_an_empty_sender_is_other(pg_schema: str) -> None:
    # Arrange
    # Act
    rel = sender_target_relationship(sender="", target="cap-a")
    # Assert
    assert rel == "other"
