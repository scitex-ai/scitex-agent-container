"""WI-2 — per-node tokens + lineage primitives (handoff §4).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2 "ACL: permissioned
messaging"):

  * "Authenticated sender identity. The ACL check is only as strong
    as the identity — the sender identity carried in
    ``params.metadata`` must be authenticated (per-node credential
    / bearer token), never a self-claimed name."

  * "Group-based default ACL. ... The group is derived from lineage
    — no per-pair config for the common case."

This module exercises the two state.db primitives that make those
requirements possible: per-node bearer tokens (the authenticated
identity) and the lineage edges (parent → child) that derive a
node's group.

No mocks (handoff §0): real SQLite under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    derive_group,
    list_node_tokens,
    mint_node_token,
    record_lineage,
    resolve_node_token,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


# ---------------------------------------------------------------------------
# Schema — node_tokens + lineage tables
# ---------------------------------------------------------------------------


def test_node_tokens_table_exists(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='node_tokens'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


def test_lineage_table_exists(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lineage'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# mint_node_token — generates a fresh secret + records the identity
# ---------------------------------------------------------------------------


def test_mint_node_token_returns_non_empty_string(db_path: Path) -> None:
    # Arrange
    name = "alice"
    # Act
    token = mint_node_token(name=name, db_path=db_path)
    # Assert
    assert isinstance(token, str) and len(token) >= 32


def test_mint_node_token_idempotent_returns_same_token(db_path: Path) -> None:
    """A second call for the same name returns the same token, not a
    new one — re-registration must not invalidate an active bearer.
    """
    # Arrange
    first = mint_node_token(name="alice", db_path=db_path)
    # Act
    second = mint_node_token(name="alice", db_path=db_path)
    # Assert
    assert first == second


def test_mint_node_token_is_unique_per_name(db_path: Path) -> None:
    # Arrange
    a = mint_node_token(name="alice", db_path=db_path)
    b = mint_node_token(name="bob", db_path=db_path)
    # Act
    different = a != b
    # Assert
    assert different is True


# ---------------------------------------------------------------------------
# resolve_node_token — bearer → identity, the auth side of ACL
# ---------------------------------------------------------------------------


def test_resolve_node_token_returns_minted_identity(db_path: Path) -> None:
    # Arrange
    token = mint_node_token(name="alice", db_path=db_path)
    # Act
    resolved = resolve_node_token(token=token, db_path=db_path)
    # Assert
    assert resolved == "alice"


def test_resolve_node_token_returns_none_for_unknown_bearer(db_path: Path) -> None:
    # Arrange
    bogus = "no-such-token-1234567890abcdef"
    # Act
    resolved = resolve_node_token(token=bogus, db_path=db_path)
    # Assert
    assert resolved is None


def test_resolve_node_token_returns_none_for_empty_string(db_path: Path) -> None:
    # Arrange
    empty = ""
    # Act
    resolved = resolve_node_token(token=empty, db_path=db_path)
    # Assert
    assert resolved is None


# ---------------------------------------------------------------------------
# record_lineage — parent → child edges
# ---------------------------------------------------------------------------


def test_record_lineage_persists_parent_pointer(db_path: Path) -> None:
    # Arrange
    record_lineage(child="bob", parent="alice", db_path=db_path)
    # Act
    conn_ctx = state_db.open_db(db_path)
    with conn_ctx as conn:
        row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name='bob'"
        ).fetchone()
    # Assert
    assert row["parent_name"] == "alice"


def test_record_lineage_idempotent_no_duplicate_rows(db_path: Path) -> None:
    """Re-recording the same edge does not duplicate the row."""
    # Arrange
    record_lineage(child="bob", parent="alice", db_path=db_path)
    record_lineage(child="bob", parent="alice", db_path=db_path)
    # Act
    conn_ctx = state_db.open_db(db_path)
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT child_name FROM lineage WHERE child_name='bob'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# derive_group — the heart of the default ACL check
# ---------------------------------------------------------------------------
#
# Per handoff §2: "A parent together with its direct children is one
# group. The group is the unit of default ACL — within a group every
# node may message every other, bidirectionally."
#
# Concretely:
#   * For a parent: group = {parent} ∪ {its direct children}
#   * For a child:  group = {child's parent} ∪ {parent's other children}


def test_derive_group_of_root_with_no_children_is_self_only(db_path: Path) -> None:
    # Arrange — register the node but no children
    mint_node_token(name="root", db_path=db_path)
    # Act
    group = derive_group(name="root", db_path=db_path)
    # Assert
    assert group == {"root"}


def test_derive_group_of_parent_includes_direct_children(db_path: Path) -> None:
    # Arrange
    mint_node_token(name="root", db_path=db_path)
    mint_node_token(name="worker-a", db_path=db_path)
    mint_node_token(name="worker-b", db_path=db_path)
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="root", db_path=db_path)
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_of_child_includes_parent_and_siblings(db_path: Path) -> None:
    """A sibling sees the same group as the parent does — bidirectional."""
    # Arrange
    mint_node_token(name="root", db_path=db_path)
    mint_node_token(name="worker-a", db_path=db_path)
    mint_node_token(name="worker-b", db_path=db_path)
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    group = derive_group(name="worker-a", db_path=db_path)
    # Assert
    assert group == {"root", "worker-a", "worker-b"}


def test_derive_group_excludes_cross_group_nodes(db_path: Path) -> None:
    """A different root's children are not in this group."""
    # Arrange — two unrelated families
    for name in ("root-1", "child-1", "root-2", "child-2"):
        mint_node_token(name=name, db_path=db_path)
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    group = derive_group(name="child-1", db_path=db_path)
    # Assert
    assert group == {"root-1", "child-1"}


def test_derive_group_of_unknown_node_is_singleton(db_path: Path) -> None:
    """A node that does not yet appear in lineage is its own singleton
    group — a fresh registration starts unattached and may only talk
    to itself until lineage is recorded.
    """
    # Arrange
    name = "fresh"
    # Act
    group = derive_group(name=name, db_path=db_path)
    # Assert
    assert group == {"fresh"}


# ---------------------------------------------------------------------------
# list_node_tokens — observability for the host operator
# ---------------------------------------------------------------------------


def test_list_node_tokens_returns_empty_initially(db_path: Path) -> None:
    # Arrange
    # (no tokens minted yet)
    # Act
    rows = list_node_tokens(db_path=db_path)
    # Assert
    assert rows == []


def test_list_node_tokens_returns_each_minted_name(db_path: Path) -> None:
    # Arrange
    mint_node_token(name="alice", db_path=db_path)
    mint_node_token(name="bob", db_path=db_path)
    # Act
    rows = list_node_tokens(db_path=db_path)
    # Assert
    names = sorted(r["name"] for r in rows)
    assert names == ["alice", "bob"]
