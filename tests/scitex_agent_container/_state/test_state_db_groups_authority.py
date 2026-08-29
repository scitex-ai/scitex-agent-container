"""Authority is MEMBERSHIP over an agent's whole named-group set.

REGRESSION — incident 2026-08-10, ``grant``. Its spec authored::

    metadata:
      labels:
        groups: [generalist, privileged, developer, researcher, active]

``a2a_peers`` reported all five. ``check_spawn`` on the SAME listen
server refused the agent with "is in none of the developer, research, or
privileged groups", because the ACL persisted only the FIRST list
element (``generalist``) and compared against that one string. Authority
therefore depended on the ORDER of a YAML list — moving ``developer`` to
the front would have silently fixed it.

Every test below drives the REAL persistence + REAL lookup against an
on-disk SQLite state.db (the yield-based ``db_path`` env override the
sibling group tests use). Nothing is mocked: a mock over
``resolve_group_name`` / ``read_comms_policy`` is exactly what would have
let this bug through, since the defect lived in what those functions
actually stored and returned.

AAA (each marker on its own line), one assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._listen._acl import check_spawn
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    is_developer,
    is_privileged,
    is_researcher,
    read_comms_policy,
    record_comms_policy,
    record_lineage,
    resolve_group_name,
    resolve_group_names,
    spawn_allowed,
)

# grant's spec labels, verbatim and IN ORDER. The order is the whole
# point: the group that grants authority is never the first element.
GRANT_GROUPS = ["generalist", "privileged", "developer", "researcher", "active"]


@pytest.fixture
def db_path(tmp_path: Path):
    # Arrange
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


def _record_grant_like(name: str, groups: list[str], db: Path) -> None:
    """Persist an agent the way ``persist_acl_policy`` does: PRIMARY =
    first authored group, SET = all of them."""
    record_comms_policy(
        name=name,
        group_name=groups[0],
        group_names=groups,
    )


# ---------------------------------------------------------------------------
# The exact mismatch: a child whose row lists developer/researcher/privileged
# NOT in first position. Each of these failed before the fix.
# ---------------------------------------------------------------------------


def test_developer_not_first_in_the_list_is_still_a_developer(pg_schema: str, db_path: Path) -> None:
    """``groups: [generalist, developer]`` IS a developer."""
    # Arrange
    _record_grant_like("alice", ["generalist", "developer"], db_path)
    # Act
    result = is_developer(name="alice")
    # Assert
    assert result is True


def test_researcher_not_first_in_the_list_is_still_a_researcher(pg_schema: str, db_path: Path) -> None:
    # Arrange
    _record_grant_like("alice", ["generalist", "researcher"], db_path)
    # Act
    result = is_researcher(name="alice")
    # Assert
    assert result is True


def test_privileged_not_first_in_the_list_is_still_privileged(pg_schema: str, db_path: Path) -> None:
    # Arrange
    _record_grant_like("alice", ["generalist", "privileged"], db_path)
    # Act
    result = is_privileged(name="alice")
    # Assert
    assert result is True


def test_grant_child_with_developer_in_its_groups_passes_check_spawn(
    pg_schema: str,
    db_path: Path,
) -> None:
    """THE reported bug: grant, a child of scitex-agent-container, denied
    spawn while its registry row listed developer."""
    # Arrange
    _record_grant_like("grant", GRANT_GROUPS, db_path)
    record_lineage(child="grant", parent="scitex-agent-container")
    # Act
    decision, _reason = check_spawn(caller="grant")
    # Assert
    assert decision == "allow"


def test_child_with_only_researcher_in_its_groups_passes_check_spawn(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The researcher half of the operator's ruling, on the same footing."""
    # Arrange
    _record_grant_like("nv", ["generalist", "researcher"], db_path)
    record_lineage(child="nv", parent="lead")
    # Act
    decision, _reason = check_spawn(caller="nv")
    # Assert
    assert decision == "allow"


