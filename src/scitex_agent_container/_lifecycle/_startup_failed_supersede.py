"""Read-side supersession: has liveness since refuted a STARTUP_FAILED?

Reads ``heartbeat.json`` MTIME — the field ``_liveness_tick_resolve``
labels PROCESS ALIVE, never the ``ts`` payload field, which is frequently
tens of thousands of seconds behind mtime for an idle agent and would
make this blind for exactly the population it exists to protect.

Never deletes or renames anything — this is a pure read used by
``sac listen``'s STATUS / DELETE handlers to relabel a marker, not
retract it. Retraction (the on-disk rename) only ever happens at the
one write-side call site: the ALIVE no-op in ``_start.agent_start``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._verdict import ALIVE
from ._verdict_state import HEARTBEAT_STALE_S, heartbeat_signal

__all__ = ["liveness_since_failure"]

_HEARTBEAT_FILENAME = "heartbeat.json"


def liveness_since_failure(
    state_dir: Path,
    marker: dict[str, Any],
    *,
    name: str,
    runtime_kind: str = "",
) -> str | None:
    """Non-empty detail string iff a fresh beat postdates the failure."""
    hb = state_dir / _HEARTBEAT_FILENAME
    try:
        mtime = hb.stat().st_mtime
    except OSError:
        return None
    failed_at = marker.get("failed_at")
    if not isinstance(failed_at, str):
        return None
    try:
        failed_ts = (
            datetime.strptime(failed_at, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None
    # One staleness window, so a beat from the same boot attempt cannot
    # refute the failure that attempt produced.
    if mtime - failed_ts < HEARTBEAT_STALE_S:
        return None
    signal = heartbeat_signal(name, path=hb, runtime_kind=runtime_kind)
    if signal.verdict != ALIVE:
        return None
    return signal.detail
