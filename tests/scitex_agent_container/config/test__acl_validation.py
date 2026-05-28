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
