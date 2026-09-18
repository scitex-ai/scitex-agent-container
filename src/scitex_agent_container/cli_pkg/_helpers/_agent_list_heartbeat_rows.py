"""Fleet-list rows sourced from privacy-safe authoritative heartbeat leases."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from ..._state.authoritative_heartbeat import classify_resident_state
from ..._state.state_store import latest_heartbeats_per_name
from ._agent_list_row import build_agent_row


def heartbeat_lease_rows(
    *,
    covered: set[str],
    display_host: str,
    running_only: bool,
    host_display_for: Callable[[str, str], str],
    beats: Iterable[dict] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Build rows for leased residents absent from registry/instance sources."""
    rows: list[dict[str, Any]] = []
    observed_at = time.time() if now is None else float(now)
    for beat in beats if beats is not None else latest_heartbeats_per_name():
        name = str(beat.get("agent_id") or "")
        if not name or name in covered:
            continue
        resident_state = classify_resident_state(
            beat,
            now=observed_at,
            process_alive=(
                beat.get("_process_alive")
                if beat.get("_process_alive") in {True, False, None}
                else None
            ),
            federation_connected=bool(beat.get("_federation_connected")),
            progress_stale_s=120.0,
        )
        live = resident_state in {"idle", "active", "blocked", "stalled"}
        if running_only and not live:
            continue
        host = str(beat.get("host") or "")
        rows.append(
            build_agent_row(
                name=name,
                status_val="running" if live else "unknown",
                screen_name="",
                multiplexer=str(beat.get("runtime") or ""),
                started="",
                host_label=host,
                host_display=host_display_for(host, display_host),
                spec_path="",
                a2a_port=None,
                account_label="",
                deferred=False,
                errors=[],
                liveness_unknown=not live,
                runtime=str(beat.get("runtime") or "unknown"),
                harness=str(beat.get("harness") or "unknown"),
                engine=str(beat.get("engine") or "unknown"),
                model=str(beat.get("model") or "unknown"),
                billing_mode="unspecified",
                auth_identity="unknown",
                runtime_identity_source="authoritative-heartbeat",
                probe_runtime="authoritative-heartbeat",
                probe_error=None,
                labels={"resident_state": resident_state},
            )
        )
        covered.add(name)
    return rows


def overlay_authoritative_heartbeats(
    rows: list[dict[str, Any]], *, beats: Iterable[dict], now: float | None = None
) -> None:
    """Annotate existing registry/instance rows without treating observers as progress."""
    observed_at = time.time() if now is None else float(now)
    by_name = {
        str(beat.get("agent_id") or ""): beat
        for beat in beats
        if beat.get("agent_id")
    }
    for row in rows:
        beat = by_name.get(str(row.get("name") or ""))
        if beat is None:
            if str(row.get("harness") or "").lower() == "hermes":
                row["resident_state"] = "unknown"
                row["heartbeat_authoritative"] = False
            continue
        row["resident_state"] = classify_resident_state(
            beat,
            now=observed_at,
            process_alive=(
                beat.get("_process_alive")
                if beat.get("_process_alive") in {True, False, None}
                else None
            ),
            federation_connected=bool(beat.get("_federation_connected")),
            progress_stale_s=120.0,
        )
        row["heartbeat_authoritative"] = True
        row["heartbeat_boot_id"] = beat.get("boot_id")
        row["heartbeat_session_id"] = beat.get("session_id")
        row["heartbeat_progress_seq"] = beat.get("progress_seq")


__all__ = ["heartbeat_lease_rows", "overlay_authoritative_heartbeats"]
