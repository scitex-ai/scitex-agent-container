"""Retained bridge argv/marker and real helper/unit ownership counterexamples."""

import hashlib
import os
from dataclasses import replace

import pytest

from scitex_agent_container._lifecycle._bootstrap_helpers import (
    ExistingBridge,
    RetainedHelper,
    verify_existing_bridge,
    verify_retention,
)
from scitex_agent_container._runners._tmux._process_group import _identity
from scitex_agent_container.runtimes._channel_inbox_dispatcher_lifecycle import (
    start_inbox_dispatcher,
)
from scitex_agent_container.runtimes._tui_cct_poller import start_cct_poller
from scitex_agent_container.runtimes._tui_turn_bridge_lifecycle import (
    MODULE_PATH,
    start_turn_bridge,
)
from tests.scitex_agent_container.runtimes.test__codex_activity_binding import _process

from . import test__bootstrap_complete as completion_source

completion = completion_source.completion


def _existing(case):
    process = _process(case["layout"]["proc"], 5_001, 0, 5_101, "python")
    argv = [
        "/public/python",
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
    marker.write_text("5001")
    info = marker.stat()
    authority = ExistingBridge(
        5_001,
        5_101,
        (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
        hashlib.sha256(marker.read_bytes()).hexdigest(),
    )
    return process, marker, authority


def test_actual_bridge_module_marker_and_health_bind_the_live_incarnation(completion):
    # Arrange
    case = completion
    _, marker, authority = _existing(case)
    # Act
    identity = verify_existing_bridge(
        case["config"],
        marker,
        authority,
        proc_root=case["layout"]["proc"],
        health_fn=lambda _config: True,
    )
    # Assert
    assert (identity.pid, identity.start_time) == (5_001, 5_101)


@pytest.mark.parametrize(
    "field,value", [("pid", 5_002), ("birth", 999), ("marker_sha256", "0" * 64)]
)
def test_changed_existing_bridge_authority_is_refused(completion, field, value):
    # Arrange
    case = completion
    _, marker, authority = _existing(case)
    authority = replace(authority, **{field: value})
    # Act
    # Assert
    with pytest.raises(RuntimeError):
        verify_existing_bridge(
            case["config"],
            marker,
            authority,
            proc_root=case["layout"]["proc"],
            health_fn=lambda _config: True,
        )


def test_same_byte_replaced_bridge_marker_is_not_the_reviewed_file(completion):
    # Arrange
    case = completion
    _, marker, authority = _existing(case)
    marker.rename(marker.with_suffix(".retained"))
    marker.write_text("5001")
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="ownership changed"):
        verify_existing_bridge(
            case["config"],
            marker,
            authority,
            proc_root=case["layout"]["proc"],
            health_fn=lambda _config: True,
        )


@pytest.mark.parametrize("wrong", ["--config-path", "--port", "--host"])
def test_live_bridge_with_foreign_config_or_endpoint_refuses(completion, wrong):
    # Arrange
    case = completion
    process, marker, authority = _existing(case)
    argv = (process / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
    argv[argv.index(wrong) + 1] = "foreign"
    (process / "cmdline").write_bytes(
        b"\0".join(item.encode() for item in argv) + b"\0"
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="authority differs"):
        verify_existing_bridge(
            case["config"],
            marker,
            authority,
            proc_root=case["layout"]["proc"],
            health_fn=lambda _config: True,
        )


def test_tcp_listener_without_actual_get_health_is_not_an_owned_ready_bridge(
    completion,
):
    # Arrange
    case = completion
    _, marker, authority = _existing(case)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="GET-only readiness"):
        verify_existing_bridge(
            case["config"],
            marker,
            authority,
            proc_root=case["layout"]["proc"],
            health_fn=lambda _config: False,
        )


def _retention_fixture(case):
    proc = case["layout"]["proc"]
    _process(proc, 5_001, 0, 5_101, "python", cgroup="/user.slice/retained.scope")
    _process(proc, 6_001, 0, 6_101, "python", cgroup="/user.slice/controller.service")
    before = _identity(5_001, proc_root=proc)
    controller = _identity(6_001, proc_root=proc)
    receipt = RetainedHelper(
        5_001,
        5_101,
        os.getuid(),
        "/user.slice/retained.scope",
        "retained.scope",
        "a" * 32,
    )
    shown = {
        "ControlGroup": receipt.control_group,
        "InvocationID": receipt.invocation_id,
        "ActiveState": "active",
        "RuntimeMaxUSec": "infinity",
    }
    return before, controller, receipt, shown


def test_persistent_scope_is_independently_read_back_with_same_birth(completion):
    # Arrange
    case = completion
    before, controller, receipt, shown = _retention_fixture(case)
    # Act
    retained = verify_retention(
        before,
        receipt,
        controller=controller,
        proc_root=case["layout"]["proc"],
        show_unit=lambda _unit: shown,
    )
    # Assert
    assert retained == receipt


@pytest.mark.parametrize(
    "key,value",
    [
        ("ActiveState", "inactive"),
        ("RuntimeMaxUSec", "30s"),
        ("InvocationID", "b" * 32),
        ("PartOf", "controller.service"),
        ("BindsTo", "controller.service"),
        ("Requires", "controller.service"),
    ],
)
def test_unknown_expiring_or_controller_dependent_retention_refuses(
    completion, key, value
):
    # Arrange
    case = completion
    before, controller, receipt, shown = _retention_fixture(case)
    shown[key] = value
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="unit readback"):
        verify_retention(
            before,
            receipt,
            controller=controller,
            proc_root=case["layout"]["proc"],
            show_unit=lambda _unit: shown,
        )


def test_sets_id_helper_still_in_controller_cgroup_is_refused(completion):
    # Arrange
    case = completion
    before, controller, receipt, shown = _retention_fixture(case)
    (case["layout"]["proc"] / "5001" / "cgroup").write_text(
        "0::/user.slice/controller.service\n"
    )
    receipt = replace(
        receipt, control_group=controller.control_group, unit="controller.service"
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="finite controller cgroup"):
        verify_retention(
            before,
            receipt,
            controller=controller,
            proc_root=case["layout"]["proc"],
            show_unit=lambda _unit: shown,
        )


@pytest.mark.parametrize(
    "filename,start",
    [
        ("tui-turn-bridge.pid", start_turn_bridge),
        ("channel-inbox-dispatcher.pid", start_inbox_dispatcher),
        ("tui-cct-poller.pid", start_cct_poller),
    ],
)
def test_actual_recovery_starter_refuses_claim_before_credentials_or_spawn(
    completion, filename, start
):
    # Arrange
    case = completion
    (case["state"] / filename).write_text("5001")
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="existing"):
        start(case["config"], replace_existing=False)
