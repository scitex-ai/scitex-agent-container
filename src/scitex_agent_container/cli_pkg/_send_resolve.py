"""Live A2A endpoint resolution for ``send_to_agent`` (registry split-brain fix).

Background — the "registry split-brain" (2026-06-19, figrecipe→beta).
``agent_send`` (this library) used to resolve a target's A2A endpoint
SOLELY from the ``instances`` table: it took the active row's
``a2a_port`` and refused (``status="error"``, ``a2a_port=null``) when no
active row existed or the row's port was null. ``a2a_send`` (the bus
transport) resolves through ``sac listen``'s live broker / peer
registry instead, so it kept reaching a live agent that ``agent_send``
declared "stopped".

The disagreement is real because the ``instances`` row goes stale while
the agent keeps running:

  * The ONLY writer of a fresh local row is
    :func:`_lifecycle._instances.record_local_instance`, reached ONLY via
    :func:`_lifecycle._start.agent_start`.
  * The health-monitor's restart callback (``_start.py``) calls
    ``runtime.start(config)`` DIRECTLY — it never re-runs ``agent_start``,
    so a crashed/hung TUI agent that the supervisor restarts comes back
    with its turn-bridge bound on the same port but NO refreshed
    ``instances`` row.
  * ``clear_stale_instance_lease`` / a prior stop can end the row.

The DURABLE, restart-surviving source of truth for an agent's bound
port is the ``a2a_ports`` allocator table (:mod:`_state.port_allocator`):
written at ``agent_start`` (``resolve_a2a_port`` → ``claim_port``),
idempotent, released only at ``agent_stop`` / ``--force``. It is the SAME
port the turn-bridge binds (``_tui_turn_bridge.resolved_a2a_port`` reads
``config.a2a.port`` which ``resolve_a2a_port`` set from the claim) and the
SAME source the listen forwarder (``_listen._forward.forward_to_live_runner``),
the registry-endpoint enricher (``_listen._registry_endpoints``), and the
peer client (``_network._peer_resolve.resolve_peer_url``) already prefer.

This module makes ``agent_send`` resolve the endpoint the SAME WAY those
paths do, so the two transports agree on the live endpoint:

  1. Active ``instances`` row (``bound_port`` preferred, legacy
     ``a2a_port`` fallback) + its ``host`` — the cross-host dispatcher
     records remote=True rows here, so this stays authoritative for
     cross-host agents.
  2. Durable local allocator claim (:func:`_state.port_allocator.get_port`)
     when the row is missing or carries a null port — the case the
     health-monitor restart leaves behind for a LOCAL agent.

Resolution is pure-ish (state.db reads only) and fail-soft on read
errors so the caller still produces its own clear error payload when no
endpoint resolves anywhere. The caller (``_send.send_to_agent``) keeps
ALL of its loud liveness gates (pid-dead, sidecar-port-unreachable,
creds-expired) — this module only fixes WHERE the port comes from, never
relaxes the "is it actually reachable?" checks.
"""

from __future__ import annotations

from typing import NamedTuple


class ResolvedEndpoint(NamedTuple):
    """Where a named agent's live ``/v1/turn`` listens, plus provenance.

    ``a2a_port`` is ``None`` only when NO source (active row port nor
    durable allocator claim) knows a port — the caller surfaces the same
    loud "no a2a_port recorded" / "not running" error it always did.

    ``host`` is the agent's host (the active row's ``host`` when a row
    exists, else the local canonical host for an allocator-only claim).

    ``source`` is provenance for diagnostics / tests:
      * ``"instance_row"`` — the active ``instances`` row carried a port.
      * ``"port_allocator"`` — the row was missing or null-port; the
        durable allocator claim supplied the port (the split-brain fix
        path).
      * ``"none"`` — nothing resolved a port.

    ``row`` is the active ``instances`` row dict when one exists (so the
    caller can keep reusing its ``pid`` / ``host`` for diagnosis without
    a second query), else ``None``.
    """

    a2a_port: int | None
    host: str
    source: str
    row: dict | None


