"""Safe runtime activity projection from SAC's existing state artifacts."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._runners._session_state import read_heartbeat

_UNKNOWN = "No authoritative runtime evidence is published."
_RUNTIME_SIGNALS = ("queue", "inference", "tool", "wait")
_FENCED_WRITERS = {"hermes-session-events", "codex-rollout-events"}
# HEARTBEAT SPEC 2026-10-08: the ONLY work evidence is the two
# mechanically-measured byte deltas. Self-reported step counters
# (turns/tools_*) are forbidden and no longer published here.
_WORK_DELTAS = (
    "session_jsonl_delta_bytes",
    "subagent_jsonl_delta_bytes",
)
_EVENT_TYPES = {
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "task_started",
    "task_complete",
    "turn_aborted",
    "message.start",
    "message.complete",
    "tool.start",
    "tool.complete",
}


def _unknown(reason: str = _UNKNOWN) -> dict[str, Any]:
    return {"state": "unknown", "value": None, "source": "", "reason": reason}


def _observed(value: Any, source: str) -> dict[str, Any]:
    return {"state": "observed", "value": value, "source": source, "reason": ""}


def _number(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _runtime_signal(control: dict[str, Any], name: str) -> dict[str, Any]:
    value = control.get(name)
    if value in (None, "") or not isinstance(value, (str, int, float, bool)):
        return _unknown()
    return _observed(value, f"runtime_control.{name}")


def _fenced_heartbeat(heartbeat: dict, now: float) -> dict | None:
    writer = heartbeat.get("writer")
    if not isinstance(writer, str) or writer not in _FENCED_WRITERS:
        return None
    resident = heartbeat.get("authoritative_heartbeat")
    if not isinstance(resident, dict):
        return None
    from .._state.authoritative_heartbeat import (
        AuthoritativeHeartbeatError,
        validate_heartbeat,
    )

    try:
        validated = validate_heartbeat(
            resident,
            expected_agent=str(resident.get("agent_id") or ""),
            expected_host=str(resident.get("host") or ""),
            now=now,
        )
    except AuthoritativeHeartbeatError:
        return None
    if now > validated["lease_expires_at"]:
        return None
    if (
        any(
            heartbeat.get(key) != validated[key]
            for key in (
                "agent_id",
                "spec_id",
                "host",
                "runtime",
                "harness",
                "engine",
                "model",
                "session_id",
                "boot_id",
            )
        )
        or heartbeat.get("ts") != validated["observed_at"]
    ):
        return None
    return validated


def _typed_activity(heartbeat: dict, resident: dict | None) -> dict[str, Any]:
    keys = (
        *_WORK_DELTAS,
        "last_event_type",
        "last_turn_status",
        "last_error_code",
        "capacity",
    )
    result = {key: _unknown() for key in keys}
    if resident is None:
        return result
    values = [_number(heartbeat.get(key)) for key in _WORK_DELTAS]
    if all(value is not None and value >= 0 for value in values):
        result.update(
            {
                key: _observed(value, f"heartbeat.{key}")
                for key, value in zip(_WORK_DELTAS, values)
            }
        )
    status = heartbeat.get("last_turn_status")
    if isinstance(status, str) and status in {"complete", "error", "interrupted"}:
        result["last_turn_status"] = _observed(status, "heartbeat.last_turn_status")
    event_type = heartbeat.get("last_event_type")
    if isinstance(event_type, str) and event_type in _EVENT_TYPES:
        result["last_event_type"] = _observed(event_type, "heartbeat.last_event_type")
    if heartbeat.get("last_error_code") == "turn_error":
        result["last_error_code"] = _observed("turn_error", "heartbeat.last_error_code")
    # Generic CAPPED sidecars/text are not provider-capacity instruments.
    result["capacity"] = _unknown(
        "No authoritative provider capacity measurement is published."
    )
    return result


def activity_projection(
    state_dir: Path,
    *,
    runtime_control: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Project current activity without interpreting logs or private prompts."""
    heartbeat = read_heartbeat(state_dir) or {}
    if not isinstance(heartbeat, dict):
        heartbeat = {}
    current_time = now if now is not None else time.time()
    resident = _fenced_heartbeat(heartbeat, current_time)
    control = runtime_control if isinstance(runtime_control, dict) else {}
    projected = {name: _runtime_signal(control, name) for name in _RUNTIME_SIGNALS}
    projected.update(_typed_activity(heartbeat, resident))
    projected["session_id"] = (
        _observed(
            resident["session_id"], "heartbeat.authoritative_heartbeat.session_id"
        )
        if resident
        else _unknown("No fenced native session identity is published.")
    )

    phase = heartbeat.get("current_phase")
    projected["phase"] = (
        _observed(phase, "heartbeat.current_phase")
        if isinstance(phase, str) and phase.strip()
        else _unknown("The agent has not published a current phase.")
    )

    operation = heartbeat.get("state")
    projected["operation"] = (
        _observed(operation, "heartbeat.state")
        if isinstance(operation, str)
        and operation in {"starting", "idle", "working", "ready", "busy", "stopping"}
        else _unknown("No current runner operation was observed.")
    )

    turn_started_at = _number(heartbeat.get("turn_started_at"))
    if turn_started_at is None:
        turn_started_at = _number(control.get("turn_started_at"))
        turn_source = "runtime_control.turn_started_at"
    else:
        turn_source = "heartbeat.turn_started_at"
    if turn_started_at is None:
        projected["turn_elapsed"] = _unknown(
            "No authoritative turn-start timestamp is published."
        )
    else:
        projected["turn_elapsed"] = _observed(
            round(
                max(0.0, (now if now is not None else time.time()) - turn_started_at), 1
            ),
            turn_source,
        )

    progress_at = _number(resident.get("progress_at")) if resident else None
    if progress_at is not None and progress_at <= 0:
        progress_at = None
    projected["last_progress"] = (
        _observed(
            datetime.fromtimestamp(progress_at, tz=timezone.utc).isoformat(),
            "heartbeat.authoritative_heartbeat.progress_at",
        )
        if progress_at is not None
        else _unknown("No fenced native event progress has been observed.")
    )
    writer = heartbeat.get("writer")
    if isinstance(writer, str) and writer in _FENCED_WRITERS and resident is None:
        projected["phase"] = _unknown(
            "The native heartbeat lease or identity is not current."
        )
        projected["operation"] = _unknown(
            "The native heartbeat lease or identity is not current."
        )
    return projected


def project_session_id(
    legacy_session_id: str | None, activity: dict, *, harness: str
) -> tuple[str | None, str]:
    """Choose the fenced runtime session; stale markers cannot name Codex threads."""
    observed = activity.get("session_id")
    if isinstance(observed, dict) and observed.get("state") == "observed":
        return observed.get("value"), str(observed.get("source") or "")
    if harness == "codex":
        return None, "unknown"
    return legacy_session_id, "session_id_marker"


__all__ = ["activity_projection", "project_session_id"]
