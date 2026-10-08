"""Restart cleanup rechecks Hermes activity when a live runtime appears."""

from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._managed_turn_drain import (
    ManagedTurnDrainRefusal,
)
from scitex_agent_container._lifecycle._start import agent_start
from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    UNKNOWN,
    INSTRUMENT_HOST_TMUX,
    SOURCE_PROCESS,
    Signal,
    decide,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.runtimes._hermes_tui_rpc import HermesTurnActivity
from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml
from tests.scitex_agent_container._helpers.spec_authority import (
    establish_test_spec_authority,
)


class _LiveMux:
    @staticmethod
    def exists(_name: str) -> bool:
        return True


class _ProtectedHermesRuntime(HermesTuiSessionRuntime):
    def stop(self, config):
        raise AssertionError("runtime teardown reached after active-turn refusal")


def _spec(tmp_path: Path) -> Path:
    root = tmp_path / "replacement-guard-hub"
    root.mkdir()
    path = root / "spec.yaml"
    path.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: tui\n"
            "  harness: hermes\n"
            "  host: ${HOSTNAME}\n"
            f"  workdir: {tmp_path}\n"
            "  a2a:\n"
            "    port: null\n"
        )
    )
    return establish_test_spec_authority(path)


@pytest.mark.parametrize("liveness", [ALIVE, UNKNOWN])
def test_internal_restart_cleanup_refuses_a_newly_busy_hermes_runtime(
    tmp_path, env_save_restore, liveness
):
    # Arrange
    spec = _spec(tmp_path)
    name = spec.parent.name
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "runtime")
    )
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add(name, str(spec), f"tui-{name}")
    runtime = _ProtectedHermesRuntime(multiplexer=_LiveMux())
    verdict = decide(
        name,
        [Signal(SOURCE_PROCESS, liveness, "restart recheck", INSTRUMENT_HOST_TMUX)],
    )
    activity = HermesTurnActivity("active", "working", "successor-live-turn")

    def action():
        agent_start(
            str(spec),
            registry=registry,
            force=True,
            # Inspection avoids unrelated ACL/port writes; the real native
            # cleanup stop and activity guard still execute on both branches.
            dry_run=True,
            runtime_factory=lambda _config: runtime,
            verdict_override=verdict,
            successor_auth_check=lambda _config: None,
            managed_turn_probe=lambda _config: activity,
            stop_instance_resolver=lambda _config, _runtime: None,
        )

    # Act
    run = action
    # Assert
    with pytest.raises(ManagedTurnDrainRefusal, match="successor-live-turn.*working"):
        run()
