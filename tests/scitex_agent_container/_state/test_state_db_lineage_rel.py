#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sender_target_relationship`` — the SQLite half that did NOT move.

These tests lived in ``test_state_db_acl_policy.py`` until 2026-08-28, when
``node_comms_policy`` moved to PostgreSQL and this function did not. They are
here, not there, for the reason the function itself moved modules: it reads
``lineage``, a different table with a different owner, still on SQLite and
still taking ``db_path``.

MEASURED WHERE THE PROPERTY STILL LIVES, which is the point. Leaving them in
a file whose subject is now a PostgreSQL store would have made them read as
coverage of the migrated code; they are not. They cover the table that stayed
behind, and they will move again when it moves.

Real SQLite, no mocks — the same isolation these tests always had.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    record_lineage,
    sender_target_relationship,
)


@pytest.fixture
def db_path(tmp_path: Path):
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


def test_two_children_of_one_parent_are_siblings(db_path: Path) -> None:
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    record_lineage(child="cap-b", parent="root", db_path=db_path)
    # Act
    rel = sender_target_relationship(sender="cap-a", target="cap-b", db_path=db_path)
    # Assert
    assert rel == "sibling"


def test_a_child_addressing_its_parent_is_parent(db_path: Path) -> None:
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    # Act
    rel = sender_target_relationship(sender="cap-a", target="root", db_path=db_path)
    # Assert
    assert rel == "parent"


def test_a_parent_addressing_its_child_is_child(db_path: Path) -> None:
    # Arrange
    record_lineage(child="cap-a", parent="root", db_path=db_path)
    # Act
    rel = sender_target_relationship(sender="root", target="cap-a", db_path=db_path)
    # Assert
    assert rel == "child"


def test_unrelated_nodes_are_other(db_path: Path) -> None:
    # Arrange — no lineage edges.
    # Act
    rel = sender_target_relationship(sender="cap-a", target="cap-z", db_path=db_path)
    # Assert
    assert rel == "other"


def test_a_node_addressing_itself_is_self(db_path: Path) -> None:
    # Arrange
    # Act
    rel = sender_target_relationship(sender="cap-a", target="cap-a", db_path=db_path)
    # Assert
    assert rel == "self"


def test_an_empty_sender_is_other(db_path: Path) -> None:
    # Arrange
    # Act
    rel = sender_target_relationship(sender="", target="cap-a", db_path=db_path)
    # Assert
    assert rel == "other"