def test_child_with_only_privileged_in_its_groups_passes_check_spawn(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    _record_grant_like("dotfiles", ["generalist", "privileged"], db_path)
    record_lineage(child="dotfiles", parent="lead")
    # Act
    decision, _reason = check_spawn(caller="dotfiles")
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# The gate still SHUTS. A broadened ACL that stopped denying would be a
# worse bug than the one being fixed.
# ---------------------------------------------------------------------------


def test_child_in_no_authorising_group_is_still_denied(pg_schema: str, db_path: Path) -> None:
    # Arrange
    _record_grant_like("worker", ["generalist", "active"], db_path)
    record_lineage(child="worker", parent="lead")
    # Act
    decision, _reason = check_spawn(caller="worker")
    # Assert
    assert decision == "deny"


def test_ungrouped_child_is_still_denied(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="worker")
    record_lineage(child="worker", parent="lead")
    # Act
    decision, _reason = check_spawn(caller="worker")
    # Assert
    assert decision == "deny"


def test_isolated_solver_group_gets_no_spawn_authority(pg_schema: str, db_path: Path) -> None:
    """A deliberately-isolated solver must not gain authority from the
    set-valued read."""
    # Arrange
    _record_grant_like("solver", ["solver", "capsule"], db_path)
    record_lineage(child="solver", parent="clew")
    # Act
    decision, _reason = check_spawn(caller="solver")
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# The MESH bucket stays single-valued. Authority is any-of; the default-ACL
# mesh keeps ONE group per agent, which is what keeps a solver isolated.
# ---------------------------------------------------------------------------


def test_primary_group_is_still_the_first_authored_group(pg_schema: str, db_path: Path) -> None:
    # Arrange
    _record_grant_like("grant", GRANT_GROUPS, db_path)
    # Act
    primary = resolve_group_name(name="grant")
    # Assert
    assert primary == "generalist"


def test_resolve_group_names_returns_every_authored_group(pg_schema: str, db_path: Path) -> None:
    # Arrange
    _record_grant_like("grant", GRANT_GROUPS, db_path)
    # Act
    groups = resolve_group_names(name="grant")
    # Assert
    assert groups == frozenset(GRANT_GROUPS)


# ---------------------------------------------------------------------------
# Backward compatibility: a row written by the OLD code (primary only, no
# set) must keep its exact old meaning rather than losing its group.
# ---------------------------------------------------------------------------


def test_legacy_row_without_a_set_still_resolves_to_its_primary(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange — group_names omitted, exactly like a pre-migration caller.
    record_comms_policy(name="legacy", group_name="developer")
    # Act
    groups = resolve_group_names(name="legacy")
    # Assert
    assert groups == frozenset({"developer"})


def test_legacy_developer_row_still_passes_check_spawn(pg_schema: str, db_path: Path) -> None:
    # Arrange
    record_comms_policy(name="legacy", group_name="developer")
    record_lineage(child="legacy", parent="lead")
    # Act
    decision, _reason = check_spawn(caller="legacy")
    # Assert
    assert decision == "allow"


def test_unknown_agent_resolves_to_no_groups(pg_schema: str, db_path: Path) -> None:
    # Arrange — no row at all.
    # Act
    groups = resolve_group_names(name="ghost")
    # Assert
    assert groups == frozenset()


# ---------------------------------------------------------------------------
# Round-trip + encoding guards on the new column.
# ---------------------------------------------------------------------------


def test_group_names_round_trip_through_the_policy_row(pg_schema: str, db_path: Path) -> None:
    # Arrange
    _record_grant_like("grant", GRANT_GROUPS, db_path)
    # Act
    policy = read_comms_policy(name="grant")
    # Assert
    assert set(policy["group_names"]) == set(GRANT_GROUPS)


def test_group_names_defaults_to_empty_for_an_unrecorded_agent(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange — no record_comms_policy call.
    # Act
    policy = read_comms_policy(name="never-recorded")
    # Assert
    assert policy["group_names"] == ()


def test_a_group_name_containing_a_comma_is_rejected(db_path: Path) -> None:
    """The column is comma-separated; silently splitting one group into
    two would corrupt an ACL input."""
    # Arrange
    bad = ["developer,researcher"]
    # Act
    raises = pytest.raises(ValueError)
    # Assert
    with raises:
        record_comms_policy(name="alice", group_names=bad)


def test_a_bare_string_group_names_is_rejected(db_path: Path) -> None:
    """A string would be iterated character by character — a silent
    fail-shut that looks like a correct ACL."""
    # Arrange
    bad = "developer"
    # Act
    raises = pytest.raises(ValueError)
    # Assert
    with raises:
        record_comms_policy(name="alice", group_names=bad)


# ---------------------------------------------------------------------------
# The DENIAL MESSAGE. It sent grant down the wrong path for hours by
# asserting a fact about the AGENT rather than reporting what the GATE saw.
# ---------------------------------------------------------------------------


def test_denial_names_the_groups_the_gate_actually_resolved(pg_schema: str, db_path: Path) -> None:
    # Arrange
    _record_grant_like("worker", ["generalist", "active"], db_path)
    record_lineage(child="worker", parent="lead")
    # Act
    _decision, reason = spawn_allowed(caller="worker")
    # Assert
    assert "'active', 'generalist'" in reason


def test_denial_no_longer_claims_the_caller_holds_none_of_the_groups(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The old sentence was flatly false against the same server's own
    a2a_peers output; it must not come back."""
    # Arrange
    _record_grant_like("worker", ["generalist"], db_path)
    record_lineage(child="worker", parent="lead")
    # Act
    _decision, reason = spawn_allowed(caller="worker")
    # Assert
    assert "is in none of the" not in reason


def test_denial_spells_researcher_in_full(pg_schema: str, db_path: Path) -> None:
    """The old text said "research", which cost a reader a wrong
    hypothesis about a string mismatch. Name the real group."""
    # Arrange
    _record_grant_like("worker", ["generalist"], db_path)
    record_lineage(child="worker", parent="lead")
    # Act
    _decision, reason = spawn_allowed(caller="worker")
    # Assert
    assert "researcher" in reason


def test_denial_points_at_refresh_acl_when_the_row_may_be_stale(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    _record_grant_like("worker", ["generalist"], db_path)
    record_lineage(child="worker", parent="lead")
    # Act
    _decision, reason = spawn_allowed(caller="worker")
    # Assert
    assert "refresh-acl" in reason


def test_denial_distinguishes_an_absent_row_from_an_ungrouped_agent(
    pg_schema: str,
    db_path: Path,
) -> None:
    """The 2026-08-09 host_exec lesson, applied to the spawn gate: both
    produce an empty group set and they are different facts."""
    # Arrange — a lineage edge but NO policy row for the caller.
    record_lineage(child="stranger", parent="lead")
    # Act
    _decision, reason = spawn_allowed(caller="stranger")
    # Assert
    assert "NO node_comms_policy row" in reason
