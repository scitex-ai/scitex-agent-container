"""Tests for :mod:`scitex_agent_container._state._lineage`.

PR-3 Checkpoint 3 — pins the BFS transitive descendants walk used
by the lineage-scoped ACL gate. Real sqlite via the test-isolated
state.db (no mocks, PA-306). AAA + one-assert (PA-307).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state._lineage import (
    ancestors_to_root,
    descendants_of,
)
from scitex_agent_container._state.state_db_nodes import record_lineage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Per-test sqlite — the lineage walk is exercised against a
    fresh, isolated state.db so tests don't leak edges between
    cases."""
    return tmp_path / "state.db"


# ---------------------------------------------------------------------------
# Empty / leaf cases
# ---------------------------------------------------------------------------


def test_descendants_empty_when_node_unknown(db_path: Path) -> None:
    # Arrange — no lineage rows for "ghost".
    # Act
    result = descendants_of(name="ghost", db_path=db_path)
    # Assert
    assert result == set()


def test_descendants_empty_when_node_is_leaf(db_path: Path) -> None:
    # Arrange — register a parent → child edge; "child" itself
    # has no further descendants.
    record_lineage(child="child", parent="root", db_path=db_path)
    # Act
    result = descendants_of(name="child", db_path=db_path)
    # Assert
    assert result == set()


def test_descendants_empty_for_empty_name() -> None:
    # Arrange — guard for the empty-string degenerate case.
    # Act
    result = descendants_of(name="")
    # Assert
    assert result == set()


# ---------------------------------------------------------------------------
# Direct children
# ---------------------------------------------------------------------------


def test_descendants_returns_direct_children(db_path: Path) -> None:
    # Arrange — root has two direct children.
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="bob", parent="root", db_path=db_path)
    # Act
    result = descendants_of(name="root", db_path=db_path)
    # Assert
    assert result == {"alice", "bob"}


def test_descendants_excludes_the_node_itself(db_path: Path) -> None:
    # Arrange — the returned set must NOT include ``name``.
    record_lineage(child="alice", parent="root", db_path=db_path)
    # Act
    result = descendants_of(name="root", db_path=db_path)
    # Assert
    assert "root" not in result


# ---------------------------------------------------------------------------
# Transitive walk
# ---------------------------------------------------------------------------


def test_descendants_walks_grandchildren(db_path: Path) -> None:
    # Arrange — root → alice → ada; "ada" is a grandchild.
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="ada", parent="alice", db_path=db_path)
    # Act
    result = descendants_of(name="root", db_path=db_path)
    # Assert
    assert "ada" in result


def test_descendants_walks_four_levels_deep(db_path: Path) -> None:
    # Arrange — a 4-deep chain.
    record_lineage(child="b", parent="a", db_path=db_path)
    record_lineage(child="c", parent="b", db_path=db_path)
    record_lineage(child="d", parent="c", db_path=db_path)
    record_lineage(child="e", parent="d", db_path=db_path)
    # Act
    result = descendants_of(name="a", db_path=db_path)
    # Assert
    assert result == {"b", "c", "d", "e"}


def test_descendants_walks_branching_tree(db_path: Path) -> None:
    # Arrange — root has two children, each with two grandchildren.
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="bob", parent="root", db_path=db_path)
    record_lineage(child="ada", parent="alice", db_path=db_path)
    record_lineage(child="alex", parent="alice", db_path=db_path)
    record_lineage(child="ben", parent="bob", db_path=db_path)
    record_lineage(child="bea", parent="bob", db_path=db_path)
    # Act
    result = descendants_of(name="root", db_path=db_path)
    # Assert
    assert result == {"alice", "bob", "ada", "alex", "ben", "bea"}


# ---------------------------------------------------------------------------
# Scope isolation — siblings + ancestors are NOT descendants
# ---------------------------------------------------------------------------


def test_descendants_does_not_include_siblings(db_path: Path) -> None:
    # Arrange — alice and bob are siblings under root. From
    # alice's perspective, bob is NOT a descendant.
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="bob", parent="root", db_path=db_path)
    # Act
    result = descendants_of(name="alice", db_path=db_path)
    # Assert
    assert "bob" not in result


def test_descendants_does_not_include_ancestors(db_path: Path) -> None:
    # Arrange — root → alice → ada. From alice's perspective,
    # root is NOT a descendant (it's an ancestor).
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="ada", parent="alice", db_path=db_path)
    # Act
    result = descendants_of(name="alice", db_path=db_path)
    # Assert
    assert "root" not in result


# ---------------------------------------------------------------------------
# Depth ceiling
# ---------------------------------------------------------------------------


def test_descendants_respects_max_depth_bound(db_path: Path) -> None:
    # Arrange — 6-deep chain; ask for max_depth=3 → only first
    # 3 levels of descendants visible.
    record_lineage(child="b", parent="a", db_path=db_path)
    record_lineage(child="c", parent="b", db_path=db_path)
    record_lineage(child="d", parent="c", db_path=db_path)
    record_lineage(child="e", parent="d", db_path=db_path)
    record_lineage(child="f", parent="e", db_path=db_path)
    record_lineage(child="g", parent="f", db_path=db_path)
    # Act
    result = descendants_of(name="a", db_path=db_path, max_depth=3)
    # Assert
    assert result == {"b", "c", "d"}


# ---------------------------------------------------------------------------
# Upward walk — ancestors_to_root (sac #404 verdict climb)
# ---------------------------------------------------------------------------


def test_ancestors_empty_when_node_has_no_parent(db_path: Path) -> None:
    # Arrange — no lineage rows for "lonely".
    # Act
    chain = ancestors_to_root(name="lonely", db_path=db_path)
    # Assert
    assert chain == []


def test_ancestors_single_parent_returns_one_element_chain(db_path: Path) -> None:
    # Arrange
    record_lineage(child="kid", parent="mom", db_path=db_path)
    # Act
    chain = ancestors_to_root(name="kid", db_path=db_path)
    # Assert
    assert chain == ["mom"]


def test_ancestors_chain_is_ordered_parent_first_root_last(db_path: Path) -> None:
    # Arrange — pusher → parent → grandparent → lead.
    record_lineage(child="pusher", parent="parent", db_path=db_path)
    record_lineage(child="parent", parent="grandparent", db_path=db_path)
    record_lineage(child="grandparent", parent="lead", db_path=db_path)
    # Act
    chain = ancestors_to_root(name="pusher", db_path=db_path)
    # Assert
    assert chain == ["parent", "grandparent", "lead"]


def test_ancestors_chain_excludes_the_queried_agent(db_path: Path) -> None:
    # Arrange
    record_lineage(child="a", parent="b", db_path=db_path)
    # Act
    chain = ancestors_to_root(name="a", db_path=db_path)
    # Assert
    assert "a" not in chain


def test_ancestors_cycle_in_hand_edited_db_is_bounded(db_path: Path) -> None:
    # Arrange — record_lineage prevents cycles, so inject one directly to
    # prove the depth guard (same "cannot trust the DB blindly" rationale
    # as descendants_of). record_lineage first creates the table.
    import sqlite3

    record_lineage(child="x", parent="y", db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lineage (child_name, parent_name, created_at) "
            "VALUES ('y', 'x', 0.0)"
        )
        conn.commit()
    # Act
    chain = ancestors_to_root(name="x", db_path=db_path, max_depth=5)
    # Assert
    assert len(chain) <= 5
