"""Bridge-only reconciliation preserves the live TUI and its auto port."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container.config._types import A2ASpec, AgentConfig
from scitex_agent_container.runtimes._tui_turn_bridge import resolved_a2a_port
from scitex_agent_container.runtimes._tui_turn_bridge_reconcile import (
    _select_live_port,
    reconcile_turn_bridge,
)


def _config(port: int | str | None = "auto") -> AgentConfig:
    return AgentConfig(name="scitex-hub", runtime="tui", a2a=A2ASpec(port=port))


def test_auto_port_is_recovered_from_authoritative_live_state() -> None:
    # Arrange
    config = _config()
    endpoint = SimpleNamespace(
        a2a_port=19_000, host="scitex-compute-03", source="instance_row"
    )
    # Act
    result = _select_live_port(
        config, endpoint=endpoint, current_host="scitex-compute-03"
    )
    # Assert
    assert result == (19_000, "instance_row")


def test_auto_port_without_live_state_refuses_to_allocate_a_replacement() -> None:
    # Arrange
    config = _config()
    endpoint = SimpleNamespace(a2a_port=None, host="scitex-compute-03", source="none")
    # Act
    caught = pytest.raises(RuntimeError, match="refuses to allocate")
    # Assert
    with caught:
        _select_live_port(config, endpoint=endpoint, current_host="scitex-compute-03")


def test_pinned_port_drift_refuses_to_guess() -> None:
    # Arrange
    config = _config(19_000)
    endpoint = SimpleNamespace(
        a2a_port=19_001, host="scitex-compute-03", source="port_allocator"
    )
    # Act
    caught = pytest.raises(RuntimeError, match="refusing to guess")
    # Assert
    with caught:
        _select_live_port(config, endpoint=endpoint, current_host="scitex-compute-03")


def test_dry_run_resolves_auto_without_touching_bridge_or_tui() -> None:
    # Arrange
    config = _config()
    starts: list[int] = []
    hosts: list[str] = []

    def live_port(_config: AgentConfig, *, current_host: str) -> tuple[int, str]:
        hosts.append(current_host)
        return 19_000, "port_allocator"

    def start(_config: AgentConfig) -> int:
        starts.append(1)
        return 222

    # Act
    result = reconcile_turn_bridge(
        config,
        apply=False,
        current_host="scitex-compute-03",
        live_port_fn=live_port,
        session_alive_fn=lambda _session: True,
        bridge_pid_read_fn=lambda _config: 111,
        pid_alive_fn=lambda pid: pid == 111,
        bridge_start=start,
    )
    # Assert
    assert (result.status, result.port, result.applied, starts, hosts) == (
        "would-reload",
        19_000,
        False,
        [],
        ["scitex-compute-03"],
    )


def test_apply_reloads_only_bridge_and_proves_same_tui_survived() -> None:
    # Arrange
    config = _config()
    sessions: list[str] = []
    starts: list[int] = []

    def session_alive(session: str) -> bool:
        sessions.append(session)
        return True

    def start(resolved: AgentConfig) -> int:
        starts.append(int(resolved_a2a_port(resolved) or 0))
        return 222

    # Act
    result = reconcile_turn_bridge(
        config,
        apply=True,
        current_host="scitex-compute-03",
        live_port_fn=lambda _config, **_kwargs: (19_000, "instance_row"),
        session_alive_fn=session_alive,
        bridge_pid_read_fn=lambda _config: 111,
        pid_alive_fn=lambda pid: pid == 111,
        bridge_start=start,
        bridge_ready_fn=lambda host, port: (host, port) == ("127.0.0.1", 19_000),
    )
    # Assert
    assert (
        result.status,
        result.bridge_pid,
        result.tui_preserved,
        starts,
        sessions,
    ) == ("reloaded", 222, True, [19_000], ["tui-scitex-hub"] * 2)


def test_missing_tui_refuses_bridge_only_reconcile() -> None:
    # Arrange
    config = _config()
    # Act
    caught = pytest.raises(RuntimeError, match="will not start or restart")
    # Assert
    with caught:
        reconcile_turn_bridge(
            config,
            apply=True,
            current_host="scitex-compute-03",
            session_alive_fn=lambda _session: False,
        )