def _active_row_for(name: str) -> dict | None:
    """Return the newest active ``instances`` row for ``name``, else None.

    Best-effort: a state.db read failure degrades to ``None`` so the
    caller falls through to the allocator claim rather than crashing the
    send. ``list_active_instances`` orders ``started_at DESC`` so the
    first match is the newest.
    """
    try:
        from .._state.state_db import list_active_instances
    except Exception:  # pragma: no cover - import guarded only for safety
        return None
    try:
        rows = [r for r in list_active_instances() if r.get("name") == name]
    except Exception:  # stx-allow: fallback (reason: an unreadable instances table must not crash send; the allocator claim below is the durable resolver and the caller still fails loud if neither knows a port)
        return None
    return rows[0] if rows else None


def _row_port(row: dict | None) -> int | None:
    """Extract a positive int port from a row (``bound_port`` preferred).

    Mirrors :func:`_network._peer_resolve._lookup_instance_endpoint` and
    :func:`_listen._registry_endpoints._instance_endpoint`: prefer the
    family-tree ``bound_port`` column, fall back to legacy ``a2a_port``.
    Returns ``None`` for a missing / non-positive value.
    """
    if not row:
        return None
    port = row.get("bound_port")
    if port is None:
        port = row.get("a2a_port")
    if isinstance(port, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(port, int) and port > 0:
        return port
    return None


def _allocator_port(name: str) -> int | None:
    """Return the durable allocator claim for ``name``, else None.

    The ``a2a_ports`` table survives a health-monitor ``runtime.start``
    restart (it is only released at ``agent_stop`` / ``--force``), so it
    is the source that stays correct when the ``instances`` row went
    stale. Best-effort: any read failure degrades to ``None``.
    """
    try:
        from .._state.port_allocator import get_port

        port = get_port(name)
    except Exception:  # stx-allow: fallback (reason: best-effort durable-port lookup — caller fails loud when no source knows a port)
        return None
    if isinstance(port, bool):
        return None
    return int(port) if isinstance(port, int) and port > 0 else None


def resolve_send_endpoint(name: str, *, current_host: str) -> ResolvedEndpoint:
    """Resolve ``name``'s live ``/v1/turn`` endpoint (split-brain fix).

    Resolution order (matches the listen forwarder + peer resolver, so
    ``agent_send`` and ``a2a_send`` agree on the live endpoint):

      1. Active ``instances`` row port (``bound_port`` / ``a2a_port``)
         when present — authoritative, and the only place a cross-host
         (remote=True) agent's host + port live.
      2. Durable ``port_allocator`` claim when the row is missing or its
         port is null — the case a health-monitor restart leaves for a
         LOCAL agent. Host is the local canonical host.

    ``host`` resolution:
      * When an active row exists, use its ``host`` (so a cross-host
        agent still routes via ssh to its peer).
      * When only an allocator claim exists, the agent ran on THIS host
        (the local allocator never holds claims for remote agents), so
        ``host`` is ``current_host``.

    Never raises — read failures degrade to ``a2a_port=None`` and the
    caller surfaces its own loud "not running" / "no a2a_port" error.
    """
    row = _active_row_for(name)
    row_port = _row_port(row)
    if row_port is not None:
        host = str(row.get("host") or "") if row else ""
        return ResolvedEndpoint(
            a2a_port=row_port,
            host=host or current_host,
            source="instance_row",
            row=row,
        )

    # Row missing OR null-port → consult the durable allocator claim. This
    # is the split-brain fix: a locally-running agent whose instances row
    # went stale (health-monitor restart, stale-lease clear) still has its
    # claim here, exactly as the listen forwarder / peer resolver rely on.
    alloc_port = _allocator_port(name)
    if alloc_port is not None:
        # An allocator claim is local by construction (remote agents'
        # ports live only in their own host's claim table, surfaced to us
        # via the remote=True instances row handled above). If a stale row
        # exists with a different (non-local) host we still prefer the
        # local claim's host: the claim proves a live LOCAL sidecar, which
        # is what we are about to dispatch to.
        return ResolvedEndpoint(
            a2a_port=alloc_port,
            host=current_host,
            source="port_allocator",
            row=row,
        )

    return ResolvedEndpoint(a2a_port=None, host=current_host, source="none", row=row)


__all__ = ["ResolvedEndpoint", "resolve_send_endpoint"]
