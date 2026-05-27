"""Tests for the ``lead:`` block in config.yaml (ADR-0013 Phase 1).

Pins :class:`LeadConfig` parsing in
:mod:`scitex_agent_container._state.host_config`. Missing block stays
optional (config-load is missing-tolerant); a present block is
strictly validated so operator typos surface at load time, not as
opaque HTTP failures from the push helper.

No mocks (PA-306): every test writes a real YAML file at ``tmp_path``
and lets the real loader read it via the env-routed config path.
One assertion per test (PA-307).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import LeadConfig, load


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml under tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# Happy path — full block round-trips
# ---------------------------------------------------------------------------


def test_lead_block_parses_into_lead_config(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text(
        "lead:\n  name: lead\n  host: mba\n  a2a_port: 8642\n",
    )
    # Act
    cfg = load()
    # Assert
    assert cfg.lead == LeadConfig(name="lead", host="mba", a2a_port=8642)


def test_lead_block_carries_name(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text(
        "lead:\n  name: orchestrator\n  host: mba\n  a2a_port: 8642\n",
    )
    # Act
    name = load().lead.name
    # Assert
    assert name == "orchestrator"


def test_lead_block_carries_host(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text(
        "lead:\n  name: lead\n  host: spartan\n  a2a_port: 8642\n",
    )
    # Act
    host = load().lead.host
    # Assert
    assert host == "spartan"


def test_lead_block_carries_a2a_port(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text(
        "lead:\n  name: lead\n  host: mba\n  a2a_port: 9001\n",
    )
    # Act
    port = load().lead.a2a_port
    # Assert
    assert port == 9001


# ---------------------------------------------------------------------------
# Missing block — config-load stays missing-tolerant
# ---------------------------------------------------------------------------


def test_missing_lead_block_yields_none(cfg_path: Path) -> None:
    # Arrange — config exists but carries no lead: block.
    cfg_path.write_text("peers: {}\n")
    # Act
    out = load().lead
    # Assert
    assert out is None


def test_no_config_file_yields_none(cfg_path: Path) -> None:
    # Arrange — env points at a path that does not exist on disk.
    # Act
    out = load().lead
    # Assert
    assert out is None


# ---------------------------------------------------------------------------
# Loud validation — every bad shape names config.yaml in the error
# ---------------------------------------------------------------------------


def test_lead_must_be_mapping(cfg_path: Path) -> None:
    # Arrange — scalar where a mapping is required.
    cfg_path.write_text("lead: 7\n")
    # Act
    raised = pytest.raises(ValueError, match="must be a mapping")
    # Assert
    with raised:
        load()


def test_lead_missing_name_is_loud(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text("lead:\n  host: mba\n  a2a_port: 8642\n")
    # Act
    raised = pytest.raises(ValueError, match="lead.name")
    # Assert
    with raised:
        load()


def test_lead_empty_name_is_loud(cfg_path: Path) -> None:
    # Arrange — whitespace-only name (would silently authenticate as
    # "" if accepted).
    cfg_path.write_text("lead:\n  name: '   '\n  host: mba\n  a2a_port: 8642\n")
    # Act
    raised = pytest.raises(ValueError, match="lead.name")
    # Assert
    with raised:
        load()


def test_lead_missing_host_is_loud(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text("lead:\n  name: lead\n  a2a_port: 8642\n")
    # Act
    raised = pytest.raises(ValueError, match="lead.host")
    # Assert
    with raised:
        load()


def test_lead_a2a_port_must_be_int(cfg_path: Path) -> None:
    # Arrange — a string port would silently route to an unparseable
    # URL in the push helper; reject at load time.
    cfg_path.write_text(
        "lead:\n  name: lead\n  host: mba\n  a2a_port: '8642'\n",
    )
    # Act
    raised = pytest.raises(ValueError, match="lead.a2a_port")
    # Assert
    with raised:
        load()


def test_lead_a2a_port_must_be_positive(cfg_path: Path) -> None:
    # Arrange — zero / negative ports are nonsense; refuse the config.
    cfg_path.write_text("lead:\n  name: lead\n  host: mba\n  a2a_port: 0\n")
    # Act
    raised = pytest.raises(ValueError, match="lead.a2a_port")
    # Assert
    with raised:
        load()


def test_lead_a2a_port_rejects_bool(cfg_path: Path) -> None:
    # Arrange — YAML's ``true`` parses as bool 1 in Python; without an
    # explicit bool guard a future ``a2a_port: true`` would silently
    # route to port 1.
    cfg_path.write_text(
        "lead:\n  name: lead\n  host: mba\n  a2a_port: true\n",
    )
    # Act
    raised = pytest.raises(ValueError, match="lead.a2a_port")
    # Assert
    with raised:
        load()
