"""Hermes managed-stop guard: native activity, bounded wait, explicit force."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._lifecycle._managed_turn_drain import (
    ManagedTurnDrainRefusal,
    guard_managed_turn,
)
from scitex_agent_container.cli_pkg.lifecycle._restart_remote import (
    remote_restart_argv,
)
from scitex_agent_container.cli_pkg.lifecycle._stop import remote_stop_argv
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._hermes_tui_rpc import HermesTurnActivity
from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml


def _config(harness: str = "hermes") -> AgentConfig:
    return AgentConfig(name="hub", harness=harness, runtime="tui")


def _activity(status: str) -> HermesTurnActivity:
    return HermesTurnActivity(
        state="idle" if status == "idle" else "active",
        session_status=status,
        session_id="live-hub",
    )


class _LiveMux:
    @staticmethod
    def exists(_name: str) -> bool:
        return True


class _Registry:
    def __init__(self, spec: Path):
        self._row = {"name": "hub", "config": str(spec)}

    def get(self, _name: str) -> dict:
        return self._row

    @staticmethod
    def remove(_name: str) -> None:
        raise AssertionError("registry removal reached after active-turn refusal")


def _hermes_spec(tmp_path: Path) -> Path:
    agent_dir = tmp_path / "hub"
    agent_dir.mkdir()
    path = agent_dir / "spec.yaml"
    path.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: tui\n"
            "  harness: hermes\n"
            "  host: ${HOSTNAME}\n"
            f"  workdir: {tmp_path}\n"
            "  available_harnesses:\n"
            "    hermes:\n"
            "      session:\n"
            "        mode: continue\n"
            "        max_age_minutes: null\n"
            "      channels:\n"
            "        - server:sac\n"
        )
    )
    return path


def test_idle_native_session_allows_stop_without_waiting():
    # Arrange
    sleeps = []
    # Act
    result = guard_managed_turn(
        _config(),
        allow_active_turn_kill=False,
        probe=lambda _config: _activity("idle"),
        sleep_fn=sleeps.append,
    )
    # Assert
    assert (result and result.status, result and result.forced, sleeps) == (
        "idle",
        False,
        [],
    )


def test_active_session_refuses_before_any_stop_side_effect():
    # Arrange
    pattern = r"is 'working'.*--drain-timeout SECONDS.*Tmux detach is always safe"

    def action():
        guard_managed_turn(
            _config(),
            allow_active_turn_kill=False,
            probe=lambda _config: _activity("working"),
        )

    # Act
    run = action
    # Assert
    with pytest.raises(ManagedTurnDrainRefusal, match=pattern):
        run()


def test_agent_stop_wires_guard_before_runtime_teardown(tmp_path: Path):
    # Arrange
    spec = _hermes_spec(tmp_path)
    runtime = HermesTuiSessionRuntime(multiplexer=_LiveMux())
    runtime.stop = lambda _config: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("runtime teardown reached after active-turn refusal")
    )

    def action():
        lc.agent_stop(
            "hub",
            registry=_Registry(spec),  # type: ignore[arg-type]
            runtime_factory=lambda _config: runtime,
            stop_instance_resolver=lambda _config, _runtime: None,
            managed_turn_probe=lambda _config: _activity("working"),
        )

    # Act
    run = action
    # Assert
    with pytest.raises(
        ManagedTurnDrainRefusal, match="session 'live-hub' is 'working'"
    ):
        run()


def test_bounded_drain_waits_until_native_session_is_idle():
    # Arrange
    observations = iter([_activity("working"), _activity("waiting"), _activity("idle")])
    clock = iter([0.0, 0.0, 0.5, 1.0, 1.0])
    sleeps = []

    # Act
    result = guard_managed_turn(
        _config(),
        allow_active_turn_kill=False,
        timeout_s=2.0,
        poll_s=0.5,
        probe=lambda _config: next(observations),
        monotonic_fn=lambda: next(clock),
        sleep_fn=sleeps.append,
    )

    # Assert
    assert (result and result.status, sleeps) == ("idle", [0.5, 0.5])


def test_unobservable_native_session_fails_closed():
    # Arrange
    def unavailable(_config):
        raise OSError("gateway socket disappeared")

    def action():
        guard_managed_turn(_config(), allow_active_turn_kill=False, probe=unavailable)

    # Act
    run = action
    # Assert
    with pytest.raises(
        ManagedTurnDrainRefusal, match=r"activity is unavailable.*--force"
    ):
        run()


def test_explicit_force_bypasses_probe_and_names_risk(caplog):
    # Arrange
    called = []
    # Act
    result = guard_managed_turn(
        _config(),
        allow_active_turn_kill=True,
        probe=lambda config: called.append(config) or _activity("working"),
    )

    # Assert
    assert (
        bool(result and result.forced),
        called,
        "prefix-cache residency may be lost" in caplog.text,
    ) == (True, [], True)


def test_non_hermes_harness_is_outside_this_guard():
    # Arrange
    def probe(_config):
        raise RuntimeError("non-Hermes harness unexpectedly probed")

    # Act
    result = guard_managed_turn(
        _config("codex"), allow_active_turn_kill=False, probe=probe
    )
    # Assert
    assert result is None


def test_remote_lifecycle_argv_preserves_explicit_drain_policy():
    # Arrange
    expected_stop = [
        "sac",
        "agents",
        "stop",
        "hub",
        "--json",
        "--drain-timeout",
        "12.5",
    ]
    expected_restart = [
        "sac",
        "agents",
        "restart",
        "hub",
        "--yes",
        "--json",
        "--drain-timeout",
        "12.5",
    ]
    # Act
    observed = (
        remote_stop_argv("hub", drain_timeout_s=12.5),
        remote_restart_argv("hub", drain_timeout_s=12.5),
    )
    # Assert
    assert observed == (expected_stop, expected_restart)


def test_remote_force_stop_stays_explicit_and_visible_in_peer_argv():
    # Arrange
    expected = "--force"
    # Act
    final_arg = remote_stop_argv("hub", force=True)[-1]
    # Assert
    assert final_arg == expected
