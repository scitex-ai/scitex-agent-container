"""A2A port observability in per-agent status."""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from scitex_agent_container._lifecycle import _status as status_module
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec


def _config() -> AgentConfig:
    config = AgentConfig(name="worker", harness="hermes", runtime="tui")
    config.a2a.port = "auto"
    return config


def _beat(*, process_alive: bool | None, connected: bool = True) -> dict:
    now = time.time()
    return {
        "agent_id": "remote-worker",
        "spec_id": "sha256:spec",
        "host": "node-7",
        "runtime": "tui",
        "harness": "hermes",
        "engine": "vllm",
        "model": "qwen",
        "session_id": "session-1",
        "boot_id": "boot-1",
        "seq": 1,
        "monotonic_ns": 1,
        "observed_at": now,
        "progress_at": now,
        "progress_seq": 1,
        "state": "idle",
        "lease_expires_at": now + 30,
        "card_id": "",
        "card_role": "",
        "_process_alive": process_alive,
        "_federation_connected": connected,
    }


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


def _write_spec(parent: Path, name: str) -> Path:
    spec_dir = parent / name
    spec_dir.mkdir(parents=True)
    path = spec_dir / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": explicit_spec(
                    {
                        "runtime": "tui",
                        "host": "${HOSTNAME}",
                        "workdir": str(parent),
                        "apptainer": {"image": "/x.sif", "binds": []},
                        "claude": {"model": "claude-sonnet-4-5"},
                        "health": {"enabled": True, "interval": 60},
                        "restart": {"policy": "on-failure", "max_retries": 3},
                    }
                ),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_agent_status_preserves_configured_intent_when_runtime_probe_fails(
    tmp_path: Path,
) -> None:
    # Arrange
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("worker", str(_write_spec(tmp_path, "worker")), "tui-worker")

    def broken_runtime(_config: AgentConfig):
        raise RuntimeError("runtime probe unavailable")

    # Act
    result = status_module.agent_status(
        "worker", registry=registry, runtime_factory=broken_runtime
    )

    # Assert
    assert result["a2a"]["configured_port"] == "auto"


def test_remote_instance_status_exposes_resolved_a2a_contract() -> None:
    # Arrange
    def rows():
        return [
            {
                "name": "remote-worker",
                "host": "node-7",
                "bound_port": 19_123,
                "remote": True,
            }
        ]

    # Act
    result = status_module._remote_instance_status(
        "remote-worker", instance_reader=rows
    )

    # Assert
    assert result is not None and (
        result["a2a"],
        result["status"],
        result["observation"]["process"]["state"],
    ) == (
        {
            "configured_port": None,
            "resolved_port": 19_123,
            "resolution_source": "active_instance_bound_port",
        },
        "unknown",
        "unknown",
    )


def test_remote_instance_direct_dead_outranks_active_row() -> None:
    # Arrange
    beat = _beat(process_alive=False)
    # Act
    result = status_module._remote_instance_status(
        "remote-worker",
        instance_reader=lambda: [
            {"name": "remote-worker", "host": "node-7", "remote": True}
        ],
        heartbeat_reader=lambda: [beat],
    )

    # Assert
    assert result is not None and (
        result["status"],
        result["observation"]["process"]["state"],
    ) == ("stopped", "exited")


def test_heartbeat_only_direct_alive_outranks_disconnection() -> None:
    # Arrange
    beat = _beat(process_alive=True, connected=False)
    # Act
    result = status_module._heartbeat_only_status(
        "remote-worker", heartbeat_reader=lambda: [beat]
    )
    # Assert
    assert result is not None and (
        result["status"],
        result["liveness"]["verdict"],
        result["observation"]["process"]["state"],
    ) == ("running", "alive", "alive")


def test_fresh_heartbeat_repairs_unknown_liveness_consistently() -> None:
    # Arrange
    beat = _beat(process_alive=None)
    # Act
    result = status_module._heartbeat_only_status(
        "remote-worker", heartbeat_reader=lambda: [beat]
    )
    # Assert
    assert result is not None and (
        result["status"],
        result["liveness"]["verdict"],
        result["observation"]["process"]["state"],
    ) == ("running", "alive", "alive")
