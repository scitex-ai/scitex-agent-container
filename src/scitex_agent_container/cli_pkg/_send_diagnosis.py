"""State-aware diagnosis for ``send_to_agent`` timeout / error returns.

When a turn POST to an agent's A2A sidecar fails (timeout, refused,
non-200), the bare ``{"status": "timeout", "error": "no response in
60s"}`` cannot tell the caller *why*: is the agent still booting, alive
and busy, alive and idle (turn never consumed), dead, or simply
unreachable on its port?

This module gathers the state that distinguishes those cases AT THE
MOMENT OF FAILURE and folds it into a single ``diagnosis`` dict that the
caller attaches to the failure payload. It reuses the same state sources
that power ``sac agents list`` / ``agent_status``:

  * registry / instances row liveness — :func:`list_active_instances`
  * heartbeat state + age            — :func:`latest_heartbeats_per_name`
    (the ``heartbeats`` diary table: ``name, host, pid, state, ts``)
  * local pid liveness               — ``os.kill(pid, 0)`` (same probe
    the state.db GC sweep uses)

No silent fallbacks: when a field can't be gathered the value is an
explicit ``"unreadable: <reason>"`` / ``"unknown"`` string rather than a
quiet default, so the caller can always tell "we don't know" apart from
"we know it's fine".
"""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timezone
from typing import Any

__all__ = ["diagnose_send_failure", "HEARTBEAT_STALE_SECONDS"]

# A heartbeat older than this is treated as "stale" — the agent is
# likely dead or hung rather than merely busy. Matches the ~120s the
# operator guidance calls out (the GC sweep's 300s default is a coarser,
# slower reaper; for live send feedback we want a tighter window).
HEARTBEAT_STALE_SECONDS = 120.0


