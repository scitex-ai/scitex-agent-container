"""Complete missing bootstrap phases on one already admitted native process.

This local maintainer API never launches, submits a turn, signals a process,
replaces a helper, changes a session/account, or clears a startup failure.
An exclusive attempt and every completed phase remain durable on partial
failure. The existing failure marker remains available for separate reviewed
retirement after the caller has inspected the genuine completion receipt.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import UUID

from .._runners._tmux._process_group import _identity
from .._state.registry import Registry
from ..runtimes._channel_inbox_dispatcher_lifecycle import (
    PID_FILENAME as INBOX_PID,
)
from ..runtimes._channel_inbox_dispatcher_lifecycle import (
    declared_durable_channels,
    start_inbox_dispatcher,
)
from ..runtimes._codex_activity_binding import assert_codex_binding_current
from ..runtimes._tui_cct_poller import (
    PID_FILENAME as CCT_PID,
)
from ..runtimes._tui_cct_poller import (
    cct_requested,
    start_cct_poller,
)
from ..runtimes._tui_recovery_guard import refuse_existing_helper
from ..runtimes._tui_turn_bridge_lifecycle import (
    PID_FILENAME as BRIDGE_PID,
)
from ..runtimes._tui_turn_bridge_lifecycle import (
    resolved_a2a_port,
    start_turn_bridge,
)
from ..runtimes.tui_session import state_dir_for_config
from ._bootstrap_evidence import admitted_native, spec_snapshot
from ._bootstrap_helpers import (
    ExistingBridge,
    bridge_health,
    unit_properties,
    verify_existing_bridge,
    verify_retention,
)
from ._bootstrap_journal import BootstrapJournal


def _list_instances(host):
    from .._state.state_store_instances import list_active_instances

    return list_active_instances(host=host)


def _read_instance(instance_id):
    from .._state.state_store_instances import read_instance

    return read_instance(instance_id)


def _postlaunch(config, runtime, registry):
    from ._handover_loader import _load_handover_module
    from ._start import _complete_started_runtime

    return _complete_started_runtime(
        config,
        runtime,
        registry=registry,
        config_path=config.config_path,
        hook_env={
            "SCITEX_AGENT_CONTAINER_CONFIG_PATH": config.config_path,
            "SCITEX_AGENT_CONTAINER_SCREEN_NAME": config.screen_name,
            "SCITEX_AGENT_CONTAINER_NAME": config.name,
        },
        handover_mod=_load_handover_module(),
    )


@dataclass(frozen=True)
class BootstrapPorts:
    """Real callable Store/postlaunch seams for isolated boundary qualification.

    Defaults use the same production primitives as normal start. This is a
    local Python API, not a remote command or a caller-provided shell program.
    """

    list_instances: Callable = _list_instances
    read_instance: Callable = _read_instance
    postlaunch: Callable = _postlaunch
    retain_helper: Callable | None = None
    show_unit: Callable = unit_properties
    bridge_health: Callable = bridge_health
    existing_bridge_source: Callable | None = None


def complete_native_bootstrap(
    config,
    runtime,
    *,
    admission,
    expected_spec_sha256: str,
    registry: Registry | None = None,
    ports: BootstrapPorts | None = None,
    observed_at: float | None = None,
    existing_bridge: ExistingBridge | None = None,
) -> dict:
    """Complete fixed normal phases once, preserving the captured launch/process.

    The caller must hold exclusive lifecycle authority. Existing Store APIs do
    not make a new atomic read/write guarantee; an active same-name local row
    or any retained helper/Registry claim refuses before helper effects.
    A dead historical helper claim must first be retired by its owner with an
    independent absence proof and exact file CAS; this API never guesses death.
    """
    source = spec_snapshot(config, expected_spec_sha256)
    binding, copies = admitted_native(
        config,
        runtime,
        admission,
        observed_at=time.time() if observed_at is None else observed_at,
    )
    registry = registry if registry is not None else Registry()
    ports = ports if ports is not None else BootstrapPorts()
    if ports.retain_helper is None:
        raise RuntimeError(
            "bootstrap completion requires a qualified persistent-helper retainer"
        )
    controller = _identity(os.getpid())
    if (
        controller is None
        or not controller.control_group
        or controller.control_group == "/"
    ):
        raise RuntimeError("bootstrap finite controller ownership is unknown")
    host = socket.gethostname()
    if registry.exists(config.name) or any(
        row.get("name") == config.name for row in ports.list_instances(host)
    ):
        raise RuntimeError("bootstrap completion refuses an existing registration")
    refuse_existing_helper(registry.dir / f"{config.name}.json", "Registry")
    state = state_dir_for_config(config)
    existing = None
    if existing_bridge is not None:
        if (
            ports.existing_bridge_source is None
            or ports.existing_bridge_source(config, existing_bridge) is not True
        ):
            raise RuntimeError("existing bridge native source compatibility is unknown")
        existing = verify_existing_bridge(
            config,
            state / BRIDGE_PID,
            existing_bridge,
            proc_root=admission.proc_root,
            health_fn=ports.bridge_health,
        )
    else:
        refuse_existing_helper(state / BRIDGE_PID, BRIDGE_PID)
    for filename in (INBOX_PID, CCT_PID):
        refuse_existing_helper(state / filename, filename)
    if resolved_a2a_port(config) is None:
        raise RuntimeError(
            "bootstrap completion requires the existing resolved A2A port"
        )
    journal = BootstrapJournal(state, binding.instance_id)
    completed = []
    phase = "claim"

    def fence():
        if spec_snapshot(config, expected_spec_sha256) != source:
            raise RuntimeError("bootstrap formal specification identity changed")
        assert_codex_binding_current(binding, admission.record)

    try:
        journal.write(
            "claim",
            {
                "launch_id": binding.instance_id,
                "name": config.name,
                "pane_pid": binding.pane.pid,
                "pane_birth": binding.pane.start_time,
                "native_pid": binding.native.pid,
                "native_birth": binding.native.start_time,
                "thread_id": binding.thread_id,
                "source_sha256": expected_spec_sha256,
                "mission_copy_count": copies,
            },
        )
        services = (
            ("bridge", runtime._turn_bridge_start or start_turn_bridge, True),
            (
                "inbox",
                runtime._inbox_dispatcher_start or start_inbox_dispatcher,
                bool(declared_durable_channels(config)),
            ),
            (
                "cct",
                runtime._cct_poller_start or start_cct_poller,
                cct_requested(config),
            ),
        )
        for phase, start, selected in services:
            fence()
            reused = phase == "bridge" and existing is not None
            pid = (
                existing.pid
                if reused
                else start(config, replace_existing=False)
                if selected
                else None
            )
            identity = (
                _identity(pid, proc_root=admission.proc_root)
                if type(pid) is int
                else None
            )
            if selected and (
                identity is None
                or identity.state == "Z"
                or identity.uid != binding.pane.uid
            ):
                raise RuntimeError(f"bootstrap {phase} has no live owned helper")
            retained = None
            if selected:
                retained = verify_retention(
                    identity,
                    ports.retain_helper(phase, identity, reused=reused),
                    controller=controller,
                    proc_root=admission.proc_root,
                    show_unit=ports.show_unit,
                )
            journal.write(
                phase,
                {
                    "selected": selected,
                    "pid": pid,
                    "birth": identity.start_time if identity else None,
                    "reused": reused,
                    "unit": retained.unit if retained else None,
                    "control_group": retained.control_group if retained else None,
                    "invocation_id": retained.invocation_id if retained else None,
                },
            )
            completed.append(phase)
            fence()
        phase = "registration"
        if registry.exists(config.name) or any(
            row.get("name") == config.name for row in ports.list_instances(host)
        ):
            raise RuntimeError("bootstrap registration appeared during completion")
        refuse_existing_helper(registry.dir / f"{config.name}.json", "Registry")
        instance_id = ports.postlaunch(config, runtime, registry)
        if (
            type(instance_id) is not str
            or str(UUID(instance_id)) != instance_id
            or instance_id == binding.instance_id
        ):
            raise RuntimeError(
                "bootstrap has no separately allocated canonical instance"
            )
        row = ports.read_instance(instance_id)
        expected = {
            "id": instance_id,
            "name": config.name,
            "host": host,
            "ended_at": None,
            "pid": binding.pane.pid,
            "process_start_time": binding.pane.start_time,
            "process_uid": binding.pane.uid,
            "control_group": binding.pane.control_group,
            "screen": admission.record["screen"],
        }
        if not isinstance(row, dict) or any(
            row.get(key) != value for key, value in expected.items()
        ):
            raise RuntimeError(
                "bootstrap canonical registration lacks exact process ownership"
            )
        registration = registry.get(config.name)
        if (
            not isinstance(registration, dict)
            or registration.get("config") != str(Path(config.config_path).resolve())
            or registration.get("screen") != config.screen_name
        ):
            raise RuntimeError("bootstrap Registry completion is absent or foreign")
        fence()
        journal.write(phase, {"instance_id": instance_id})
        completed.append(phase)
        result = {
            "completed": True,
            "launch_id": binding.instance_id,
            "instance_id": instance_id,
            "mission_copy_count": copies,
            "phases": completed,
        }
        journal.write("result", result)
        return result
    except Exception as error:
        # Persist the actual partial state, then propagate. Never undo a live
        # helper/registration or turn a refusal into a successful no-op.
        journal.write(
            "result",
            {
                "completed": False,
                "phase": phase,
                "phases": completed,
                "error_type": type(error).__name__,
            },
        )
        raise
    finally:
        journal.close()
