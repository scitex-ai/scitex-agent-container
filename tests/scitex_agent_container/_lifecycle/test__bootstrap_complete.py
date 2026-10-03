"""Real proc/FD/JSONL and file claims; synthetic Store and retention protocols."""

import hashlib
import json
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle._bootstrap_complete import (
    BootstrapPorts,
    complete_native_bootstrap,
)
from scitex_agent_container._lifecycle._bootstrap_evidence import source_mission
from scitex_agent_container._lifecycle._bootstrap_helpers import (
    ExistingBridge,
    RetainedHelper,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._codex_activity import CodexActivityError
from scitex_agent_container.runtimes._codex_startup_admission import (
    capture_startup_admission,
)
from scitex_agent_container.runtimes._sdk_channels import _TELEGRAMMER_CHANNEL
from scitex_agent_container.runtimes._tui_turn_bridge_lifecycle import MODULE_PATH
from scitex_agent_container.runtimes.tui_session import (
    TuiSessionRuntime,
    state_dir_for_config,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc
from tests.scitex_agent_container.runtimes.test__codex_activity_binding import (
    AGENT,
    INSTANCE,
    _layout,
    _process,
)
from tests.scitex_agent_container.runtimes.test__codex_startup_admission import (
    _accepted,
)

CANONICAL = "58391d81-4355-4d85-ac80-8c1d7c8d89f4"


class NativeMux:
    """Metadata only. Any runtime launch, stop, paste, or key is a failure."""

    def pane_pid(self, _name):
        return 4_001

    def __getattr__(self, name):
        raise AssertionError(f"unexpected native operation: {name}")


class Protocols:
    """In-memory database/retainer with real filesystem process observations."""

    def __init__(self, layout, config):
        self.layout, self.config = layout, config
        self.calls = []
        self.rows = {}
        self.units = {}
        self.fail = None
        self.foreign_row = False
        self.after_bridge = None

    def starter(self, service):
        def start(_config, *, replace_existing):
            if replace_existing is not False:
                raise AssertionError("recovery requested helper replacement")
            self.calls.append("start:" + service)
            if self.fail == service:
                raise RuntimeError("synthetic helper refusal")
            pid = {"bridge": 5_001, "inbox": 5_002, "cct": 5_003}[service]
            _process(self.layout["proc"], pid, 0, pid + 100, "python")
            if service == "bridge" and self.after_bridge:
                self.after_bridge()
            return pid

        return start

    def retain(self, service, identity, *, reused):
        self.calls.append("retain:" + service)
        unit = "sac-listen.service" if reused else f"retained-{service}.scope"
        cgroup = f"/user.slice/{unit}"
        (self.layout["proc"] / str(identity.pid) / "cgroup").write_text(
            f"0::{cgroup}\n"
        )
        retained = RetainedHelper(
            identity.pid, identity.start_time, identity.uid, cgroup, unit, "a" * 32
        )
        self.units[unit] = {
            "ControlGroup": cgroup,
            "InvocationID": "a" * 32,
            "ActiveState": "active",
            "RuntimeMaxUSec": "infinity",
        }
        return retained

    def list_instances(self, _host):
        return list(self.rows.values())

    def read_instance(self, instance_id):
        return self.rows.get(instance_id)

    def postlaunch(self, config, runtime, registry):
        self.calls.append("registration")
        registry.add(
            config.name, str(Path(config.config_path).resolve()), config.screen_name
        )
        self.rows[CANONICAL] = {
            "id": CANONICAL,
            "name": config.name,
            "host": socket.gethostname(),
            "ended_at": None,
            "pid": 4_001,
            "process_start_time": 100,
            "process_uid": os.getuid(),
            "control_group": "/user.slice/fixture.scope",
            "screen": "tui-" + config.name,
        }
        if self.foreign_row:
            self.rows[CANONICAL]["process_start_time"] = 999
        return CANONICAL

    def ports(self):
        return BootstrapPorts(
            self.list_instances,
            self.read_instance,
            self.postlaunch,
            self.retain,
            self.units.get,
            lambda _config: True,
            lambda _config, _authority: True,
        )


@pytest.fixture
def completion(tmp_path):
    layout = _layout(tmp_path)
    (tmp_path / ".git").mkdir()
    spec = tmp_path / ".scitex" / "agent-container" / "agents" / AGENT / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        yaml.safe_dump(
            explicit_doc(
                {
                    "harness": "codex",
                    "runtime": "tui",
                    "host": socket.gethostname(),
                    "startup_prompts": ["Read the public synthetic Card."],
                    "a2a": {"port": 19_023, "host": "127.0.0.1"},
                    "claude": {"channels": [_TELEGRAMMER_CHANNEL]},
                    "comms": {"channels": ["server:sac", _TELEGRAMMER_CHANNEL]},
                }
            )
        )
    )
    config = load_config(str(spec))
    config.env["SAC_INSTANCE_UUID"] = INSTANCE
    state = state_dir_for_config(config)
    state.mkdir(parents=True)
    marker = state / "STARTUP_FAILED"
    marker.write_bytes(b"original refused startup evidence")
    mission = source_mission(config)
    probe = capture_startup_admission(
        config, pane_pid=4_001, mission=mission, proc_root=layout["proc"], issued_at=20
    )
    _accepted(layout, mission=mission)
    protocols = Protocols(layout, config)
    runtime = TuiSessionRuntime(
        multiplexer=NativeMux(),
        turn_bridge_start=protocols.starter("bridge"),
        inbox_dispatcher_start=protocols.starter("inbox"),
        cct_poller_start=protocols.starter("cct"),
    )
    registry = Registry(tmp_path / "registry")
    return {
        "layout": layout,
        "config": config,
        "state": state,
        "marker": marker,
        "probe": probe,
        "protocols": protocols,
        "runtime": runtime,
        "registry": registry,
        "sha": hashlib.sha256(spec.read_bytes()).hexdigest(),
    }


def _complete(case, **kwargs):
    return complete_native_bootstrap(
        case["config"],
        case["runtime"],
        admission=case["probe"],
        expected_spec_sha256=case["sha"],
        registry=case["registry"],
        ports=case["protocols"].ports(),
        observed_at=100,
        **kwargs,
    )


def test_same_process_completion_records_a_distinct_canonical_identity(completion):
    # Arrange
    case = completion
    # Act
    result = _complete(case)
    # Assert
    assert (
        result["launch_id"],
        result["instance_id"],
        result["mission_copy_count"],
    ) == (INSTANCE, CANONICAL, 1)


def test_completion_leaves_original_refusal_bytes_untouched(completion):
    # Arrange
    case = completion
    before = case["marker"].read_bytes()
    # Act
    _complete(case)
    # Assert
    assert case["marker"].read_bytes() == before


def test_exact_two_native_source_missions_record_the_measured_copy_count(completion):
    # Arrange
    case = completion
    case["probe"] = replace(case["probe"], mission=case["probe"].mission * 2)
    _accepted(case["layout"], mission=case["probe"].mission)
    # Act
    result = _complete(case)
    # Assert
    assert result["mission_copy_count"] == 2


@pytest.mark.parametrize("suffix", [" unrelated context", "\n", "third-copy"])
def test_noncanonical_whole_native_mission_refuses_before_helpers(completion, suffix):
    # Arrange
    case = completion
    case["probe"] = replace(case["probe"], mission=case["probe"].mission + suffix)
    _accepted(case["layout"], mission=case["probe"].mission)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="source mandates"):
        _complete(case)