def _heartbeat_age_seconds(hb_ts: Any, now: float) -> float | None:
    """Seconds between ``hb_ts`` (REAL unix-seconds) and ``now``.

    The ``heartbeats`` table stores ``ts`` as REAL unix-seconds. Returns
    ``None`` only when the value is genuinely unparseable so the caller
    surfaces ``"unreadable"`` rather than a fabricated age.
    """
    if hb_ts is None:
        return None
    try:
        return round(now - float(hb_ts), 1)
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: Any) -> bool | None:
    """True/False if ``pid`` is/ isn't alive locally; None if unknowable.

    Uses ``os.kill(pid, 0)`` — the same liveness probe the state.db GC
    sweep uses. Returns ``None`` when there is no pid to probe (so the
    caller does not mistake "no pid recorded" for "process dead").
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another uid — alive from our POV.
        return True
    except OSError:
        return False


def _has_durable_port_claim(name: str) -> bool:
    """True iff ``name`` holds a live ``port_allocator`` claim.

    The ``a2a_ports`` claim is written at ``agent_start`` and released
    only at ``agent_stop`` / ``--force``, so it survives a health-monitor
    ``runtime.start`` restart that does NOT refresh the ``instances`` row.
    A live claim therefore means "this agent is running" even when its
    instances row went stale — the registry split-brain case. Best-effort:
    any read failure returns ``False`` (the caller then reports the
    instances-row verdict unchanged, never a fabricated "running").
    """
    try:
        from .._state.port_allocator import get_port

        return get_port(name) is not None
    except Exception:  # stx-allow: fallback (reason: an unreadable allocator table must not crash the diagnosis nor fabricate liveness — fall back to the instances-row verdict)
        return False


def _port_reachable(
    port: int, *, host: str = "127.0.0.1", timeout: float = 1.0
) -> bool:
    """True iff a TCP connect to ``host:port`` succeeds within ``timeout``.

    Distinguishes "sidecar listening" from "connection refused / not
    booted". Only meaningful for a local (loopback) port; cross-host
    callers pass ``reachable_checked=False`` instead.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def diagnose_send_failure(
    name: str,
    *,
    a2a_port: int | None,
    peer_host: str,
    current_host: str,
    now: float | None = None,
    brokered: Any = None,
) -> dict[str, Any]:
    """Gather state-of-the-agent at the moment a send failed.

    Returns a ``diagnosis`` dict the caller folds into the ``timeout`` /
    ``error`` payload. Every field is explicit — an un-gatherable value
    becomes ``"unreadable: <reason>"`` / ``"unknown"`` / ``None``, never
    a silent healthy-looking default.

    Inside a container the local ``state.db`` is a private, effectively
    empty per-agent bridge DB, so EVERY field gathered below would be a
    fabrication about a peer ("stopped", ``pid: null``, ``boot_complete:
    false``). On that path the facts are sourced from the HOST fleet
    registry instead, via the same broker door ``agent_status`` uses — see
    :mod:`._send_broker` / :mod:`._send_diagnosis_brokered`. ``brokered``
    lets the caller pass an already-fetched
    :class:`._send_broker.BrokeredPeer` so one send costs one lookup, not
    two. On a bare host none of this engages and the local gathering below
    runs exactly as it always has.

    Fields
    ------
    registry_status: ``"running"`` (an active instances row exists) or
        ``"stopped"`` (none) or ``"unreadable: <reason>"``.
    pid / pid_alive: recorded pid and whether it is alive locally
        (``True`` / ``False`` / ``None`` when unknowable, e.g. cross-host
        or no pid recorded).
    heartbeat_state: the latest heartbeat ``state``
        (idle/working/starting/...) or ``None`` when no heartbeat row, or
        ``"unreadable: <reason>"``.
    heartbeat_age_seconds: age of the latest heartbeat in seconds, or
        ``None``.
    last_activity: the instance's ``last_heartbeat_at`` ISO string (or
        the diary heartbeat ts as ISO) — best available "last seen".
    a2a_port / port_reachable: the resolved port and whether a local TCP
        connect succeeded. ``port_reachable`` is ``None`` for cross-host
        rows (we don't probe a remote port from here).
    boot_complete: ``True`` when a heartbeat is present and fresh; ``False``
        when stale/absent; ``None`` when unknowable.
    likely_causes: plain-language interpretation of the above.

    Parameters
    ----------
    name: agent name.
    a2a_port: resolved port the send targeted (may be ``None``).
    peer_host: ``row["host"]`` — the host the turn was POSTed to.
    current_host: lead-side resolved host.
    now: wall-clock override for deterministic tests.
    brokered: a pre-fetched :class:`._send_broker.BrokeredPeer`. When
        ``None`` and we are in a container, the host is asked here.
    """
    # --- in-container: source the facts from the HOST fleet registry -------
    # The local reads below cannot see a single peer from inside a SIF, and
    # their emptiness renders as death. Ask the host instead.
    from ._send_broker import PeerLookupUnavailable, should_broker_peer_lookup

    if brokered is None and should_broker_peer_lookup():
        from ._send_broker import lookup_peer_via_host

        try:
            brokered = lookup_peer_via_host(name)
        except PeerLookupUnavailable as exc:
            # Could not ask → UNKNOWN. Explicitly NOT "stopped": falling back
            # to the blind local read here would recreate the exact bug.
            from ._send_diagnosis_brokered import unknown_lookup_diagnosis

            return unknown_lookup_diagnosis(
                name,
                current_host=current_host,
                reason=str(exc),
                a2a_port=a2a_port,
            )
    if brokered is not None:
        from ._send_diagnosis_brokered import brokered_diagnosis

        return brokered_diagnosis(
            name,
            a2a_port=a2a_port,
            peer_host=peer_host,
            current_host=current_host,
            peer=brokered,
        )

    # --- bare host: unchanged local gathering ------------------------------
    now = time.time() if now is None else now
    is_local = (not peer_host) or peer_host == current_host

    diagnosis: dict[str, Any] = {
        "agent": name,
        "host": peer_host or current_host,
        "is_local": is_local,
        "a2a_port": a2a_port,
    }

    # ---- registry / instances row + pid ---------------------------------
    # ``registry_status`` reflects the SAME live-endpoint sources
    # ``send_to_agent`` now resolves through (registry split-brain fix):
    # an active ``instances`` row OR a durable ``port_allocator`` claim.
    # Without the allocator arm a locally-running agent whose instances
    # row went stale (health-monitor restart via ``runtime.start``,
    # stale-lease clear) would still be reported "stopped" here even
    # though ``send_to_agent`` correctly reaches it — the exact
    # split-brain symptom this diagnosis is meant to explain, not repeat.
    row: dict[str, Any] | None = None
    try:
        from .._state.state_db import list_active_instances

        rows = list_active_instances()
        matching = [r for r in rows if r.get("name") == name]
        row = matching[0] if matching else None
        if row is not None:
            diagnosis["registry_status"] = "running"
        elif _has_durable_port_claim(name):
            # No active row, but a live allocator claim — the agent is
            # running (a stopped agent releases its claim). Mark it
            # running and note the source so the operator isn't misled.
            diagnosis["registry_status"] = "running"
            diagnosis["registry_source"] = "port_allocator"
        else:
            diagnosis["registry_status"] = "stopped"
    except Exception as exc:  # noqa: BLE001 — report loudly, never fake "stopped"
        diagnosis["registry_status"] = f"unreadable: {type(exc).__name__}: {exc}"

    pid = row.get("pid") if row else None
    diagnosis["pid"] = pid
    if is_local:
        diagnosis["pid_alive"] = _pid_alive(pid)
    else:
        # Cross-host: no local liveness signal. State it, don't guess.
        diagnosis["pid_alive"] = None

    # last_activity: prefer the instances row's last_heartbeat_at.
    diagnosis["last_activity"] = (row or {}).get("last_heartbeat_at")

    # ---- heartbeat state + age (diary `heartbeats` table) ----------------
    hb_state: Any = None
    hb_age: float | None = None
    try:
        from .._state.state_db import latest_heartbeats_per_name

        beats = {b.get("name"): b for b in latest_heartbeats_per_name()}
        beat = beats.get(name)
        if beat is None:
            hb_state = None
            diagnosis["heartbeat_state"] = None
            diagnosis["heartbeat_age_seconds"] = None
        else:
            hb_state = beat.get("state")
            hb_age = _heartbeat_age_seconds(beat.get("ts"), now)
            diagnosis["heartbeat_state"] = hb_state
            if hb_age is None:
                diagnosis["heartbeat_age_seconds"] = (
                    f"unreadable: bad ts {beat.get('ts')!r}"
                )
            else:
                diagnosis["heartbeat_age_seconds"] = hb_age
            # Fall back to the diary ts for last_activity when the
            # instances row had none.
            if not diagnosis.get("last_activity") and beat.get("ts") is not None:
                try:
                    diagnosis["last_activity"] = (
                        datetime.fromtimestamp(float(beat["ts"]), tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                except (TypeError, ValueError):
                    pass
    except Exception as exc:  # noqa: BLE001 — report loudly, never fake idle
        diagnosis["heartbeat_state"] = f"unreadable: {type(exc).__name__}: {exc}"
        diagnosis["heartbeat_age_seconds"] = None

    hb_fresh = hb_age is not None and hb_age <= HEARTBEAT_STALE_SECONDS

    # ---- boot completion -------------------------------------------------
    # Boot looks complete when a heartbeat exists and is fresh.
    hb_state_str = diagnosis.get("heartbeat_state")
    if isinstance(hb_state_str, str) and hb_state_str.startswith("unreadable"):
        diagnosis["boot_complete"] = None
    elif hb_state is None:
        diagnosis["boot_complete"] = False
    else:
        diagnosis["boot_complete"] = bool(hb_fresh)

    # ---- port reachability ----------------------------------------------
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        diagnosis["port_reachable"] = None
    elif not is_local:
        # Don't probe a remote port from the lead; we can't interpret it.
        diagnosis["port_reachable"] = None
    else:
        diagnosis["port_reachable"] = _port_reachable(a2a_port)

    diagnosis["likely_causes"] = _interpret(diagnosis, hb_state, hb_age, hb_fresh)
    return diagnosis


def _interpret(
    d: dict[str, Any], hb_state: Any, hb_age: float | None, hb_fresh: bool
) -> str:
    """Turn the gathered fields into plain-language guidance."""
    port_reachable = d.get("port_reachable")
    pid_alive = d.get("pid_alive")
    registry = d.get("registry_status")

    # Hard "not running" cases first.
    if registry == "stopped":
        return (
            "agent is not in the active registry (no live instance row); "
            "it is stopped — start it before sending"
        )
    if pid_alive is False:
        return (
            "recorded agent pid is not alive; the process crashed or was "
            "killed — restart it"
        )
    if d.get("a2a_port") in (None, 0) or (
        isinstance(d.get("a2a_port"), int) and d["a2a_port"] <= 0
    ):
        return "no a2a_port recorded for the agent; it has not bound a sidecar"
    if port_reachable is False:
        return (
            f"agent sidecar is not listening on port {d.get('a2a_port')}; "
            "it is not booted or the sidecar crashed"
        )

    # Heartbeat-based interpretation.
    if isinstance(hb_state, str) and hb_state in ("working", "busy"):
        return (
            "agent is alive and actively working; the turn is likely still "
            "in progress — raise timeout_seconds or poll status"
        )
    if hb_age is not None and not hb_fresh:
        return (
            f"heartbeat is stale ({hb_age}s old, > {int(HEARTBEAT_STALE_SECONDS)}s); "
            "the agent appears dead or hung — restart it"
        )
    if hb_state is None:
        return (
            "no heartbeat recorded yet; the agent may still be finishing boot "
            "(sidecar reachable but session not ready) — retry shortly"
        )
    if hb_fresh and hb_state in ("idle", "ready", "starting"):
        return (
            "agent is alive but did not consume the turn (heartbeat fresh, "
            f"state={hb_state!r}); it may still be finishing boot, or the turn "
            "was not enqueued — retry shortly"
        )
    # Fresh heartbeat, some other state — alive but unexpected.
    return (
        f"agent heartbeat is fresh (state={hb_state!r}) but it did not reply; "
        "retry shortly or poll status"
    )
