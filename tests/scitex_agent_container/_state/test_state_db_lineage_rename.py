#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``rename_lineage`` — what moves, and what the schema will not let move.

The lineage half of the agent-rename flow, replacing the two
``NAME_COLUMNS`` pairs that renamed the SQLite table until 2026-08-28.

The asymmetry is the whole subject of this file. ``child_name`` is the
store IDENTITY, so an agent's OWN edge can be moved (copy onto the new
identity, retire the old). ``parent_name`` is IMMUTABLE, so the edges that
name the agent as a PARENT cannot be re-pointed at all — the first value is
kept forever, and ``hide``/``unhide`` append ops carrying no values, so
even a retire-and-restore comes back with its field stamps intact.

That leaves three possible behaviours for renaming an agent that has
children, and two of them are silent privilege escalations:

  * leave the children's edges pointing at a name nothing answers to →
    each child resolves to NO parent → each child is a ROOT → each child
    may spawn;
  * hide those edges instead → identical outcome, reached deliberately;
  * REFUSE. Which is what it does, and what these tests pin.

Real PostgreSQL via ``pg_schema``, no mocks. SKIPs on a host with no
writable database, which includes every agent container.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_lineage_group import record_lineage
from scitex_agent_container._state.state_db_lineage_rename import (
    LineageRenameError,
    rename_lineage,
)
from scitex_agent_container._state.state_db_lineage_store import (
    new_lineage_store,
    read_edges,
    reset_lineage_store,
)


@pytest.fixture(autouse=True)
def _drop_cached_handle():
    """Start and end with an empty process-wide handle cache."""
    reset_lineage_store()
    yield
    reset_lineage_store()


# ---------------------------------------------------------------------------
# The half that moves: an agent's own edge.
# ---------------------------------------------------------------------------


@pytest.fixture
def renamed_leaf(pg_schema: str):
    """A childless agent renamed. Yields ``(moved, created_at_before)``."""
    record_lineage(child="old-name", parent="root")
    store = new_lineage_store()
    try:
        before = float(store.get({"child_name": "old-name"}).values["created_at"])
    finally:
        store.close()
    moved = rename_lineage(old="old-name", new="new-name")
    yield moved, before


def test_renaming_a_leaf_reports_that_an_edge_moved(renamed_leaf) -> None:
    # Arrange
    moved, _before = renamed_leaf
    # Act
    result = moved
    # Assert
    assert result is True


def test_the_new_name_inherits_the_parent(renamed_leaf) -> None:
    # Arrange
    _moved, _before = renamed_leaf
    # Act
    parent = read_edges().parent("new-name")
    # Assert
    assert parent == "root"


def test_the_old_name_no_longer_resolves_to_a_parent(renamed_leaf) -> None:
    """The escalation this step exists to prevent, checked directly.

    An edge left behind under the old name is not the danger; an edge
    MISSING under the live name is, because no parent means ROOT and a root
    may spawn. This asserts the old identity stopped answering.
    """
    # Arrange
    _moved, _before = renamed_leaf
    # Act
    parent = read_edges().parent("old-name")
    # Assert
    assert parent is None


def test_created_at_travels_verbatim(renamed_leaf) -> None:
    """The spawn happened when it happened; a rename is not a re-spawn."""
    # Arrange
    _moved, before = renamed_leaf
    store = new_lineage_store()
    # Act
    try:
        after = float(store.get({"child_name": "new-name"}).values["created_at"])
    finally:
        store.close()
    # Assert
    assert after == before


def test_the_old_identity_is_hidden_not_hard_deleted(renamed_leaf) -> None:
    """Nothing is ever hard-deleted, so the history stays auditable."""
    # Arrange
    _moved, _before = renamed_leaf
    store = new_lineage_store()
    # Act
    try:
        hidden = store.is_hidden({"child_name": "old-name"})
    finally:
        store.close()
    # Assert
    assert hidden is True


def test_renaming_an_unknown_name_is_a_no_op(pg_schema: str) -> None:
    # Arrange
    old, new = "never-existed", "whatever"
    # Act
    moved = rename_lineage(old=old, new=new)
    # Assert
    assert moved is False


def test_the_rename_round_trips_through_its_inverse(pg_schema: str) -> None:
    """The undo the rename flow pushes is the same verb, arguments swapped."""
    # Arrange
    record_lineage(child="alpha", parent="root")
    # Act
    rename_lineage(old="alpha", new="beta")
    rename_lineage(old="beta", new="alpha")
    # Assert
    assert read_edges().parent("alpha") == "root"


# ---------------------------------------------------------------------------
# The half that cannot move: edges naming this agent as the PARENT.
# ---------------------------------------------------------------------------


@pytest.fixture
def refused_rename(pg_schema: str):
    """Try to rename an agent that HAS children. Yields the refusal."""
    record_lineage(child="kid-a", parent="the-parent")
    record_lineage(child="kid-b", parent="the-parent")
    record_lineage(child="the-parent", parent="grandparent")
    try:
        rename_lineage(old="the-parent", new="renamed-parent")
    except LineageRenameError as exc:
        yield exc
        return
    pytest.fail("rename_lineage accepted a rename it cannot carry out")


def test_renaming_a_parent_is_refused(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="kid-a", parent="the-parent")
    # Act
    # (the rename is the act; it must refuse rather than half-succeed)
    # Assert
    with pytest.raises(LineageRenameError):
        rename_lineage(old="the-parent", new="renamed-parent")


def test_the_refusal_names_the_blocking_children(refused_rename) -> None:
    """A refusal must say WHAT it saw, not merely that it refused."""
    # Arrange
    message = str(refused_rename)
    # Act
    named = "kid-a" in message and "kid-b" in message
    # Assert
    assert named is True


def test_the_refusal_explains_that_parent_name_is_immutable(
    refused_rename,
) -> None:
    # Arrange
    message = str(refused_rename)
    # Act
    explained = "IMMUTABLE" in message
    # Assert
    assert explained is True


def test_a_refused_rename_leaves_the_children_pointing_at_the_old_name(
    refused_rename,
) -> None:
    """Refused BEFORE any write, so there is nothing for the unwind to undo."""
    # Arrange
    _exc = refused_rename
    # Act
    edges = read_edges()
    # Assert
    assert edges.parent("kid-a") == "the-parent"


def test_a_refused_rename_leaves_the_parents_own_edge_alone(
    refused_rename,
) -> None:
    # Arrange
    _exc = refused_rename
    # Act
    edges = read_edges()
    # Assert
    assert edges.parent("the-parent") == "grandparent"


def test_a_refused_rename_creates_nothing_under_the_new_name(
    refused_rename,
) -> None:
    # Arrange
    _exc = refused_rename
    # Act
    edges = read_edges()
    # Assert
    assert edges.parent("renamed-parent") is None

# EOF