@pytest.mark.parametrize("suffix", [" unrelated context", "\n", "third-copy"])
def test_noncanonical_whole_native_mission_refuses_before_helpers_preserves_state(
    completion, suffix
):
    # Arrange
    case = completion
    case["probe"] = replace(case["probe"], mission=case["probe"].mission + suffix)
    _accepted(case["layout"], mission=case["probe"].mission)
    # Act
    try:
        _complete(case)
    except RuntimeError:
        pass
    # Assert
    assert case["protocols"].calls == []


@pytest.mark.parametrize(
    "filename",
    ["tui-turn-bridge.pid", "channel-inbox-dispatcher.pid", "tui-cct-poller.pid"],
)
def test_retained_unqualified_helper_claim_refuses_without_replacement(
    completion, filename
):
    # Arrange
    case = completion
    (case["state"] / filename).write_text("2153295")
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="existing"):
        _complete(case)


@pytest.mark.parametrize(
    "filename",
    ["tui-turn-bridge.pid", "channel-inbox-dispatcher.pid", "tui-cct-poller.pid"],
)
def test_retained_unqualified_helper_claim_refuses_without_replacement_preserves_state(
    completion, filename
):
    # Arrange
    case = completion
    (case["state"] / filename).write_text("2153295")
    # Act
    try:
        _complete(case)
    except RuntimeError:
        pass
    # Assert
    assert case["protocols"].calls == []


