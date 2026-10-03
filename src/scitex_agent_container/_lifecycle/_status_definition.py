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
) -> dict:
    """Observe the local runtime without creating a registry/birth/session claim.

    The spec describes intent. The local runtime probe describes local process
    presence. Same-name instances remain separate, unbound placement evidence;
    local absence neither declares those owners dead nor imports their identity.
    """
    try:
        from ._verdict_resolve import process_signal

        runtime = runtime_factory(config)
        signal = (process_probe or process_signal)(config, runtime)
    except Exception as exc:  # stx-allow: fallback (an unavailable local runtime observation must remain unknown)
        signal = Signal(
            SOURCE_PROCESS, UNKNOWN,
            f"local runtime observation unavailable ({type(exc).__name__})",
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
        "process_observation_scope": "local",
        "stored_credential": "unknown",
        "account": "unknown",
        "a2a": {
            "configured_port": config.a2a.port,
            # A name-only fleet claim is not an endpoint bound to this runtime.
            "resolved_port": None,
            "resolution_source": "none",
        },
        "liveness": decide(name, [signal]).to_dict(),
        "placement_evidence": _placements(name, instance_reader),
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
