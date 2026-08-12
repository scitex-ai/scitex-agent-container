"""Group-based a2a ACL + developer-group authority (operator 2026-06-25).

The ACL-enforcement half of the group feature, exercised at the
:mod:`scitex_agent_container._listen._acl` decision functions:

* ``check_send_acl`` — a send between two members of the SAME named
  group is allowed (full mesh) even with NO lineage edge and NO grant.
  Cross-group messaging is DEFAULT-ALLOW (operator 2026-07-03): a send
  between different groups with no lineage / mesh / grant is now allowed
  (messaging is collaboration, not a security boundary). The overrides
  that STILL deny are preserved — an explicit ``block_send`` yields a
  "block" decision, and a per-spec ``spec.comms`` deny yields "deny".
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
from scitex_agent_container._state.state_db_blocks import block_send
from scitex_agent_container._state.state_db_nodes import (
    grant_send,
    record_comms_policy,
    record_lineage,
)
from scitex_agent_container.config._group_resolver import (
    all_named_groups,
    group_from_labels,
)


def _seed_child_from_labels(
    name: str,
    labels: dict,
    *,
    db_path: Path,
    may_spawn: bool = True,
) -> None:
    """Seed a non-root agent EXACTLY as ``agent_start`` would.

    Walks the real production path — ``metadata.labels`` → the resolvers →
    ``node_comms_policy`` — rather than hand-writing a resolved group into
    the DB. That is what makes these tests able to catch a gap in the
    RESOLVER (a role that derives no group) and not just in the gate.

    BOTH projections are written, exactly like ``persist_acl_policy``:
    ``group_name`` (PRIMARY, the mesh bucket) and ``group_names`` (the FULL
    set the authority gates read). Seeding only the primary is what this
    helper used to do, and it is precisely the reduction that hid the
    2026-08-10 defect — a helper that no longer mirrors production stops
    being able to catch a production bug.
    """
    record_lineage(child=name, parent="root-parent", db_path=db_path)
    record_comms_policy(
        name=name,
        group_name=group_from_labels(labels),
        group_names=all_named_groups(labels),
        may_spawn=may_spawn,
        db_path=db_path,
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


def test_send_allowed_cross_group_by_default(db_path: Path) -> None:
    """Messaging default-allow (operator 2026-07-03): different named
    groups, no lineage, no grant → ALLOW. Cross-group messaging is
    collaboration, not a security boundary."""
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
    assert decision == "allow"


def test_send_blocked_sender_still_denied_cross_group(db_path: Path) -> None:
    """Override preserved: an explicit block still denies even though the
    cross-group default is now allow."""
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db_path)
    record_comms_policy(name="carol", group_name="analysts", db_path=db_path)
    block_send(sender="alice", target="carol", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="alice",
        target="carol",
        db_path=db_path,
    )
    # Assert
    assert decision == "block"


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


def test_ungrouped_pair_allowed_by_default(db_path: Path) -> None:
    """Messaging default-allow: two ungrouped agents in unrelated lineage
    families may now message each other with no grant."""
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
    assert decision == "allow"


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


def test_send_allowed_mesh_group_to_non_mesh_group(db_path: Path) -> None:
    """Messaging default-allow: a non-mesh group is no longer a MESSAGING
    boundary — developer → solver-group now ALLOWS. (Group-based isolation
    still gates PRIVILEGED actions via check_lineage_acl; a solver that must
    reject inbound messages uses per-spec spec.comms.inbound=deny.)"""
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
    assert decision == "allow"


def test_send_allowed_non_mesh_group_to_mesh_group(db_path: Path) -> None:
    """Messaging default-allow, both directions: solver-group → researcher
    now ALLOWS (the exact cross-group case PR #12/#524's mesh could not
    cover for paper-group agents a2a-ing developer agents)."""
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
    assert decision == "allow"


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
# check_spawn — MULTI-GROUP labels, seeded from real spec labels through the
# real resolvers (incident 2026-08-10, grant).
#
# grant's spec authors five groups and `developer` is NOT the first. Every
# gate resolved only the first element, so the same listen server answered
# `a2a_peers` with all five and `check_spawn` with "is in none of the
# developer, research, or privileged groups". Authority hung on YAML list
# ORDER. These go through `_seed_child_from_labels`, so a future reduction
# anywhere in labels -> resolver -> DB -> gate fails here, not in production.
# ---------------------------------------------------------------------------

GRANT_LABELS = {
    "role": "project-maintainer",
    "groups": ["generalist", "privileged", "developer", "researcher", "active"],
}


def test_child_with_developer_late_in_its_groups_may_spawn(db_path: Path) -> None:
    """THE reported bug, end-to-end from grant's real spec labels."""
    # Arrange
    _seed_child_from_labels("grant", GRANT_LABELS, db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="grant", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_child_with_developer_late_in_its_groups_may_manage_peers(
    db_path: Path,
) -> None:
    """The same membership question on the MANAGE gate (tail / stop /
    restart), which read the identical reduced group."""
    # Arrange
    _seed_child_from_labels("grant", GRANT_LABELS, db_path=db_path)
    # Act
    decision, _reason = check_lineage_acl(
        caller="grant", target="unrelated-peer", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_child_with_researcher_late_in_its_groups_may_spawn(db_path: Path) -> None:
    # Arrange
    labels = {"role": "worker", "groups": ["generalist", "researcher"]}
    _seed_child_from_labels("nv", labels, db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="nv", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_child_with_privileged_late_in_its_groups_may_spawn(db_path: Path) -> None:
    # Arrange
    labels = {"role": "worker", "groups": ["generalist", "privileged"]}
    _seed_child_from_labels("dotfiles", labels, db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="dotfiles", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_multi_group_child_with_no_authorising_group_is_still_denied(
    db_path: Path,
) -> None:
    """The gate must still SHUT — a set-valued read that stopped denying
    would be a worse bug than the one it fixes."""
    # Arrange
    labels = {"role": "worker", "groups": ["generalist", "active", "analysts"]}
    _seed_child_from_labels("worker-b", labels, db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="worker-b", db_path=db_path)
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# check_spawn / check_lineage_acl — BOTH privileged roles, seeded from real
# spec labels (nv-spawn-acl-incident). The operator ruled that developer AND
# researcher roles must both be able to spawn / restart peers. A child agent
# authored with an explicit ``groups: [researcher]`` label already worked; a
# child authored with only ``role: researcher`` did NOT — it resolved to the
# empty group and was denied by the root-only gate. Both are pinned here,
# together with the cases that must STAY denied.
# ---------------------------------------------------------------------------


def test_labelled_researcher_child_may_spawn(db_path: Path) -> None:
    """Explicit ``groups: [researcher]`` child may spawn (regression pin)."""
    # Arrange
    _seed_child_from_labels(
        "neurovista", {"groups": ["researcher", "active"]}, db_path=db_path
    )
    # Act
    decision, _reason = check_spawn(caller="neurovista", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_role_derived_researcher_child_may_spawn(db_path: Path) -> None:
    """``role: researcher`` with NO groups label may spawn.

    The half of the operator's ruling that was missing: only developer-ish
    ROLES auto-joined their group, so a research agent that named its role
    but not its group was ungrouped — and an ungrouped child is denied.
    """
    # Arrange
    _seed_child_from_labels("res-by-role", {"role": "researcher"}, db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="res-by-role", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_role_derived_researcher_child_may_manage_peer(db_path: Path) -> None:
    """``role: researcher`` child may RESTART a developer peer (the incident)."""
    # Arrange
    _seed_child_from_labels("res-by-role", {"role": "research-agent"}, db_path=db_path)
    record_comms_policy(name="scitex-clew", group_name="developer", db_path=db_path)
    # Act
    decision, _reason = check_lineage_acl(
        caller="res-by-role", target="scitex-clew", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_role_derived_developer_child_may_spawn(db_path: Path) -> None:
    """The other half of the ruling: ``role: project-maintainer`` may spawn."""
    # Arrange
    _seed_child_from_labels(
        "dev-by-role", {"role": "project-maintainer"}, db_path=db_path
    )
    # Act
    decision, _reason = check_spawn(caller="dev-by-role", db_path=db_path)
    # Assert
    assert decision == "allow"


def test_worker_role_child_still_denied_spawn(db_path: Path) -> None:
    """NEGATIVE: a ``role: worker`` child derives no group → still denied.

    Guards against the fix over-reaching: role-derivation must promote the
    research roles ONLY, not every role. The clew haiku-TUI workers are real
    agents carrying exactly this label.
    """
    # Arrange
    _seed_child_from_labels("worker-1", {"role": "worker"}, db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="worker-1", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_isolated_solver_child_still_denied_spawn(db_path: Path) -> None:
    """NEGATIVE: an explicit non-mesh ``groups: [solver]`` child stays denied.

    Even when its role says ``researcher``: the explicit label wins, so a
    deliberately-isolated solver is never promoted into the fleet mesh.
    """
    # Arrange
    _seed_child_from_labels(
        "solver-1", {"role": "researcher", "groups": ["solver"]}, db_path=db_path
    )
    # Act
    decision, _reason = check_spawn(caller="solver-1", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_researcher_child_with_may_spawn_false_is_denied(db_path: Path) -> None:
    """NEGATIVE: per-spec ``lineage.may_spawn=false`` still overrides the group.

    The group grants authority; the spec can still revoke it. Without this,
    the fix would have removed an operator escape hatch.
    """
    # Arrange
    _seed_child_from_labels(
        "res-nospawn", {"role": "researcher"}, db_path=db_path, may_spawn=False
    )
    # Act
    decision, _reason = check_spawn(caller="res-nospawn", db_path=db_path)
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
