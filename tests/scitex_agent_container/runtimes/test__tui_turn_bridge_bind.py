"""TUI turn bridge (``runtimes._tui_turn_bridge``): bind, rebind, port ownership and declared host.

Real seams only (PA-306 no-mocks); see test__tui_turn_bridge.py.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container.config._a2a_defaults import DEFAULT_A2A_HOST
from scitex_agent_container.runtimes import _tui_turn_bridge as bridge
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml
from tests.scitex_agent_container.runtimes._tui_turn_bridge_helpers import (
    _HAS_PORT_DISCOVERY,
    _LAN_HOST,
    _PID,
    _WILDCARD_HOST,
    _cfg_with_host,
    _free_port,
    _gate_says_free,
    _observed_bind_host,
    _wait_until_bound,
)


def test_turn_bridge_server_sets_allow_reuse_address() -> None:
    # Arrange
    server_cls = bridge._TurnBridgeServer
    # Act
    reuse = server_cls.allow_reuse_address
    # Assert — SO_REUSEADDR so a rebind is not blocked by a TIME_WAIT socket.
    assert reuse is True


def test_build_server_rebind_after_close_succeeds() -> None:
    # Arrange — bind a real bridge server on an ephemeral port, then close it.
    first = bridge.build_server(
        host="127.0.0.1", port=0, on_turn=lambda *_a, **_k: None, agent_name="fig"
    )
    port = first.server_address[1]
    first.server_close()
    # Act — a fresh bind on the SAME port must succeed (allow_reuse_address).
    second = bridge.build_server(
        host="127.0.0.1", port=port, on_turn=lambda *_a, **_k: None, agent_name="fig"
    )
    rebound_port = second.server_address[1]
    second.server_close()
    # Assert
    assert rebound_port == port


def test_build_server_raises_named_error_when_port_held() -> None:
    # Arrange — a REAL socket holds the port so the bridge bind is refused.
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    # Act
    # Assert
    try:
        with pytest.raises(bridge.TurnBridgePortBusyError):
            bridge.build_server(
                host="127.0.0.1",
                port=port,
                on_turn=lambda *_a, **_k: None,
                agent_name="fig",
            )
    finally:
        held.close()


def test_start_turn_bridge_fails_loud_when_port_stays_busy(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a REAL listener holds the agent's a2a port; spawn must NOT run.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="stuck", config_path=str(spec)
    )
    clock = {"t": 0.0}

    def must_not_spawn(*_a, **_k):
        raise AssertionError("start must not spawn a bridge into a held port")

    # Act
    # Assert
    try:
        with pytest.raises(bridge.TurnBridgePortBusyError):
            bridge.start_turn_bridge(
                config,
                spawn=must_not_spawn,
                port_free_timeout_s=0.5,
                sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
                now_fn=lambda: clock["t"],
            )
    finally:
        held.close()


def test_stop_turn_bridge_releases_held_a2a_port(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a REAL listener subprocess bound to the agent's a2a port,
    # recorded as the agent's bridge PID; stop must SIGTERM it AND wait for
    # the port to release (the incident: stop returned before release).
    port = _free_port()
    listener_code = (
        "import socket,time;"
        "s=socket.socket();"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('127.0.0.1',{port}));"
        "s.listen();"
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", listener_code])
    _wait_until_bound(port)
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="free-me", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    # Act
    bridge.stop_turn_bridge(config)
    proc.wait(timeout=5)
    # Assert — the a2a port is bindable again.
    assert bridge.port_is_free("127.0.0.1", port) is True


# ---------------------------------------------------------------------------
# Stop teardown — free the OWN port from a survivor that is NOT the tracked PID
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not _HAS_PORT_DISCOVERY,
    reason="needs lsof/ss/fuser to resolve the survivor holder for the sweep",
)
def test_stop_turn_bridge_force_frees_survivor_on_own_port(
    isolated_home: Path,
) -> None:
    # Arrange — a REAL survivor bound to the agent's OWN a2a port that is NOT
    # the tracked bridge PID and IGNORES SIGTERM (the 2026-07-12 incident:
    # await_bridge_release only reaps the tracked PID, so a survivor kept the
    # port and the next start failed loud). The tracked PID is already reaped,
    # so ONLY the force-kill sweep can free the port.
    port = _free_port()
    survivor_code = (
        "import socket,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "s=socket.socket();"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('127.0.0.1',{port}));"
        "s.listen();"
        "time.sleep(60)"
    )
    survivor = subprocess.Popen([sys.executable, "-c", survivor_code])
    _wait_until_bound(port)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)  # a tracked bridge PID that is already reaped
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="survivor-agent", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(dead.pid), encoding="utf-8")
    # Act
    try:
        bridge.stop_turn_bridge(config)
        survivor.wait(timeout=5)
        # Assert — the survivor on the OWN port was SIGKILLed; port bindable.
        assert bridge.port_is_free("127.0.0.1", port) is True
    finally:
        if survivor.poll() is None:  # pragma: no cover - defensive cleanup
            survivor.kill()
            survivor.wait(timeout=5)


def test_stop_turn_bridge_fails_loud_when_own_port_stays_stuck(
    isolated_home: Path,
) -> None:
    # Arrange — a probe that never reports the OWN a2a port free (a genuinely
    # unkillable holder). Even after the force-kill sweep, stop must FAIL LOUD
    # (raise the actionable TurnBridgePortBusyError) rather than silently
    # proceed into an EADDRINUSE crash on the next start. The tracked PID is
    # already reaped and the real port is free, so the sweep finds nothing to
    # kill and the injected probe drives the bounded re-poll to time out.
    port = _free_port()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="stuck-forever", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(dead.pid), encoding="utf-8")
    clock = {"t": 0.0}
    # Act
    # Assert
    with pytest.raises(bridge.TurnBridgePortBusyError):
        bridge.stop_turn_bridge(
            config,
            grace_s=0.5,
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"],
            port_free_fn=lambda _h, _p: False,
        )


def test_resolved_a2a_host_returns_the_declared_spec_host() -> None:
    # Arrange
    config = _cfg_with_host(_LAN_HOST)
    # Act
    host = bridge.resolved_a2a_host(config)
    # Assert
    assert host == _LAN_HOST


def test_resolved_a2a_host_defaults_to_loopback_when_undeclared() -> None:
    # Arrange — CASE 1 (no-regression): a spec with no host key at all.
    config = _cfg_with_host(None)
    # Act
    host = bridge.resolved_a2a_host(config)
    # Assert
    assert host == DEFAULT_A2A_HOST


def test_resolved_a2a_host_defaults_to_loopback_for_a_blank_host() -> None:
    # Arrange — a whitespace-only host states nothing and must not become an
    # unbindable empty string.
    config = _cfg_with_host("   ")
    # Act
    host = bridge.resolved_a2a_host(config)
    # Assert
    assert host == DEFAULT_A2A_HOST


def test_the_bridge_default_host_agrees_with_the_fleet_wide_default() -> None:
    # Arrange — the bridge carries its OWN "127.0.0.1" literal because the two
    # canonical spellings live in modules over this repo's line cap and cannot
    # be edited. Pin the agreement so a future drift breaks HERE, loudly,
    # instead of quietly splitting the fleet's bind address in two.
    pinned = DEFAULT_A2A_HOST
    # Act
    bridge_default = bridge.DEFAULT_HOST
    # Assert
    assert bridge_default == pinned


def test_bridge_binds_loopback_for_an_undeclared_spec_host() -> None:
    # Arrange — CASE 1 observed on a real socket.
    config = _cfg_with_host(None)
    # Act
    bound = _observed_bind_host(config)
    # Assert
    assert bound == DEFAULT_A2A_HOST


def test_bridge_binds_loopback_when_the_spec_declares_loopback() -> None:
    # Arrange — CASE 1 as all 102 fleet specs actually spell it today.
    config = _cfg_with_host(DEFAULT_A2A_HOST)
    # Act
    bound = _observed_bind_host(config)
    # Assert
    assert bound == DEFAULT_A2A_HOST


def test_bridge_binds_the_wildcard_address_the_spec_declares() -> None:
    # Arrange — CASE 2 observed on a real socket: the spec asks for every
    # interface and the kernel confirms the socket is there, not on loopback.
    config = _cfg_with_host(_WILDCARD_HOST)
    # Act
    bound = _observed_bind_host(config)
    # Assert
    assert bound == _WILDCARD_HOST


def test_start_turn_bridge_spawns_with_the_declared_host(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — CASE 2 through the LAUNCHER: the spawned bridge's --host must
    # carry the spec's value, or the subprocess binds somewhere else entirely.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **_kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = _cfg_with_host(_WILDCARD_HOST)
    config.config_path = str(spec)
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert recorded["argv"][recorded["argv"].index("--host") + 1] == _WILDCARD_HOST


def test_start_turn_bridge_spawns_with_loopback_for_an_undeclared_host(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — CASE 1 through the LAUNCHER.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **_kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = _cfg_with_host(None)
    config.config_path = str(spec)
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert recorded["argv"][recorded["argv"].index("--host") + 1] == DEFAULT_A2A_HOST


def test_start_turn_bridge_explicit_host_overrides_the_spec(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — the caller-supplied host is a seam and must still win over the
    # spec, so the spec default never removes an override.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **_kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = _cfg_with_host(_WILDCARD_HOST)
    config.config_path = str(spec)
    # Act
    bridge.start_turn_bridge(
        config,
        spawn=fake_spawn,
        host=DEFAULT_A2A_HOST,
        port_free_fn=_gate_says_free,
    )
    # Assert
    assert recorded["argv"][recorded["argv"].index("--host") + 1] == DEFAULT_A2A_HOST
