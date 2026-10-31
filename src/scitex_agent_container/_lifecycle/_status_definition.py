"""Local definition status, independent of an unbound fleet placement row."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .._state.observation import DefinitionState, build_agent_observation
from ..config import AgentConfig, resolve_hostname
from ._runtime_identity import resolve_runtime_identity
from ._verdict import (
    INSTRUMENT_NO_OBSERVATION,
    SOURCE_PROCESS,
    UNKNOWN,
    Signal,
    decide,
)


def _placements(name: str, instance_reader: Callable[[], list[dict]] | None) -> dict:
    """Keep every matching unended row as placement, never local death proof."""
    try:
        if instance_reader is None:
            from .._state.state_store import list_active_instances

            instance_reader = list_active_instances
        records = [
            {
                "id": row.get("id"),
                "host": row.get("host") or "",
                "screen": row.get("screen") or "",
                "started_at": row.get("started_at") or "",
                "bound_port": row.get("bound_port"),
                "remote": bool(row.get("remote")),
                "spawned_by": row.get("spawned_by"),
                "liveness": "unknown",
            }
            for row in instance_reader()
            if row.get("name") == name
        ]
        return {"source": "active-instances", "state": "observed", "records": records}
    except Exception:  # stx-allow: fallback (a failed placement read is unknown, not evidence of absence)
        return {"source": "active-instances", "state": "unknown", "records": []}


def defined_status(
    name: str,
    path: str,
    config: AgentConfig,
    *,
    runtime_factory: Callable[[AgentConfig], Any],
    instance_reader: Callable[[], list[dict]] | None = None,
    process_probe: Callable[[AgentConfig, Any], Signal] | None = None,
    remote_process_probe: Callable[[AgentConfig, str], Signal] | None = None,
    remote_host_resolver: Callable[[str], str | None] | None = None,
) -> dict:
    """Observe the local runtime without creating a registry/birth/session claim.

    The spec describes intent; the latest active instances row describes where
    this incarnation was launched. Probe that owner when it is remote. A local
    process probe is valid only when placement resolves to this host; if remote
    placement cannot be resolved, report UNKNOWN rather than local absence.
    """
    placement = _placements(name, instance_reader)
    records = placement.get("records", [])
    placement_host = (records[0].get("host") if records else "") or ""
    target_host = placement_host or config.hosts_spec.host
    observation_scope = "local"
    observation_host = resolve_hostname()
    try:
        if target_host:
            from ._verdict_remote import _remote_peer_for_host

            peer = (remote_host_resolver or _remote_peer_for_host)(target_host)
        else:
            peer = None
        if peer:
            from ._verdict_remote import remote_process_signal

            signal = (remote_process_probe or remote_process_signal)(config, peer)
            observation_scope = "remote"
            observation_host = peer
        else:
            from ._verdict_resolve import process_signal

            runtime = runtime_factory(config)
            signal = (process_probe or process_signal)(config, runtime)
    except Exception as exc:  # stx-allow: fallback (an unavailable placement/process observation must remain unknown)
        observation_scope = "unknown"
        observation_host = ""
        signal = Signal(
            SOURCE_PROCESS, UNKNOWN,
            f"process observation unavailable ({type(exc).__name__})",
            INSTRUMENT_NO_OBSERVATION,
        )
    result = {
        "name": name,
        "config": path,
        "registered": False,
        "definition_source": "discovered-spec",
        "host": resolve_hostname(),
        "configured_host": config.hosts_spec.host,
        "screen": "",
        "started_at": "",
        "process_observation_scope": observation_scope,
        "process_observation_host": observation_host,
        "stored_credential": "unknown",
        "account": "unknown",
        "a2a": {
            "configured_port": config.a2a.port,
            # A name-only fleet claim is not an endpoint bound to this runtime.
            "resolved_port": None,
            "resolution_source": "none",
        },
        "liveness": decide(name, [signal]).to_dict(),
        "placement_evidence": placement,
        **resolve_runtime_identity(config, running=False, birth_record=None),
    }
    observation = build_agent_observation(result, definition_state=DefinitionState.VALID)
    if observation["process"]["state"] == "exited":
        # This read has no local incarnation. Positive absence does not prove
        # that a local process previously started and exited.
        observation["process"]["state"] = "absent"
    result["observation"] = observation
    result["status"] = {"alive": "running", "absent": "stopped"}.get(
        observation["process"]["state"], "unknown"
    )
    return result