def test_owned_existing_bridge_is_retained_and_never_started_again(completion):
    # Arrange
    case = completion
    pid = 5_001
    process = _process(case["layout"]["proc"], pid, 0, 5_101, "python")
    argv = [
        "/public/consumer/python",
        "-m",
        MODULE_PATH,
        "--config-path",
        case["config"].config_path,
        "--port",
        "19023",
        "--host",
        "127.0.0.1",
    ]
    (process / "cmdline").write_bytes(
        b"\0".join(item.encode() for item in argv) + b"\0"
    )
    marker = case["state"] / "tui-turn-bridge.pid"
    marker.write_text(str(pid))
    meta = marker.stat()
    authority = ExistingBridge(
        pid,
        5_101,
        (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns),
        hashlib.sha256(marker.read_bytes()).hexdigest(),
    )
    # Act
    _complete(case, existing_bridge=authority)
    # Assert
    assert case["protocols"].calls == [
        "retain:bridge",
        "start:inbox",
        "retain:inbox",
        "start:cct",
        "retain:cct",
        "registration",
    ]


def test_partial_helper_failure_is_durable_and_never_registers(completion):
    # Arrange
    case = completion
    case["protocols"].fail = "inbox"
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="synthetic helper refusal"):
        _complete(case)


def test_partial_helper_failure_is_durable_and_never_registers_preserves_state(
    completion,
):
    # Arrange
    case = completion
    case["protocols"].fail = "inbox"
    # Act
    try:
        _complete(case)
    except RuntimeError:
        pass
    result = json.loads(
        next(
            case["state"].glob("native-bootstrap-completion-*/result.json")
        ).read_text()
    )
    # Assert
    assert result == {
        "completed": False,
        "phase": "inbox",
        "phases": ["bridge"],
        "error_type": "RuntimeError",
    }


def test_consumed_partial_attempt_refuses_before_restarting_any_helper(completion):
    # Arrange
    case = completion
    case["protocols"].fail = "inbox"
    try:
        _complete(case)
    except RuntimeError:
        pass
    # Act
    # Assert
    with pytest.raises(FileExistsError):
        _complete(case)


def test_consumed_partial_attempt_refuses_before_restarting_any_helper_preserves_state(
    completion,
):
    # Arrange
    case = completion
    case["protocols"].fail = "inbox"
    try:
        _complete(case)
    except RuntimeError:
        pass
    before = list(case["protocols"].calls)
    # Act
    try:
        _complete(case)
    except FileExistsError:
        pass
    # Assert
    assert case["protocols"].calls == before


def test_changed_native_birth_after_bridge_refuses_before_inbox(completion):
    # Arrange
    case = completion
    case["protocols"].after_bridge = lambda: _process(
        case["layout"]["proc"], 4_001, 0, 999, "apptainer"
    )
    # Act
    # Assert
    with pytest.raises(CodexActivityError, match="ownership changed"):
        _complete(case)


