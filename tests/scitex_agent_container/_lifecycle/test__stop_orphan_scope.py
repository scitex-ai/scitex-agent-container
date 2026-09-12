from __future__ import annotations

from pathlib import Path

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._lifecycle._stop_outcome import (
    StopVerificationError,
    verify_tui_incarnation_stopped,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml


class _NoTmuxRuntime(TuiSessionRuntime):
    def __init__(self) -> None:
        pass

    def stop(self, _config) -> bool:
        return False

    def is_running(self, _config) -> bool:
        return False


class _NoHandover:
    def push_pre_stop_snapshot(self, _config) -> bool:
        return True


def _spec(tmp_path: Path) -> Path:
    root = tmp_path / "scitex-app"
    root.mkdir()
    path = root / "spec.yaml"
    path.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: tui\n"
            "  host: ${HOSTNAME}\n"
            f"  workdir: {tmp_path}\n"
            "  apptainer:\n"
            "    image: /x.sif\n"
            "    binds: []\n"
            "  claude:\n"
            "    model: sonnet\n"
            "  health:\n"
            "    enabled: false\n"
            "    interval: 60\n"
            "  restart:\n"
            "    policy: never\n"
            "    max_retries: 0\n"
            "  hooks:\n"
            "    pre_start: []\n"
            "    post_start: []\n"
            "    pre_stop: []\n"
            "    post_stop: []\n"
        )
    )
    return path


def test_force_stop_preserves_state_when_orphan_scope_survives(tmp_path: Path) -> None:
    # Arrange
    registry = Registry(registry_dir=tmp_path / "registry")
    spec = _spec(tmp_path)
    registry.add("scitex-app", str(spec), "tui-scitex-app")
    instance = {
        "id": "01991c62-61ab-7abc-8000-000000000001",
        "host": "scitex-compute-03",
        "process_start_time": 17,
        "process_uid": 1000,
        "control_group": "/user.slice/tmux-spawn-a.scope",
        "scope_invocation_id": "a" * 32,
        "scope_unit": "tmux-spawn-a.scope",
    }

    def verifier(**fields) -> str:
        return verify_tui_incarnation_stopped(
            **fields,
            ensure_scope_down=lambda _row: False,
            outcome_recorder=lambda **_outcome: None,
        )

    # Act
    try:
        lc.agent_stop(
            "scitex-app",
            registry=registry,
            force=True,
            runtime_factory=lambda _config: _NoTmuxRuntime(),
            handover_mod=_NoHandover(),
            stop_instance_resolver=lambda _config, _runtime: instance,
            tui_stop_verifier=verifier,
        )
    except Exception as exc:  # noqa: BLE001 - assertion names exact type
        error = exc
    else:
        error = None
    # Assert
    assert (
        isinstance(error, StopVerificationError),
        registry.exists("scitex-app"),
    ) == (
        True,
        True,
    )


def test_force_stop_rejects_legacy_row_before_runtime_signal(tmp_path: Path) -> None:
    # Arrange
    registry = Registry(registry_dir=tmp_path / "registry")
    spec = _spec(tmp_path)
    registry.add("scitex-app", str(spec), "tui-scitex-app")
    runtime = _NoTmuxRuntime()
    runtime_calls: list[bool] = []
    runtime.stop = lambda _config: runtime_calls.append(True)  # type: ignore[method-assign]
    legacy = {
        "id": "f571b488-8683-4e57-a6f6-1a12870226c4",
        "host": "scitex-compute-03",
        "pid": 904612,
    }

    def verifier(**fields) -> str:
        return verify_tui_incarnation_stopped(
            **fields,
            outcome_recorder=lambda **_outcome: None,
        )

    # Act
    try:
        lc.agent_stop(
            "scitex-app",
            registry=registry,
            force=True,
            runtime_factory=lambda _config: runtime,
            handover_mod=_NoHandover(),
            stop_instance_resolver=lambda _config, _runtime: legacy,
            tui_stop_verifier=verifier,
        )
    except Exception as exc:  # noqa: BLE001 - assertion names exact type
        error = exc
    else:
        error = None
    # Assert
    assert (
        isinstance(error, StopVerificationError),
        registry.exists("scitex-app"),
        runtime_calls,
    ) == (True, True, [])
