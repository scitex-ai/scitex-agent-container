"""Phase-3 ACL (ADR-0010 Step 2) — ``_parsers._comms`` round-trip tests.

``parse_comms`` / ``parse_lineage`` translate ``spec.comms`` /
``spec.lineage`` YAML into their dataclasses, preserving the legacy
all-allow default and failing loud on out-of-domain values.

AAA, one assertion per test, no mocks (real YAML round-trip).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers import parse_comms, parse_lineage


def test_parse_comms_absent_yields_default_allow() -> None:
    """No ``spec.comms`` block → all-allow default (legacy preservation)."""
    # Arrange
    spec: dict = {}
    # Act
    parsed = parse_comms(spec)
    # Assert
    assert parsed.outbound.siblings == "allow"


def test_parse_comms_outbound_siblings_deny() -> None:
    """Explicit deny survives the YAML → dataclass round-trip."""
    # Arrange
    spec = {"comms": {"outbound": {"siblings": "deny"}}}
    # Act
    parsed = parse_comms(spec)
    # Assert
    assert parsed.outbound.siblings == "deny"


def test_parse_comms_inbound_parent_deny() -> None:
    """Inbound parent deny round-trips."""
    # Arrange
    spec = {"comms": {"inbound": {"parent": "deny"}}}
    # Act
    parsed = parse_comms(spec)
    # Assert
    assert parsed.inbound.parent == "deny"


def test_parse_comms_a2a_listen_false() -> None:
    """Gap-3: explicit ``listen: false`` toggles the new surface."""
    # Arrange
    spec = {"comms": {"a2a": {"listen": False}}}
    # Act
    parsed = parse_comms(spec)
    # Assert
    assert parsed.a2a.listen is False


def test_parse_comms_rejects_unknown_key() -> None:
    """Typos at the YAML surface fail loud rather than silently degrading."""
    # Arrange
    spec = {"comms": {"unknown": {}}}
    # Act
    # Assert
    with pytest.raises(ValueError):
        parse_comms(spec)


def test_parse_lineage_group_solitary() -> None:
    """Gap-4: ``group: solitary`` survives the round-trip."""
    # Arrange
    spec = {"lineage": {"group": "solitary"}}
    # Act
    parsed = parse_lineage(spec)
    # Assert
    assert parsed.group == "solitary"


def test_parse_lineage_may_spawn_false() -> None:
    """Gap-5: ``may_spawn: false`` survives the round-trip."""
    # Arrange
    spec = {"lineage": {"may_spawn": False}}
    # Act
    parsed = parse_lineage(spec)
    # Assert
    assert parsed.may_spawn is False


def test_parse_lineage_rejects_unknown_group_value() -> None:
    """Out-of-domain group value fails at parse time."""
    # Arrange
    spec = {"lineage": {"group": "cluster"}}
    # Act
    # Assert
    with pytest.raises(ValueError):
        parse_lineage(spec)
