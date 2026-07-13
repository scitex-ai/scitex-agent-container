"""Diagnosis built from the HOST fleet registry, for the in-container send path.

Companion to :mod:`._send_diagnosis`. That module gathers its facts from the
LOCAL ``state.db``; inside an apptainer SIF that database is a private,
effectively empty per-agent bridge DB, so every field it produces about a
PEER is a fabrication:

    registry_status: "stopped"   <- no row (because we cannot see any row)
    pid:             null
    boot_complete:   false       <- no heartbeat (because we cannot see one)

This module produces the same ``diagnosis`` dict from the answer the HOST's
``sac listen`` gave us (:mod:`._send_broker`), and — where the host does not
expose a fact — says ``unknown`` instead of inventing a healthy-looking or
dead-looking default.

The one rule that governs every choice here
-------------------------------------------
**Absence of evidence is not death.** A field we could not measure is
``None`` / ``"unknown: …"``, never ``False`` and never ``"stopped"``. The
remedy an operator (or an agent) reaches for on a "dead" verdict is
DESTRUCTIVE — ``--force --fresh`` — so a false red kills a healthy, working
agent while a false green merely costs a round-trip. The asymmetry is
deliberate.

Two facts, both measured on the live fleet 2026-07-14, are why this module
refuses to promote hints into verdicts:

* **The pid is deliberately NOT fetched.** ``GET /agents/<name>/status`` does
  not carry one, and sourcing it from the registry list would import a
  *stale* pid: a health-monitor restart gives the agent a new pid without
  refreshing the recorded row, so ``os.kill(stale_pid, 0)`` returns False for
  an agent that is running perfectly. That would trip the caller's
  ``pid_alive is False`` gate and declare a live agent dead. ``pid_alive``
  therefore stays ``None`` (UNKNOWN) on this path — a refusal, not an
  oversight.

* **An unbound /v1/turn port is NOT death.** Of the 47 agents the host knew,
  only 5 had their ``/v1/turn`` port bound; the other 41 — including agents
  that answered a2a messages that same minute — held a port claim with
  nothing listening on it. Most of this fleet is reached over the a2a
  subscriber channel, which does not require that port. So an unreachable
  port means "this transport cannot carry the turn", never "the agent is
  gone".
"""

from __future__ import annotations

from typing import Any

__all__ = ["brokered_diagnosis", "unknown_lookup_diagnosis"]

# The host status route exposes no heartbeat; be explicit about the gap
# rather than emitting a ``None`` the caller could read as "idle".
_NO_HEARTBEAT = "unknown: not exposed by host listen GET /agents/<name>/status"

_HOST_STATUS_NOTE = (
    "HINT ONLY — never a liveness verdict. The host's STARTUP_FAILED marker "
    "is written once and never reconciled, so it goes stale: measured "
    "2026-07-14, scitex-writer reported 'startup_failed' from a ~2-day-old "
    "marker while its host row held a live pid and it answered an a2a message "
    "that same turn. Do not conclude death from this field."
)


def unknown_lookup_diagnosis(
    name: str,
    *,
    current_host: str,
    reason: str,
    a2a_port: int | None = None,
) -> dict[str, Any]:
    """The diagnosis for "we could not ask the host" — UNKNOWN, not dead.

    Every liveness field is explicitly unknown. Note what is NOT here:
    ``registry_status`` is *not* ``"stopped"``, ``pid_alive`` is *not*
    ``False``, ``boot_complete`` is *not* ``False``. Rendering "I could not
    check" as "the agent is stopped" is the precise bug this whole change
    exists to kill; reproducing it in the failure path would be absurd.
    """
    return {
        "agent": name,
        "host": current_host,
        "is_local": None,
        "a2a_port": a2a_port,
        "lookup_source": "host_broker",
        "lookup_error": reason,
        "registry_status": f"unknown: {reason}",
        "pid": None,
        "pid_alive": None,
        "last_activity": None,
        "heartbeat_state": None,
        "heartbeat_age_seconds": None,
        "boot_complete": None,
        "port_reachable": None,
        "likely_causes": (
            "the host listen broker could not be asked, so this agent's state "
            "is UNKNOWN — it may well be alive and working. Absence of "
            "evidence is NOT death: do not stop, force-restart or reap the "
            "agent on the strength of this result. Restore the broker on the "
            "host (`sac listen restart`) and retry the lookup."
        ),
    }


