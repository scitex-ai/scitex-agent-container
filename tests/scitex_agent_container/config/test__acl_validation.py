"""Phase-3 ACL (ADR-0010 Step 2) — ``validate_phase3_acl`` structural check.

The validator is the read-only check ``sac doctor`` runs over a spec's
``spec.comms`` / ``spec.lineage`` blocks: absence is valid (default
preservation), and out-of-domain keys surface a named error.

AAA, one assertion per test, no mocks.
"""

from __future__ import annotations

from scitex_agent_container.config._acl_validation import validate_phase3_acl


def test_validate_phase3_acl_accepts_empty_spec() -> None:
    """Default-preservation: absence yields zero errors."""
    # Arrange
    spec: dict = {}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert errs == []


def test_validate_phase3_acl_flags_unknown_outbound_key() -> None:
    """A typo under ``spec.comms.outbound`` surfaces a validation error."""
    # Arrange
    spec = {"comms": {"outbound": {"cousins": "deny"}}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("cousins" in e for e in errs)


def test_validate_phase3_acl_rejects_non_mapping_comms() -> None:
    """``spec.comms`` as a scalar is rejected with a type error."""
    # Arrange
    spec = {"comms": "nope"}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.comms must be a mapping" in e for e in errs)


def test_validate_phase3_acl_flags_unknown_top_level_comms_key() -> None:
    """An unknown direct child of ``spec.comms`` is named."""
    # Arrange
    spec = {"comms": {"sideways": {}}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.comms.sideways is not a valid key" in e for e in errs)


def test_validate_phase3_acl_rejects_non_mapping_outbound() -> None:
    """``spec.comms.outbound`` as a scalar is rejected with a type error."""
    # Arrange
    spec = {"comms": {"outbound": "deny"}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.comms.outbound must be a mapping" in e for e in errs)


def test_validate_phase3_acl_rejects_out_of_domain_siblings() -> None:
    """A non allow/deny value under outbound.siblings is rejected."""
    # Arrange
    spec = {"comms": {"outbound": {"siblings": "maybe"}}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.comms.outbound.siblings must be one of" in e for e in errs)


def test_validate_phase3_acl_rejects_non_mapping_a2a() -> None:
    """``spec.comms.a2a`` as a scalar is rejected with a type error."""
    # Arrange
    spec = {"comms": {"a2a": True}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.comms.a2a must be a mapping" in e for e in errs)


def test_validate_phase3_acl_rejects_non_bool_a2a_listen() -> None:
    """``spec.comms.a2a.listen`` must be a boolean."""
    # Arrange
    spec = {"comms": {"a2a": {"listen": "yes"}}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.comms.a2a.listen must be a boolean" in e for e in errs)


def test_validate_phase3_acl_flags_unknown_a2a_key() -> None:
    """An unknown key under ``spec.comms.a2a`` is named."""
    # Arrange
    spec = {"comms": {"a2a": {"port": 9000}}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.comms.a2a.port is not a valid key" in e for e in errs)


def test_validate_phase3_acl_rejects_non_mapping_lineage() -> None:
    """``spec.lineage`` as a scalar is rejected with a type error."""
    # Arrange
    spec = {"lineage": "solitary"}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.lineage must be a mapping" in e for e in errs)


def test_validate_phase3_acl_flags_unknown_lineage_key() -> None:
    """An unknown key under ``spec.lineage`` is named."""
    # Arrange
    spec = {"lineage": {"clan": "x"}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.lineage.clan is not a valid key" in e for e in errs)


def test_validate_phase3_acl_rejects_out_of_domain_group() -> None:
    """A group outside the allowed set is rejected."""
    # Arrange
    spec = {"lineage": {"group": "cluster"}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.lineage.group must be one of" in e for e in errs)


def test_validate_phase3_acl_rejects_non_bool_may_spawn() -> None:
    """``spec.lineage.may_spawn`` must be a boolean."""
    # Arrange
    spec = {"lineage": {"may_spawn": "false"}}
    # Act
    errs = validate_phase3_acl(spec)
    # Assert
    assert any("spec.lineage.may_spawn must be a boolean" in e for e in errs)
