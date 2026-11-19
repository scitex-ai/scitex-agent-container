"""``cli_pkg._explain_grants`` — the Grants section of ``sac agents explain``.

Real ``AgentConfig`` values; no mocks.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._explain_grants import grants_lines
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._grants import Grants, all_granted


def _config(labels: dict, grants=None) -> AgentConfig:
    config = AgentConfig(name="agt")
    config.labels = labels
    config.grants = grants
    return config


def test_legacy_eligible_agent_gets_loud_host_exec_banner() -> None:
    # Arrange
    config = _config({"groups": ["developer"]})
    # Act
    lines = grants_lines(config)
    # Assert
    assert lines[1].startswith("!!! HOST EXEC: HELD")


def test_legacy_grants_are_labelled_implicit_default() -> None:
    # Arrange
    config = _config({})
    # Act
    lines = grants_lines(config)
    # Assert
    assert any(
        line.startswith("  fleet_env: implicit (legacy default") for line in lines
    )


def test_explicit_withheld_host_exec_is_not_held() -> None:
    # Arrange
    config = _config({"groups": ["developer"]}, Grants(host_network=True))
    # Act
    lines = grants_lines(config)
    # Assert
    assert lines[1].startswith("    HOST EXEC: not held")


def test_explicit_list_grant_shows_named_parts() -> None:
    # Arrange
    config = _config({}, Grants(host_network=True, host_binds=frozenset({"cards"})))
    # Act
    lines = grants_lines(config)
    # Assert
    assert "  host_binds: explicit: ['cards']" in "\n".join(lines)


def test_explicit_block_without_network_says_start_refuses() -> None:
    # Arrange
    config = _config({}, Grants())
    # Act
    lines = grants_lines(config)
    # Assert
    assert any("START WILL REFUSE" in line for line in lines)


def test_fully_granted_block_shows_no_refusal() -> None:
    # Arrange
    config = _config({}, all_granted())
    # Act
    lines = grants_lines(config)
    # Assert
    assert not any("START WILL REFUSE" in line for line in lines)