def brokered_diagnosis(
    name: str,
    *,
    a2a_port: int | None,
    peer_host: str,
    current_host: str,
    peer: Any,
    port_probe: Any = None,
) -> dict[str, Any]:
    """Build the ``diagnosis`` dict from the HOST's answer about ``name``.

    Args:
        peer: The :class:`._send_broker.BrokeredPeer` the host returned.
        port_probe: The TCP-reachability probe to use for the agent's
            ``/v1/turn`` port — injected so tests drive this without opening
            a socket. Defaults to :func:`._send_diagnosis._port_reachable`.
            Its result is recorded as a HINT about the *transport*, and is
            never allowed to become a verdict about the *agent*.
    """
    if port_probe is None:
        from ._send_diagnosis import _port_reachable as port_probe

    is_local = (not peer_host) or peer_host == current_host
    diagnosis: dict[str, Any] = {
        "agent": name,
        "host": peer_host or current_host,
        "is_local": is_local,
        "a2a_port": a2a_port,
        # Provenance is part of the answer: a reader must never have to guess
        # whether a verdict came from the blind container-local DB or from the
        # real fleet registry on the host.
        "lookup_source": "host_broker",
        "registry_source": "host listen GET /agents/<name>/status",
    }

    if not peer.known:
        diagnosis["registry_status"] = "not_found"
    elif peer.a2a_port is not None:
        # The host holds a durable a2a-port claim. Claims are released only at
        # ``agent stop`` / ``--force``, so this is real evidence of life.
        diagnosis["registry_status"] = "running"
    else:
        diagnosis["registry_status"] = "stopped"

    # pid: withheld on purpose — see the module docstring. UNKNOWN, not False.
    diagnosis["pid"] = None
    diagnosis["pid_alive"] = None
    diagnosis["last_activity"] = None
    diagnosis["heartbeat_state"] = _NO_HEARTBEAT
    diagnosis["heartbeat_age_seconds"] = None
    # boot_complete: the host route carries no heartbeat, so we cannot know.
    # ``False`` here would be a lie that reads as "never booted".
    diagnosis["boot_complete"] = None

    session_id = peer.body.get("session_id") if isinstance(peer.body, dict) else None
    if session_id is not None:
        diagnosis["session_id"] = session_id
    if peer.host_status:
        diagnosis["host_status"] = peer.host_status
        diagnosis["host_status_note"] = _HOST_STATUS_NOTE

    if not isinstance(a2a_port, int) or a2a_port <= 0 or not is_local:
        diagnosis["port_reachable"] = None
    else:
        diagnosis["port_reachable"] = port_probe(a2a_port)

    diagnosis["likely_causes"] = _interpret_brokered(diagnosis)
    return diagnosis


def _interpret_brokered(diagnosis: dict[str, Any]) -> str:
    """Plain-language reading of the host's answer. Never concludes death
    from a hint."""
    registry = diagnosis.get("registry_status")
    port = diagnosis.get("a2a_port")
    reachable = diagnosis.get("port_reachable")
    name = diagnosis.get("agent")

    if registry == "not_found":
        return (
            "the host fleet registry has no agent by this name — check the "
            "spelling, or the agent was never registered on this host"
        )
    if registry == "stopped":
        return (
            "the host fleet registry holds no a2a-port claim for this agent, "
            "and a claim is released only at `sac agents stop` / --force — so "
            "it is very likely stopped. This is the HOST's answer about the "
            "real fleet, not a container-local guess"
        )

    # registry == "running": the host holds a live port claim.
    if reachable is False:
        return (
            f"the agent IS live in the host fleet registry (it holds a2a port "
            f"{port}), but nothing is listening on that port, so the /v1/turn "
            f"transport cannot carry this turn. This is NOT a death verdict "
            f"and must not be acted on as one: most agents in this fleet never "
            f"bind /v1/turn (measured 2026-07-14: only 5 of 47 had it bound) "
            f"and are reached over the a2a subscriber channel instead. Deliver "
            f"with `sac a2a send {name} ...` (or the a2a_send tool), which does "
            f"not require this port. Do NOT force-restart the agent on this "
            f"signal — that would kill a healthy, working agent"
        )
    if reachable is True:
        return (
            "agent is live in the host fleet registry and its /v1/turn sidecar "
            "is accepting connections; if the turn still fails, the agent is "
            "alive and the failure is downstream of delivery"
        )
    return (
        "agent is live in the host fleet registry; its /v1/turn port was not "
        "probed from here, so reachability of that transport is UNKNOWN"
    )
