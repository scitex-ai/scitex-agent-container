"""Group-based a2a ACL + developer-group authority (operator 2026-06-25).

The ACL-enforcement half of the group feature, exercised at the
:mod:`scitex_agent_container._listen._acl` decision functions:

* ``check_send_acl`` — a send between two members of the SAME named
  group is allowed (full mesh) even with NO lineage edge and NO grant;
  a cross-group send (different groups, no lineage, no grant) is denied;
  an explicit grant still flips that cross-group deny to allow
  (backward-compatible).
* ``check_spawn`` — a developer-group caller may spawn even though it is
  a (non-root) child, bypassing the root-only default; a non-developer
  child is still denied.
* ``check_lineage_acl`` — a developer-group caller may manage ANY agent
  (stop / restart / delete / status / tail), not just its lineage
  descendants; a non-developer caller with no lineage edge is denied.

Groups are seeded via ``record_comms_policy(group_name=...)`` — the same
row ``agent_start`` writes from the resolved spec label. No mocks: real
on-disk SQLite via the yield-based ``db_path`` env override.

AAA (each marker on its own line), one assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._listen._acl import (
    check_lineage_acl,
    check_send_acl,
    check_spawn,
)
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    grant_send,
    record_comms_policy,
    record_lineage,
)


@pytest.fixture
def db_path(tmp_path: Path):
    # Arrange
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


# ---------------------------------------------------------------------------
# check_send_acl — named-group mesh
# ---------------------------------------------------------------------------


def test_send_allowed_within_same_named_group(db_path: Path) -> None:
    """Same named group, no lineage edge, no grant → allow (full mesh)."""
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    record_comms_policy(name="bob", group_name="developer", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="alice",
        target="bob",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_send_denied_cross_group_without_grant(db_path: Path) -> None:
    """Different named groups, no lineage, no grant → deny."""
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    record_comms_policy(name="carol", group_name="analysts", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="alice",
        target="carol",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_cross_group_deny_carries_cross_group_reason(db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    record_comms_policy(name="carol", group_name="analysts", db_path=db_path)
    # Act
    _decision, reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="alice",
        target="carol",
        db_path=db_path,
    )
    # Assert
    assert reason is not None and "cross-group" in reason


def test_send_allowed_cross_group_with_explicit_grant(db_path: Path) -> None:
    """An explicit grant still flips a cross-group deny to allow."""
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    record_comms_policy(name="carol", group_name="analysts", db_path=db_path)
    grant_send(sender="alice", target="carol", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="alice",
        target="carol",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_ungrouped_pair_still_denied_without_grant(db_path: Path) -> None:
    """Two ungrouped agents do not share a named group → legacy deny."""
    # Arrange — no group_name on either; unrelated lineage families.
    record_lineage(child="x", parent="root-x", db_path=db_path)
    record_lineage(child="y", parent="root-y", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="x",
        claimed_from_agent="x",
        target="y",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# check_send_acl — cross-group mesh among {developer, researcher, generalist}
# (operator 2026-06-27)
# ---------------------------------------------------------------------------


def test_send_allowed_developer_to_researcher(db_path: Path) -> None:
    """Cross-group mesh: developer → researcher, no grant → allow."""
    # Arrange
    record_comms_policy(name="dev-1", group_name="developer", db_path=db_path)
    record_comms_policy(name="res-1", group_name="researcher", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="dev-1",
        claimed_from_agent="dev-1",
        target="res-1",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_send_allowed_researcher_to_generalist(db_path: Path) -> None:
    """Cross-group mesh: researcher → generalist, no grant → allow."""
    # Arrange
    record_comms_policy(name="res-1", group_name="researcher", db_path=db_path)
    record_comms_policy(name="gen-1", group_name="generalist", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="res-1",
        claimed_from_agent="res-1",
        target="gen-1",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_send_allowed_generalist_to_developer_all_directions(db_path: Path) -> None:
    """Mesh is bidirectional: generalist → developer, no grant → allow."""
    # Arrange
    record_comms_policy(name="gen-1", group_name="generalist", db_path=db_path)
    record_comms_policy(name="dev-1", group_name="developer", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="gen-1",
        claimed_from_agent="gen-1",
        target="dev-1",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_send_denied_mesh_group_to_isolated_solver(db_path: Path) -> None:
    """A non-mesh group (solver) stays isolated: developer → solver → deny."""
    # Arrange
    record_comms_policy(name="dev-1", group_name="developer", db_path=db_path)
    record_comms_policy(name="solver-1", group_name="solver", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="dev-1",
        claimed_from_agent="dev-1",
        target="solver-1",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_send_denied_isolated_solver_to_mesh_group(db_path: Path) -> None:
    """Isolation holds in both directions: solver → researcher → deny."""
    # Arrange
    record_comms_policy(name="solver-1", group_name="solver", db_path=db_path)
    record_comms_policy(name="res-1", group_name="researcher", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="solver-1",
        claimed_from_agent="solver-1",
        target="res-1",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# check_spawn — developer-group full authority
# ---------------------------------------------------------------------------


def test_developer_child_may_spawn(db_path: Path) -> None:
    """A developer-group caller may spawn even as a (non-root) child."""
    # Arrange
    record_lineage(child="dev-1", parent="root", db_path=db_path)
    record_comms_policy(name="dev-1", group_name="developer", db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="dev-1", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_non_developer_child_still_denied_spawn(db_path: Path) -> None:
    """Backward-compatible: a non-developer child is still root-only denied."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_comms_policy(name="worker-a", group_name="analysts", db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="worker-a", db_path=db_path)
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# check_lineage_acl — developer-group full agent-CRUD authority
# ---------------------------------------------------------------------------


