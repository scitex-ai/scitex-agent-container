"""Phase-3 ACL (ADR-0010 Step 2) — loader + parser tests.

Covers:

* :func:`parse_comms` round-trips ``spec.comms`` defaults and explicit
  outbound/inbound deny values.
* :func:`parse_lineage` round-trips ``spec.lineage.group=solitary`` +
  ``may_spawn=false``.
* The loader translates ``spec.comms.a2a.listen: false`` into
  ``A2ASpec.port = None`` (Gap-3 — operator-friendly alias for the
  existing ``spec.a2a.port: null`` surface).
* :func:`validate_phase3_acl` rejects out-of-domain values.

AAA, one assertion per test, no mocks (real YAML round-trip).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.config._acl_validation import validate_phase3_acl
from scitex_agent_container.config._parsers import parse_comms, parse_lineage


# ---------------------------------------------------------------------------
# parse_comms / parse_lineage
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# validate_phase3_acl — structural check used by sac doctor
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# load_config — full v3 YAML round-trip (Gap-3 a2a-listen translation)
# ---------------------------------------------------------------------------


def _write_spec(tmp_path: Path, body: str) -> Path:
    agent_dir = tmp_path / "cap-a"
    agent_dir.mkdir()
    p = agent_dir / "spec.yaml"
    p.write_text(body)
    return p


def test_load_config_a2a_listen_false_disables_a2a_port(tmp_path: Path) -> None:
    """Gap-3 end-to-end: a YAML with ``spec.comms.a2a.listen: false``
    yields an :class:`AgentConfig` whose ``a2a.port`` is ``None``
    (sidecar disabled, byte-identical to legacy ``spec.a2a.port: null``)."""
    # Arrange
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  comms:\n"
        "    a2a:\n"
        "      listen: false\n"
    )
    spec_path = _write_spec(tmp_path, body)
    # Act
    config = load_config(spec_path)
    # Assert
    assert config.a2a.port is None


def test_load_config_default_a2a_port_preserved_when_listen_absent(
    tmp_path: Path,
) -> None:
    """Default-preservation: with no ``spec.comms.a2a`` block, the
    ``a2a.port`` keeps its legacy default ('auto')."""
    # Arrange
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec: {}\n"
    )
    spec_path = _write_spec(tmp_path, body)
    # Act
    config = load_config(spec_path)
    # Assert
    assert config.a2a.port == "auto"


def test_load_config_lineage_may_spawn_false_round_trips(tmp_path: Path) -> None:
    """Gap-5 end-to-end: ``spec.lineage.may_spawn: false`` reaches the
    loaded :class:`AgentConfig` so core agent_start can persist it."""
    # Arrange
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  lineage:\n"
        "    may_spawn: false\n"
    )
    spec_path = _write_spec(tmp_path, body)
    # Act
    config = load_config(spec_path)
    # Assert
    assert config.lineage.may_spawn is False
