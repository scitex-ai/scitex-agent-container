"""Owned process metadata and real loader/lifecycle refusal boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._api_salvage import SalvageIdentity, _restart
from scitex_agent_container._lifecycle._engine_select import check_engine_before_stop
from scitex_agent_container._lifecycle._managed_turn_drain import (
    ManagedTurnDrainRefusal,
)
from scitex_agent_container._lifecycle._restart_preflight import (
    preflight_from_config_path,
)
from scitex_agent_container._lifecycle._selected_harness import (
    SelectedRuntimeFence,
    require_selected_runtime,
    require_selected_stop_target,
    require_selected_successor_down,
)
from scitex_agent_container._lifecycle._stop import agent_restart, agent_stop
from scitex_agent_container._lifecycle._stop_escalate import (
    ensure_previous_runtime_down,
)
from scitex_agent_container._runners._tmux._process_group import _identity
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._hermes_tui_rpc import HermesTurnActivity
from scitex_agent_container.runtimes._native_tui_admission import (
    preflight_native_tui_from_config_path,
)
from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime

from ..config.test__selected_harness import _write_declaration


@pytest.fixture
def owned_route(tmp_path):
    path, _raw = _write_declaration(tmp_path)
    previous = os.environ.get("TEST_GO_TWO")
    os.environ["TEST_GO_TWO"] = "synthetic-owned-key"
    try:
        config = load_config(path, harness_override="hermes", engine_override="go-muse")
        process = _identity(os.getpid())
        if process is None:
            raise RuntimeError("owned-test-process-metadata-unavailable")
        fence = SelectedRuntimeFence(
            config.name,
            "owned-instance",
            "owned-session",
            "owned-boot",
            "hermes",
            "go-muse",
            "opencode-go",
            config.model,
            "TEST_GO_ONE",
            process.pid,
            process.start_time,
            process.uid,
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
        registry = Registry(tmp_path / "registry")
        registry.add(config.name, str(path), config.screen_name)
        yield path, config, fence, registry
    finally:
        if previous is None:
            os.environ.pop("TEST_GO_TWO", None)
        else:
            os.environ["TEST_GO_TWO"] = previous


def test_owned_observed_hermes_route_is_admitted(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route
    # Act
    result = require_selected_runtime(config, fence, lambda _config: fence)
    # Assert
    assert result is None


def test_matching_observer_from_another_host_boot_refuses(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route
    previous_host = replace(fence, host_boot_id="00000000-0000-0000-0000-000000000000")

    # Act
    def operation():
        require_selected_runtime(config, previous_host, lambda _config: previous_host)

    # Assert
    with pytest.raises(ValueError, match="host-boot-mismatch"):
        operation()


def test_canonical_row_cannot_override_a_foreign_host_boot(owned_route):
    # Arrange
    _path, _config, fence, _registry = owned_route
    previous_host = replace(fence, host_boot_id="00000000-0000-0000-0000-000000000000")

    # Act
    def operation():
        require_selected_stop_target(previous_host, _canonical_instance(previous_host))

    # Assert
    with pytest.raises(ValueError, match="host-boot-mismatch"):
        operation()


def _canonical_instance(fence):
    return {
        "id": fence.instance_id,
        "name": fence.agent,
        "pid": fence.pid,
        "process_start_time": fence.process_start_time,
        "process_uid": fence.process_uid,
    }


def test_canonical_stop_target_matches_owned_fence(owned_route):
    # Arrange
    _path, _config, fence, _registry = owned_route
    # Act
    result = require_selected_stop_target(fence, _canonical_instance(fence))
    # Assert
    assert result is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "native-instance"),
        ("name", "native-lead"),
        ("pid", 1),
        ("process_start_time", 1),
        ("process_uid", -1),
    ],
)
def test_foreign_canonical_stop_target_refuses(owned_route, field, value):
    # Arrange
    _path, _config, fence, _registry = owned_route
    instance = {**_canonical_instance(fence), field: value}

    # Act
    def operation():
        require_selected_stop_target(fence, instance)

    # Assert
    with pytest.raises(ValueError, match="stop-target-mismatch"):
        operation()


def test_missing_canonical_stop_target_refuses(owned_route):
    # Arrange
    _path, _config, fence, _registry = owned_route

    # Act
    def operation():
        require_selected_stop_target(fence, None)

    # Assert
    with pytest.raises(ValueError, match="stop-target-unknown"):
        operation()


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "foreign-session"),
        ("instance_id", "foreign-instance"),
        ("boot_id", "foreign-boot"),
        ("model", "another-model"),
        ("provider", "commandcode"),
        ("account", "TEST_GO_TWO"),
        ("harness", "codex"),
        ("engine", "another-engine"),
    ],
)
def test_changed_observed_runtime_or_route_refuses(owned_route, field, value):
    # Arrange
    _path, config, fence, _registry = owned_route
    observed = replace(fence, **{field: value})

    # Act
    def operation():
        require_selected_runtime(config, fence, lambda _config: observed)

    # Assert
    with pytest.raises(ValueError, match="runtime-fence-changed"):
        operation()


@pytest.mark.parametrize("observed", [None, {}, True])
def test_unknown_observation_is_not_restart_authority(owned_route, observed):
    # Arrange
    _path, config, fence, _registry = owned_route

    # Act
    def operation():
        require_selected_runtime(config, fence, lambda _config: observed)

    # Assert
    with pytest.raises(ValueError, match="runtime-fence-changed"):
        operation()


def test_stale_pid_incarnation_refuses(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route
    old = replace(fence, process_start_time=fence.process_start_time + 1)

    # Act
    def operation():
        require_selected_runtime(config, old, lambda _config: old)

    # Assert
    with pytest.raises(ValueError, match="process-incarnation-changed"):
        operation()


def test_other_os_user_refuses(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route
    foreign = replace(fence, process_uid=fence.process_uid + 1)

    # Act
    def operation():
        require_selected_runtime(config, foreign, lambda _config: foreign)

    # Assert
    with pytest.raises(ValueError, match="runtime-refused"):
        operation()


def test_terminated_owned_process_refuses(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=2)
    stopped = replace(fence, pid=child.pid)

    # Act
    def operation():
        require_selected_runtime(config, stopped, lambda _config: stopped)

    # Assert
    with pytest.raises(ValueError, match="process-incarnation-changed"):
        operation()


def test_native_default_cannot_be_fenced_as_hermes(owned_route):
    # Arrange
    path, _config, fence, _registry = owned_route
    native = load_config(path)

    # Act
    def operation():
        require_selected_runtime(native, fence, lambda _config: fence)

    # Assert
    with pytest.raises(ValueError, match="runtime-refused"):
        operation()


def test_pre_stop_auth_uses_selected_api_harness(owned_route):
    # Arrange
    path, _config, _fence, _registry = owned_route
    # Act
    result = preflight_from_config_path(
        str(path), harness_override="hermes", engine_override="go-muse"
    )
    # Assert
    assert result is None


def test_pre_stop_engine_uses_selected_api_engine(owned_route):
    # Arrange
    path, _config, _fence, _registry = owned_route
    # Act
    result = check_engine_before_stop(
        str(path), "go-muse", harness_override="hermes", probe=False
    )
    # Assert
    assert result is None


def test_native_admission_does_not_substitute_codex(owned_route):
    # Arrange
    path, _config, _fence, _registry = owned_route
    # Act
    result = preflight_native_tui_from_config_path(
        str(path), harness_override="hermes", engine_override="go-muse"
    )
    # Assert
    assert result is None


def test_second_fence_refuses_change_after_preflights(owned_route):
    # Arrange
    _path, config, fence, registry = owned_route
    observations = iter([fence, replace(fence, account="TEST_GO_TWO")])

    # Act
    def operation():
        agent_restart(
            config.name,
            registry,
            harness_override="hermes",
            engine_override="go-muse",
            probe_engine=False,
            expected_runtime=fence,
            observe_runtime=lambda _config: next(observations),
        )

    # Assert
    with pytest.raises(ValueError, match="runtime-fence-changed"):
        operation()


def test_override_without_owned_fence_refuses_before_auth_or_stop(owned_route):
    # Arrange
    _path, config, _fence, registry = owned_route

    # Act
    def operation():
        agent_restart(
            config.name, registry, harness_override="hermes", engine_override="go-muse"
        )

    # Assert
    with pytest.raises(ValueError, match="owned-runtime-required"):
        operation()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_selected_restart_cannot_skip_teardown_verification(owned_route, timeout):
    # Arrange
    _path, config, fence, registry = owned_route

    # Act
    def operation():
        agent_restart(
            config.name,
            registry,
            harness_override="hermes",
            engine_override="go-muse",
            expected_runtime=fence,
            observe_runtime=lambda _config: fence,
            wait_for_stop_timeout_s=timeout,
        )

    # Assert
    with pytest.raises(ValueError, match="stop-verification-required"):
        operation()


class _OwnedProcessMux:
    def __init__(self, pid):
        self.pid = pid

    def exists(self, _name):
        return _identity(self.pid) is not None


def test_selected_stop_keeps_active_hermes_turn_guard(owned_route):
    # Arrange
    _path, config, fence, registry = owned_route
    runtime = HermesTuiSessionRuntime(multiplexer=_OwnedProcessMux(fence.pid))
    activity = HermesTurnActivity(
        state="active", session_status="working", session_id=fence.session_id
    )

    # Act
    def operation():
        agent_stop(
            config.name,
            registry,
            force=True,
            harness_override="hermes",
            engine_override="go-muse",
            runtime_factory=lambda _config: runtime,
            stop_instance_resolver=lambda _config, _runtime: None,
            allow_active_turn_kill=False,
            managed_turn_probe=lambda _config: activity,
        )

    # Assert
    with pytest.raises(ManagedTurnDrainRefusal, match="working"):
        operation()


def test_selected_stop_refuses_shared_name_native_instance_before_teardown(owned_route):
    # Arrange
    _path, config, fence, registry = owned_route
    runtime = HermesTuiSessionRuntime(multiplexer=_OwnedProcessMux(fence.pid))
    instance = {**_canonical_instance(fence), "id": "productive-native-instance"}

    # Act
    def operation():
        agent_stop(
            config.name,
            registry,
            force=True,
            harness_override="hermes",
            engine_override="go-muse",
            expected_runtime=fence,
            runtime_factory=lambda _config: runtime,
            stop_instance_resolver=lambda _config, _runtime: instance,
        )

    # Assert
    with pytest.raises(ValueError, match="stop-target-mismatch"):
        operation()


def test_selected_settle_refuses_an_unloadable_projection(owned_route):
    # Arrange
    path, config, _fence, _registry = owned_route
    path.write_text("not a config")

    # Act
    def operation():
        ensure_previous_runtime_down(
            config.name,
            str(path),
            harness_override="hermes",
            engine_override="go-muse",
            runtime_factory=None,
            sleep_fn=lambda _seconds: None,
            timeout_s=1,
        )

    # Assert
    with pytest.raises(ValueError, match="canonical-document-invalid"):
        operation()


def test_observer_error_is_fixed_and_does_not_echo_private_text(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route

    def unavailable(_config):
        raise RuntimeError("synthetic-private-provider-detail")

    # Act
    def operation():
        require_selected_runtime(config, fence, unavailable)

    # Assert
    with pytest.raises(ValueError, match="^selected-harness-runtime-unobservable$"):
        operation()


def test_salvage_uses_normal_fenced_override_without_spec_rewrite(owned_route):
    # Arrange
    _path, config, fence, registry = owned_route
    identity = SalvageIdentity(
        fence.agent,
        fence.instance_id,
        fence.session_id,
        fence.boot_id,
        fence.harness,
        fence.engine,
        fence.provider,
        fence.model,
    )
    calls = []

    def restart(_name, _registry, **kwargs):
        calls.append(kwargs)
        return True

    # Act
    _restart(
        config,
        identity,
        registry=registry,
        restart=restart,
        expected_runtime=fence,
        observe_runtime=lambda _config: fence,
    )
    # Assert
    assert calls[0]["harness_override"] == "hermes"


@pytest.fixture
def matching_default_route(owned_route):
    import yaml

    path, _config, fence, registry = owned_route
    raw = yaml.safe_load(path.read_text())
    raw["spec"]["harness"] = "hermes"
    raw["spec"]["engine"] = "go-other"
    raw["spec"]["available_engines"]["go-other"] = {
        **raw["spec"]["available_engines"]["go-muse"],
        "provider": {
            "hermes_provider": "opencode-go",
            "auth_token_env": "TEST_GO_OTHER",
        },
    }
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    return path, config, fence, registry


def test_matching_hermes_default_still_uses_observed_engine_before_stop(
    matching_default_route,
):
    # Arrange
    _path, config, fence, registry = matching_default_route
    projections = []

    def changed_after_preflights(selected):
        projections.append(selected.engine_key)
        return fence if len(projections) == 1 else replace(fence, account="TEST_GO_TWO")

    # Act
    try:
        agent_restart(
            config.name,
            registry,
            expected_runtime=fence,
            observe_runtime=changed_after_preflights,
        )
    except ValueError:
        pass
    # Assert
    assert projections == ["go-muse", "go-muse"]


def test_matching_default_salvage_passes_observed_projection_to_restart(
    matching_default_route,
):
    # Arrange
    _path, config, fence, registry = matching_default_route
    identity = SalvageIdentity(
        fence.agent,
        fence.instance_id,
        fence.session_id,
        fence.boot_id,
        fence.harness,
        fence.engine,
        fence.provider,
        fence.model,
    )
    calls = []

    def restart(_name, _registry, **kwargs):
        calls.append(kwargs)
        return True

    # Act
    _restart(
        config,
        identity,
        registry=registry,
        restart=restart,
        expected_runtime=fence,
        observe_runtime=lambda _config: fence,
    )
    # Assert
    assert calls[0]["harness_override"] == "hermes"


def test_matching_default_salvage_passes_observed_engine_to_restart(
    matching_default_route,
):
    # Arrange
    _path, config, fence, registry = matching_default_route
    identity = SalvageIdentity(
        fence.agent,
        fence.instance_id,
        fence.session_id,
        fence.boot_id,
        fence.harness,
        fence.engine,
        fence.provider,
        fence.model,
    )
    calls = []

    def restart(_name, _registry, **kwargs):
        calls.append(kwargs)
        return True

    # Act
    _restart(
        config,
        identity,
        registry=registry,
        restart=restart,
        expected_runtime=fence,
        observe_runtime=lambda _config: fence,
    )
    # Assert
    assert calls[0]["engine_override"] == "go-muse"


def test_matching_default_salvage_without_real_observer_refuses(matching_default_route):
    # Arrange
    _path, config, fence, registry = matching_default_route
    identity = SalvageIdentity(
        fence.agent,
        fence.instance_id,
        fence.session_id,
        fence.boot_id,
        fence.harness,
        fence.engine,
        fence.provider,
        fence.model,
    )

    # Act
    def operation():
        _restart(config, identity, registry=registry)

    # Assert
    with pytest.raises(ValueError, match="owned-runtime-required"):
        operation()


def test_fenced_matching_default_restart_cannot_skip_stop_gate(matching_default_route):
    # Arrange
    _path, config, fence, registry = matching_default_route

    # Act
    def operation():
        agent_restart(
            config.name,
            registry,
            expected_runtime=fence,
            observe_runtime=lambda _config: fence,
            wait_for_stop_timeout_s=0,
        )

    # Assert
    with pytest.raises(ValueError, match="stop-verification-required"):
        operation()


def test_reappearing_owned_runtime_refuses_selected_force_start(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route
    runtime = HermesTuiSessionRuntime(multiplexer=_OwnedProcessMux(fence.pid))

    # Act
    def operation():
        require_selected_successor_down(
            config, runtime, force=True, harness_override="hermes"
        )

    # Assert
    with pytest.raises(RuntimeError, match="runtime-reappeared-before-start"):
        operation()


def test_unfenced_default_force_start_guard_keeps_legacy_behavior(owned_route):
    # Arrange
    _path, config, fence, _registry = owned_route
    runtime = HermesTuiSessionRuntime(multiplexer=_OwnedProcessMux(fence.pid))
    # Act
    result = require_selected_successor_down(
        config, runtime, force=True, harness_override=None
    )
    # Assert
    assert result is None
