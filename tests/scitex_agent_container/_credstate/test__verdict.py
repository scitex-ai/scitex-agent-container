"""The loud, honest answer: what is missing, and where does it come from.

Real dataclasses throughout — nothing here is stubbed, because the
verdict logic IS the behaviour under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scitex_agent_container._credstate._model import (
    CredentialDescriptor,
    CredentialPlacement,
)
from scitex_agent_container._credstate._observe import LocalObservation
from scitex_agent_container._credstate._verdict import (
    ABSENT,
    EXPIRED,
    EXPIRING,
    EXTRA_REFRESHER,
    NO_REFRESHER,
    OK,
    SEVERITY_FAULT,
    SEVERITY_OK,
    SEVERITY_WARN,
    UNDECLARED,
    UNRESOLVABLE,
    WORLD_READABLE,
    assess,
    check_single_refresher,
    undeclared_findings,
    worst_severity,
)

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
PRIMARY = "scitex-nas-03"
REPLICA = "scitex-compute-04"
KEY = "anthropic-oauth:demo"


def _descriptor(**kwargs):
    base = dict(
        origin_node=PRIMARY,
        cred_key=KEY,
        account="demo",
        tier="primary_secret",
        primary_node=PRIMARY,
    )
    base.update(kwargs)
    return CredentialDescriptor(**base)


def _placement(**kwargs):
    base = dict(
        origin_node=REPLICA,
        cred_key=KEY,
        node=REPLICA,
        role="replica",
        locator="file:/home/agent/.claude/.credentials.json",
    )
    base.update(kwargs)
    return CredentialPlacement(**base)


def _observation(**kwargs):
    base = dict(
        locator="file:/home/agent/.claude/.credentials.json",
        present=True,
        scheme="file",
        file_mode="0600",
        holds_refresh_material=False,
        artifact_expires_at=NOW + timedelta(hours=4),
    )
    base.update(kwargs)
    return LocalObservation(**base)


def _verdicts(findings):
    return {f.verdict for f in findings}


def test_a_required_but_absent_credential_is_a_fault():
    # Arrange
    observation = _observation(present=False, artifact_expires_at=None)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert _verdicts(findings) == {ABSENT}


def test_an_absent_primary_secret_refuses_to_be_copied_to_a_replica():
    # Arrange — this refusal is the two-tier model holding, not a gap.
    observation = _observation(present=False, artifact_expires_at=None)
    # Act
    findings = assess(
        descriptor=_descriptor(tier="primary_secret"),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert "do NOT copy" in findings[0].remedy


def test_the_refusal_names_the_node_the_material_actually_lives_on():
    # Arrange
    observation = _observation(present=False, artifact_expires_at=None)
    # Act
    findings = assess(
        descriptor=_descriptor(tier="primary_secret"),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert PRIMARY in findings[0].remedy


def test_an_absent_distributable_names_the_command_that_materializes_it():
    # Arrange
    observation = _observation(present=False, artifact_expires_at=None)
    descriptor = _descriptor(
        tier="distributable", obtain_command="sac accounts keepalive --to " + REPLICA
    )
    # Act
    findings = assess(
        descriptor=descriptor,
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert "sac accounts keepalive" in findings[0].remedy


def test_a_credential_with_no_descriptor_says_its_source_is_unknown():
    # Arrange
    observation = _observation(present=False, artifact_expires_at=None)
    # Act
    findings = assess(
        descriptor=None,
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert "unknown" in findings[0].remedy


def test_a_non_required_absent_credential_is_not_a_fault():
    # Arrange
    observation = _observation(present=False, artifact_expires_at=None)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(required=False),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert _verdicts(findings) == {OK}


def test_an_unresolvable_locator_is_reported_as_such():
    # Arrange
    observation = _observation(scheme=None, present=False)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(locator="vault://x"),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert _verdicts(findings) == {UNRESOLVABLE}


def test_an_artifact_past_its_own_expiry_is_expired():
    # Arrange — the fact nobody held when eight subagents died.
    observation = _observation(artifact_expires_at=NOW - timedelta(minutes=1))
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert EXPIRED in _verdicts(findings)


def test_an_artifact_inside_the_horizon_is_expiring():
    # Arrange
    observation = _observation(artifact_expires_at=NOW + timedelta(minutes=5))
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert EXPIRING in _verdicts(findings)


def test_an_artifact_comfortably_in_the_future_is_neither():
    # Arrange
    observation = _observation(artifact_expires_at=NOW + timedelta(hours=8))
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert _verdicts(findings) == {OK}


def test_expiry_remedy_points_at_the_primary_when_run_on_a_replica():
    # Arrange
    observation = _observation(artifact_expires_at=NOW - timedelta(minutes=1))
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert PRIMARY in findings[0].remedy


def test_a_world_readable_credential_is_reported():
    # Arrange
    observation = _observation(world_readable=True, file_mode="0644")
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert WORLD_READABLE in _verdicts(findings)


def test_a_replica_holding_refresh_material_is_the_cr001_violation():
    # Arrange — invisible today: the disk is the only witness and it
    # reports a second holder exactly as it reports the first.
    observation = _observation(holds_refresh_material=True)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(role="replica"),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert EXTRA_REFRESHER in _verdicts(findings)


def test_the_cr001_violation_is_a_fault_not_a_warning():
    # Arrange
    observation = _observation(holds_refresh_material=True)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(role="replica"),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert [f for f in findings if f.verdict == EXTRA_REFRESHER][0].is_fault


def test_the_cr001_summary_explains_mutual_invalidation():
    # Arrange
    observation = _observation(holds_refresh_material=True)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(role="replica"),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert "revokes the other" in findings[0].summary


def test_a_declared_primary_holding_no_refresh_material_is_a_fault():
    # Arrange — nothing here can renew; the credential is on a one-way trip.
    observation = _observation(holds_refresh_material=False)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(node=PRIMARY, role="primary"),
        observation=observation,
        node=PRIMARY,
        now=NOW,
    )
    # Assert
    assert NO_REFRESHER in _verdicts(findings)


def test_a_healthy_primary_reports_ok():
    # Arrange
    observation = _observation(holds_refresh_material=True)
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(node=PRIMARY, role="primary"),
        observation=observation,
        node=PRIMARY,
        now=NOW,
    )
    # Assert
    assert _verdicts(findings) == {OK}


def test_a_healthy_replica_reports_ok():
    # Arrange
    observation = _observation()
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert _verdicts(findings) == {OK}


def test_every_simultaneous_fault_is_reported_not_only_the_worst():
    # Arrange — a world-readable AND expired token is two fixes.
    observation = _observation(
        world_readable=True,
        file_mode="0644",
        artifact_expires_at=NOW - timedelta(minutes=1),
    )
    # Act
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Assert
    assert {EXPIRED, WORLD_READABLE} <= _verdicts(findings)


def test_two_nodes_holding_refresh_material_is_a_fleet_level_fault():
    # Arrange
    holders = [PRIMARY, REPLICA]
    # Act
    findings = check_single_refresher(
        holders=holders, cred_key=KEY, declared_primary=PRIMARY
    )
    # Assert
    assert findings[0].verdict == EXTRA_REFRESHER


def test_no_node_holding_refresh_material_is_a_fleet_level_fault():
    # Arrange — nothing in the fleet can renew it.
    holders = []
    # Act
    findings = check_single_refresher(
        holders=holders, cred_key=KEY, declared_primary=PRIMARY
    )
    # Assert
    assert findings[0].verdict == NO_REFRESHER


def test_the_holder_disagreeing_with_the_declaration_is_a_fault():
    # Arrange — the declaration and the disk must not silently differ.
    holders = [REPLICA]
    # Act
    findings = check_single_refresher(
        holders=holders, cred_key=KEY, declared_primary=PRIMARY
    )
    # Assert
    assert findings[0].severity == SEVERITY_FAULT


def test_exactly_one_holder_matching_the_declaration_is_clean():
    # Arrange
    holders = [PRIMARY]
    # Act
    findings = check_single_refresher(
        holders=holders, cred_key=KEY, declared_primary=PRIMARY
    )
    # Assert
    assert findings == []


def test_duplicate_reports_of_the_same_holder_are_not_a_violation():
    # Arrange — two observations of one node is not two nodes.
    holders = [PRIMARY, PRIMARY]
    # Act
    findings = check_single_refresher(
        holders=holders, cred_key=KEY, declared_primary=PRIMARY
    )
    # Assert
    assert findings == []


def test_material_present_but_declared_nowhere_is_reported():
    # Arrange — the shape of the token found in a ~/.bashrc.
    observed = ["file:/home/agent/.env", "file:/home/agent/.claude/.credentials.json"]
    # Act
    findings = undeclared_findings(
        observed_locators=observed,
        declared_locators=["file:/home/agent/.claude/.credentials.json"],
        node=REPLICA,
    )
    # Assert
    assert _verdicts(findings) == {UNDECLARED}


def test_fully_declared_material_produces_no_undeclared_finding():
    # Arrange
    observed = ["file:/home/agent/.claude/.credentials.json"]
    # Act
    findings = undeclared_findings(
        observed_locators=observed, declared_locators=observed, node=REPLICA
    )
    # Assert
    assert findings == []


def test_a_fault_dominates_the_overall_severity():
    # Arrange
    observation = _observation(present=False, artifact_expires_at=None)
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Act
    severity = worst_severity(findings)
    # Assert
    assert severity == SEVERITY_FAULT


def test_a_warning_dominates_a_clean_result():
    # Arrange
    observation = _observation(world_readable=True, file_mode="0644")
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=observation,
        node=REPLICA,
        now=NOW,
    )
    # Act
    severity = worst_severity(findings)
    # Assert
    assert severity == SEVERITY_WARN


def test_a_clean_result_is_ok():
    # Arrange
    findings = assess(
        descriptor=_descriptor(),
        placement=_placement(),
        observation=_observation(),
        node=REPLICA,
        now=NOW,
    )
    # Act
    severity = worst_severity(findings)
    # Assert
    assert severity == SEVERITY_OK
