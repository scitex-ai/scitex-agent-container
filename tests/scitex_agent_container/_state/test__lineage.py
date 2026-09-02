"""Tests for :mod:`scitex_agent_container._state._lineage`.

PR-3 Checkpoint 3 — pins the BFS transitive descendants walk used
by the lineage-scoped ACL gate. AAA + one-assert (PA-307).

REAL POSTGRESQL SINCE 2026-08-28, not a temp local file. The walk reads
the shared lineage store, so isolation comes from ``pg_schema`` pointing
``SCITEX_STORE_DSN`` at a throwaway schema — which is stronger than the
``tmp_path`` these tests used to take, because it exercises the real
resolver instead of a path threaded past it. Still no mocks (PA-306).

These SKIP on a host with no writable database, which includes every agent
container (loopback there is a read-only replica of the fleet cluster).
"""

from __future__ import annotations

from scitex_agent_container._state._lineage import (
    ancestors_to_root,
    descendants_of,
)
from scitex_agent_container._state.state_db_nodes import record_lineage


# ---------------------------------------------------------------------------
# Empty / leaf cases
# ---------------------------------------------------------------------------


def test_descendants_empty_when_node_unknown(pg_schema: str) -> None:
    # Arrange — no lineage rows for "ghost".
    # Act
    result = descendants_of(name="ghost")
    # Assert
    assert result == set()


def test_descendants_empty_when_node_is_leaf(pg_schema: str) -> None:
    # Arrange — register a parent → child edge; "child" itself
    # has no further descendants.
    record_lineage(child="child", parent="root")
    # Act
    result = descendants_of(name="child")
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


def test_descendants_returns_direct_children(pg_schema: str) -> None:
    # Arrange — root has two direct children.
    record_lineage(child="alice", parent="root")
    record_lineage(child="bob", parent="root")
    # Act
    result = descendants_of(name="root")
    # Assert
    assert result == {"alice", "bob"}


def test_descendants_excludes_the_node_itself(pg_schema: str) -> None:
    # Arrange — the returned set must NOT include ``name``.
    record_lineage(child="alice", parent="root")
    # Act
    result = descendants_of(name="root")
    # Assert
    assert "root" not in result


# ---------------------------------------------------------------------------
# Transitive walk
# ---------------------------------------------------------------------------


def test_descendants_walks_grandchildren(pg_schema: str) -> None:
    # Arrange — root → alice → ada; "ada" is a grandchild.
    record_lineage(child="alice", parent="root")
    record_lineage(child="ada", parent="alice")
    # Act
    result = descendants_of(name="root")
    # Assert
    assert "ada" in result


def test_descendants_walks_four_levels_deep(pg_schema: str) -> None:
    # Arrange — a 4-deep chain.
    record_lineage(child="b", parent="a")
    record_lineage(child="c", parent="b")
    record_lineage(child="d", parent="c")
    record_lineage(child="e", parent="d")
    # Act
    result = descendants_of(name="a")
    # Assert
    assert result == {"b", "c", "d", "e"}


def test_descendants_walks_branching_tree(pg_schema: str) -> None:
    # Arrange — root has two children, each with two grandchildren.
    record_lineage(child="alice", parent="root")
    record_lineage(child="bob", parent="root")
    record_lineage(child="ada", parent="alice")
    record_lineage(child="alex", parent="alice")
    record_lineage(child="ben", parent="bob")
    record_lineage(child="bea", parent="bob")
    # Act
    result = descendants_of(name="root")
    # Assert
    assert result == {"alice", "bob", "ada", "alex", "ben", "bea"}


# ---------------------------------------------------------------------------
# Scope isolation — siblings + ancestors are NOT descendants
# ---------------------------------------------------------------------------


def test_descendants_does_not_include_siblings(pg_schema: str) -> None:
    # Arrange — alice and bob are siblings under root. From
    # alice's perspective, bob is NOT a descendant.
    record_lineage(child="alice", parent="root")
    record_lineage(child="bob", parent="root")
    # Act
    result = descendants_of(name="alice")
    # Assert
    assert "bob" not in result


def test_descendants_does_not_include_ancestors(pg_schema: str) -> None:
    # Arrange — root → alice → ada. From alice's perspective,
    # root is NOT a descendant (it's an ancestor).
    record_lineage(child="alice", parent="root")
    record_lineage(child="ada", parent="alice")
    # Act
    result = descendants_of(name="alice")
    # Assert
    assert "root" not in result


# ---------------------------------------------------------------------------
# Depth ceiling
# ---------------------------------------------------------------------------


def test_descendants_respects_max_depth_bound(pg_schema: str) -> None:
    # Arrange — 6-deep chain; ask for max_depth=3 → only first
    # 3 levels of descendants visible.
    record_lineage(child="b", parent="a")
    record_lineage(child="c", parent="b")
    record_lineage(child="d", parent="c")
    record_lineage(child="e", parent="d")
    record_lineage(child="f", parent="e")
    record_lineage(child="g", parent="f")
    # Act
    result = descendants_of(name="a", max_depth=3)
    # Assert
    assert result == {"b", "c", "d"}


# ---------------------------------------------------------------------------
# Upward walk — ancestors_to_root (sac #404 verdict climb)
# ---------------------------------------------------------------------------


def test_ancestors_empty_when_node_has_no_parent(pg_schema: str) -> None:
    # Arrange — no lineage rows for "lonely".
    # Act
    chain = ancestors_to_root(name="lonely")
    # Assert
    assert chain == []


def test_ancestors_single_parent_returns_one_element_chain(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="kid", parent="mom")
    # Act
    chain = ancestors_to_root(name="kid")
    # Assert
    assert chain == ["mom"]


def test_ancestors_chain_is_ordered_parent_first_root_last(pg_schema: str) -> None:
    # Arrange — pusher → parent → grandparent → lead.
    record_lineage(child="pusher", parent="parent")
    record_lineage(child="parent", parent="grandparent")
    record_lineage(child="grandparent", parent="lead")
    # Act
    chain = ancestors_to_root(name="pusher")
    # Assert
    assert chain == ["parent", "grandparent", "lead"]


def test_ancestors_chain_excludes_the_queried_agent(pg_schema: str) -> None:
    # Arrange
    record_lineage(child="a", parent="b")
    # Act
    chain = ancestors_to_root(name="a")
    # Assert
    assert "a" not in chain


def test_ancestors_cycle_is_bounded(pg_schema: str) -> None:
    # Arrange — two edges that point at each other. This used to be an
    # ``INSERT OR REPLACE`` that bent an existing row into a loop; against
    # the store that is impossible, because ``parent_name`` is IMMUTABLE
    # and the first value is kept forever. A cycle can therefore only
    # arrive as two SEPARATE edges, which is what this builds — a more
    # faithful reproduction of the only shape the guard can ever meet.
    record_lineage(child="x", parent="y")
    record_lineage(child="y", parent="x")
    # Act
    chain = ancestors_to_root(name="x", max_depth=5)
    # Assert
    assert len(chain) <= 5


def test_descendants_cycle_is_bounded(pg_schema: str) -> None:
    # Arrange — the DOWN walk over the same mutually-pointing pair. It had
    # no cycle test at all before, because the local-file version could only be
    # given a cycle by hand-editing the file.
    record_lineage(child="x", parent="y")
    record_lineage(child="y", parent="x")
    # Act
    found = descendants_of(name="x", max_depth=5)
    # Assert
    assert found == {"y"}
