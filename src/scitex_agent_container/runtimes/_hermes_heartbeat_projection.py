"""Durable owner-side projection of Hermes' session-event cursor."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .._runners._atomic import atomic_write_text
from ._hermes_heartbeat import (
    WRITER_HERMES_SESSION_EVENTS,
    HermesHeartbeatObservation,
    _gateway_identity,
    observe_hermes_heartbeat,
)
from ._hermes_tui_rpc import HermesTuiRpcError

PROJECTION_FILE = "hermes-heartbeat-events.json"
PROJECTION_MAX_AGE_S = 15.0


class HermesHeartbeatProjectionError(HermesTuiRpcError):
    """The event projection is absent, stale, or from another engine."""


def _read_projection_payload(state_dir: Path) -> dict:
    try:
        payload = json.loads(
            (Path(state_dir) / PROJECTION_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError) as exc:
        raise HermesHeartbeatProjectionError(
            f"Hermes heartbeat event projection is unavailable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection is malformed"
        )
    return payload


def _projection_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise HermesHeartbeatProjectionError(
            f"Hermes heartbeat event projection has invalid {key}: {value!r}"
        )
    return value


def read_hermes_heartbeat_projection(
    state_dir: Path,
    agent_name: str,
    *,
    previous: dict | None,
    max_age_s: float = PROJECTION_MAX_AGE_S,
    now_fn: Callable[[], float] = time.time,
) -> HermesHeartbeatObservation:
    """Read the owner-maintained event cursor, rejecting stale incarnations."""
    del previous
    state_dir = Path(state_dir)
    payload = _read_projection_payload(state_dir)
    identity = _gateway_identity(state_dir)
    if payload.get("hermes_gateway_generation") != identity.generation:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection belongs to a previous gateway"
        )
    if payload.get("agent_name") != agent_name:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection belongs to another agent"
        )
    observed_at = payload.get("observed_at")
    activity_at = payload.get("hermes_activity_at")
    if (
        not isinstance(observed_at, (int, float))
        or observed_at < 0
        or not isinstance(activity_at, (int, float))
        or activity_at < 0
        or activity_at > observed_at
    ):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid observation time"
        )
    age = float(now_fn()) - float(observed_at)
    if age < -5.0 or age > float(max_age_s):
        raise HermesHeartbeatProjectionError(
            f"Hermes heartbeat event projection is stale ({age:.3f}s old)"
        )
    state = payload.get("state")
    epoch = payload.get("hermes_event_epoch")
    engine = payload.get("engine_incarnation_id")
    session_id = payload.get("hermes_session_id")
    if (
        state not in {"ready", "busy"}
        or not isinstance(epoch, str)
        or not epoch
        or engine != f"{identity.generation}:{epoch}:{session_id}"
        or not isinstance(session_id, str)
        or not session_id
    ):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid identity or state"
        )
    inflight = payload.get("hermes_tools_inflight")
    if not isinstance(inflight, list) or not all(
        isinstance(value, str) and value for value in inflight
    ):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid tool cursor"
        )
    tools_inflight = _projection_int(payload, "tools_inflight")
    if tools_inflight != len(set(inflight)):
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection tool count does not match its ids"
        )
    history_complete = payload.get("hermes_event_history_complete")
    if type(history_complete) is not bool:
        raise HermesHeartbeatProjectionError(
            "Hermes heartbeat event projection has invalid history completeness"
        )
    return HermesHeartbeatObservation(
        observed_at=float(observed_at),
        activity_at=float(activity_at),
        state=str(state),
        engine_incarnation_id=str(engine),
        gateway_generation=identity.generation,
        event_epoch=epoch,
        event_seq=_projection_int(payload, "hermes_event_seq"),
        event_history_complete=history_complete,
        counter_scope_started_seq=_projection_int(
            payload, "hermes_counter_scope_started_seq"
        ),
        session_id=session_id,
        turns_accepted=_projection_int(payload, "turns_accepted"),
        turns_completed=_projection_int(payload, "turns_completed"),
        tools_started=_projection_int(payload, "tools_started"),
        tools_completed=_projection_int(payload, "tools_completed"),
        tools_inflight=tools_inflight,
        inflight_tool_ids=tuple(sorted(set(inflight))),
        last_event_type=str(payload.get("last_event_type") or ""),
        last_turn_status=str(payload.get("last_turn_status") or ""),
    )


def refresh_hermes_heartbeat_projection(
    state_dir: Path,
    agent_name: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
    now_fn: Callable[[], float] = time.time,
) -> HermesHeartbeatObservation:
    """Advance and atomically persist the owner-side Hermes event cursor."""
    state_dir = Path(state_dir)
    projection_path = state_dir / PROJECTION_FILE
    if projection_path.exists():
        previous = _read_projection_payload(state_dir)
        current_generation = _gateway_identity(state_dir).generation
        if previous.get("hermes_gateway_generation") == current_generation:
            # A corrupt cursor from THIS engine must stop the bridge rather
            # than silently reset counters. A prior engine is intentionally
            # allowed through: observe_hermes_heartbeat fences it by identity.
            read_hermes_heartbeat_projection(
                state_dir,
                agent_name,
                previous=None,
                max_age_s=float("inf"),
                now_fn=now_fn,
            )
    else:
        previous = None
    observed = observe_hermes_heartbeat(
        state_dir,
        agent_name,
        previous=previous,
        timeout_s=timeout_s,
        connect_fn=connect_fn,
        now_fn=now_fn,
    )
    payload = {
        "writer": WRITER_HERMES_SESSION_EVENTS,
        "agent_name": agent_name,
        "observed_at": observed.observed_at,
        "state": observed.state,
        **observed.heartbeat_fields(),
    }
    atomic_write_text(state_dir / PROJECTION_FILE, json.dumps(payload))
    return observed


__all__ = [
    "HermesHeartbeatProjectionError",
    "PROJECTION_FILE",
    "read_hermes_heartbeat_projection",
    "refresh_hermes_heartbeat_projection",
]