def test_developer_may_manage_unrelated_agent(db_path: Path) -> None:
    """Developer-group caller manages a target with NO lineage edge."""
    # Arrange
    record_comms_policy(name="dev-1", group_name="developer", db_path=db_path)
    record_lineage(child="victim", parent="someone-else", db_path=db_path)
    # Act
    decision, _reason = check_lineage_acl(
        caller="dev-1", target="victim", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_non_developer_cannot_manage_unrelated_agent(db_path: Path) -> None:
    """Backward-compatible: non-developer with no lineage edge → deny."""
    # Arrange
    record_comms_policy(name="worker-a", group_name="analysts", db_path=db_path)
    record_lineage(child="victim", parent="someone-else", db_path=db_path)
    # Act
    decision, _reason = check_lineage_acl(
        caller="worker-a", target="victim", db_path=db_path
    )
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# check_lineage_acl — standard-fleet MANAGE mesh (operator 2026-06-29
# "agents manage agents"). A caller may manage a target when BOTH resolve
# into the developer / researcher / generalist mesh, with no lineage edge
# and no per-pair grant. This is what lets a researcher restart a developer
# peer via the host listen bypass.
# ---------------------------------------------------------------------------


def test_researcher_may_manage_developer_target_via_mesh(db_path: Path) -> None:
    """Researcher → developer, no lineage, no grant → allow (manage mesh)."""
    # Arrange — neurovista (researcher) restarts scitex-todo (developer).
    record_comms_policy(name="neurovista", group_name="researcher", db_path=db_path)
    record_comms_policy(name="scitex-todo", group_name="developer", db_path=db_path)
    # Act
    decision, _reason = check_lineage_acl(
        caller="neurovista", target="scitex-todo", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_mesh_caller_cannot_manage_isolated_solver_target(db_path: Path) -> None:
    """A non-mesh target (isolated solver) stays unmanageable cross-group.

    Uses a RESEARCHER caller (not developer): the developer group has
    full agent-CRUD authority over ANY target regardless of mesh, so the
    isolation must be probed by a mesh-but-non-developer caller.
    """
    # Arrange — researcher caller, solver target (outside the mesh).
    record_comms_policy(name="res-1", group_name="researcher", db_path=db_path)
    record_comms_policy(name="solver-1", group_name="solver", db_path=db_path)
    record_lineage(child="solver-1", parent="someone-else", db_path=db_path)
    # Act
    decision, _reason = check_lineage_acl(
        caller="res-1", target="solver-1", db_path=db_path
    )
    # Assert
    assert decision == "deny"
