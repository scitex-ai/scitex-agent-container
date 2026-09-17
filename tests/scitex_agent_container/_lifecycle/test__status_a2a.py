"""A2A port observability in per-agent status."""

from __future__ import annotations

from scitex_agent_container._lifecycle import _status as status_module
from scitex_agent_container.config import AgentConfig


def _config() -> AgentConfig:
    config = AgentConfig(name="worker", harness="hermes", runtime="tui")
    config.a2a.port = "auto"
    return config


def test_a2a_status_separates_configured_and_durable_resolved_port() -> None:
    # Arrange
    def port_reader(name: str) -> int | None:
        return 19_555 if name == "worker" else None

    # Act
    result = status_module._a2a_status(
        "worker", _config(), port_reader=port_reader
    )

    # Assert
    assert result == {
        "configured_port": "auto",
        "resolved_port": 19_555,
        "resolution_source": "durable_port_claim",
    }


def test_a2a_status_does_not_infer_resolved_port_without_durable_claim() -> None:
    # Arrange
    def port_reader(_name: str) -> None:
        return None

    # Act
    result = status_module._a2a_status(
        "worker", _config(), port_reader=port_reader
    )

    # Assert
    assert result == {
        "configured_port": "auto",
        "resolved_port": None,
        "resolution_source": "none",
    }
