"""Safe runtime activity projection from SAC's existing state artifacts."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._runners._session_state import read_heartbeat

_UNKNOWN = "No authoritative runtime evidence is published."
_RUNTIME_SIGNALS = ("queue", "inference", "tool", "wait")


def _unknown(reason: str = _UNKNOWN) -> dict[str, Any]:
    return {"state": "unknown", "value": None, "source": "", "reason": reason}


def _observed(value: Any, source: str) -> dict[str, Any]:
    return {"state": "observed", "value": value, "source": source, "reason": ""}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _runtime_signal(control: dict[str, Any], name: str) -> dict[str, Any]:
    value = control.get(name)
    if value in (None, "") or not isinstance(value, (str, int, float, bool)):
        return _unknown()
    return _observed(value, f"runtime_control.{name}")


def activity_projection(
    state_dir: Path,
    *,
    runtime_control: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Project current activity without interpreting logs or private prompts."""
    heartbeat = read_heartbeat(state_dir) or {}
    control = runtime_control if isinstance(runtime_control, dict) else {}
    projected = {name: _runtime_signal(control, name) for name in _RUNTIME_SIGNALS}

    phase = heartbeat.get("current_phase")
    projected["phase"] = (
        _observed(phase, "heartbeat.current_phase")
        if isinstance(phase, str) and phase.strip()
        else _unknown("The agent has not published a current phase.")
    )

    operation = heartbeat.get("state")
    projected["operation"] = (
        _observed(operation, "heartbeat.state")
        if operation in {"starting", "idle", "working", "ready", "busy", "stopping"}
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

    try:
        progress_at = (state_dir / "session.jsonl").stat().st_mtime
    except OSError:
        progress_at = None
    projected["last_progress"] = (
        _observed(
            datetime.fromtimestamp(progress_at, tz=timezone.utc).isoformat(),
            "session.jsonl.mtime",
        )
        if progress_at is not None
        else _unknown("No runtime transcript progress has been observed.")
    )
    return projected


__all__ = ["activity_projection"]