def test_changed_native_birth_after_bridge_refuses_before_inbox_preserves_state(
    completion,
):
    # Arrange
    case = completion
    case["protocols"].after_bridge = lambda: _process(
        case["layout"]["proc"], 4_001, 0, 999, "apptainer"
    )
    # Act
    try:
        _complete(case)
    except CodexActivityError:
        pass
    # Assert
    assert "start:inbox" not in case["protocols"].calls


def test_foreign_canonical_process_row_never_yields_completion_success(completion):
    # Arrange
    case = completion
    case["protocols"].foreign_row = True
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="exact process ownership"):
        _complete(case)


def test_foreign_canonical_process_row_never_yields_completion_success_preserves_state(
    completion,
):
    # Arrange
    case = completion
    case["protocols"].foreign_row = True
    # Act
    try:
        _complete(case)
    except RuntimeError:
        pass
    # Assert
    assert case["marker"].read_bytes() == b"original refused startup evidence"


def test_existing_same_name_canonical_registration_refuses_without_superseding(
    completion,
):
    # Arrange
    case = completion
    case["protocols"].rows[CANONICAL] = {"name": case["config"].name}
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="existing registration"):
        _complete(case)


def test_existing_same_name_canonical_registration_refuses_without_superseding_preserves_state(
    completion,
):
    # Arrange
    case = completion
    case["protocols"].rows[CANONICAL] = {"name": case["config"].name}
    # Act
    try:
        _complete(case)
    except RuntimeError:
        pass
    # Assert
    assert case["protocols"].calls == []


def test_missing_persistent_retainer_refuses_before_any_helper_effect(completion):
    # Arrange
    case = completion
    ports = replace(case["protocols"].ports(), retain_helper=None)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="persistent-helper retainer"):
        complete_native_bootstrap(
            case["config"],
            case["runtime"],
            admission=case["probe"],
            expected_spec_sha256=case["sha"],
            registry=case["registry"],
            ports=ports,
            observed_at=100,
        )


def test_missing_persistent_retainer_refuses_before_any_helper_effect_preserves_state(
    completion,
):
    # Arrange
    case = completion
    ports = replace(case["protocols"].ports(), retain_helper=None)
    # Act
    try:
        complete_native_bootstrap(
            case["config"],
            case["runtime"],
            admission=case["probe"],
            expected_spec_sha256=case["sha"],
            registry=case["registry"],
            ports=ports,
            observed_at=100,
        )
    except RuntimeError:
        pass
    # Assert
    assert case["protocols"].calls == []


def test_old_healthy_bridge_without_source_qualification_refuses_before_adoption(
    completion,
):
    # Arrange
    case = completion
    ports = replace(case["protocols"].ports(), existing_bridge_source=None)
    authority = ExistingBridge(5_001, 5_101, (0, 0, 4, 0), "0" * 64)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="source compatibility is unknown"):
        complete_native_bootstrap(
            case["config"],
            case["runtime"],
            admission=case["probe"],
            expected_spec_sha256=case["sha"],
            registry=case["registry"],
            ports=ports,
            observed_at=100,
            existing_bridge=authority,
        )


def test_old_healthy_bridge_without_source_qualification_refuses_before_adoption_preserves_state(
    completion,
):
    # Arrange
    case = completion
    ports = replace(case["protocols"].ports(), existing_bridge_source=None)
    authority = ExistingBridge(5_001, 5_101, (0, 0, 4, 0), "0" * 64)
    # Act
    try:
        complete_native_bootstrap(
            case["config"],
            case["runtime"],
            admission=case["probe"],
            expected_spec_sha256=case["sha"],
            registry=case["registry"],
            ports=ports,
            observed_at=100,
            existing_bridge=authority,
        )
    except RuntimeError:
        pass
    # Assert
    assert case["protocols"].calls == []
