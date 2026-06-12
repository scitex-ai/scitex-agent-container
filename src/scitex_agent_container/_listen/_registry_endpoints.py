"""Per-row a2a_port + turn_url enrichment for the registry list/status routes.

The lead dispatch a2a dc6fd23387f64e329049d218cf85a4d4 (Q1) asked for
``GET /agents`` and ``GET /agents/<name>/status`` to surface two extra
fields on every row:

* ``a2a_port`` — the port the agent's local sidecar is bound to.
* ``turn_url`` — the derived ``http://<host>:<port>/v1/turn`` URL the
  caller can POST to (used by scitex-todo's notify resolver P3a-b).

Sourcing chain — the single source of truth for "where does an agent
listen" is, in order:

1. ``_state.port_allocator.get_port(name)`` — the ``a2a_ports`` table
   in ``state.db`` (always populated on local agent_start).
2. ``_lookup_instance_endpoint(name)`` from ``_network._peer_resolve``
   — the ``instances`` table (cross-host rows written by the dispatcher
   with ``remote=True``; the local port allocator never sees them).
3. For ``a2a_port`` only: ``None`` (caller surfaces the missing field).

For the host:

1. The ``instances`` row's ``host`` column (cross-host case).
2. ``host_config.load().canonical_host()`` (local fallback — what every
   other ``state.db`` write uses for self-identity).

This module is pure-logic on purpose: everything that touches state.db
is wrapped in ``try/except Exception → None`` so an unreadable registry
NEVER blocks the ``/agents`` response. The whole point of these fields
is to be additive — a missing port surfaces as ``a2a_port: null /
turn_url: null`` and the caller branches accordingly.
"""

from __future__ import annotations


def _instance_endpoint(agent_name: str) -> tuple[int | None, str | None]:
    """Return ``(bound_port, host)`` from the active ``instances`` row.

    Inlines the same lookup that
    :func:`_network._peer_resolve._lookup_instance_endpoint` performs,
    but without importing from ``_network.peer`` — that module's
    circular ``from .peer import PeerError`` makes it unsafe to pull
    in from listen handlers that may load before ``peer`` itself.

    Prefers the ``bound_port`` column, falling back to legacy
    ``a2a_port`` for rows written before the family-tree columns
    existed. Returns ``(None, None)`` on any failure (best-effort
    — caller surfaces the missing field rather than a stack trace).
    """
    try:
        from .._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r.get("name") == agent_name]
        if not rows:
            return (None, None)
        # list_active_instances orders started_at DESC → newest first.
        row = rows[0]
        port = row.get("bound_port")
        if port is None:
            port = row.get("a2a_port")
        host = row.get("host") or None
        return (int(port) if port is not None else None, host)
    except Exception:  # stx-allow: fallback (reason: best-effort cross-host lookup — missing field surfaces as null)
        return (None, None)


def resolve_a2a_port(agent_name: str) -> int | None:
    """Return the bound A2A port for ``agent_name``, or ``None``.

    Sourcing: port_allocator first, instances-table fallback for
    cross-host rows. Best-effort — every IO / state.db failure
    degrades to ``None`` so the caller still ships a valid (if
    field-less) row.
    """
    # 1. Local allocator (THE source of truth for agents that ran on
    # this host — auto-port or static-port, the claim lives here).
    try:
        from .._state.port_allocator import get_port

        port = get_port(agent_name)
        if port is not None:
            return int(port)
    except Exception:  # stx-allow: fallback (reason: best-effort enrichment — missing port surfaces as a null field)
        pass
    # 2. Cross-host fallback — the dispatcher records the bound port +
    # host on the lead-side ``instances`` row for remote=True agents.
    inst_port, _ = _instance_endpoint(agent_name)
    if inst_port is not None:
        return int(inst_port)
    return None


def resolve_a2a_host(agent_name: str) -> str | None:
    """Return the host the agent listens on, or ``None``.

    Sourcing: cross-host ``instances`` row first, then this host's
    canonical name from ``host_config``. Best-effort — every failure
    degrades to ``None`` so the caller still ships a valid row.
    """
    # 1. Cross-host case — the ``instances`` row's ``host`` column is
    # the dispatcher-recorded canonical hostname for the agent.
    _, inst_host = _instance_endpoint(agent_name)
    if inst_host:
        return inst_host
    # 2. Local fallback — host_config's canonical hostname is the same
    # source every other state.db write uses for self-identity, so a
    # locally-running agent's turn_url ends up addressed by the same
    # name a cross-host peer would use.
    try:
        from .._state.host_config import load as load_host_config

        return load_host_config().canonical_host()
    except Exception:  # stx-allow: fallback (reason: host_config errors must not block the /agents response)
        return None


def derive_turn_url(host: str | None, port: int | None) -> str | None:
    """Return ``http://<host>:<port>/v1/turn`` iff both inputs are valid.

    Pure — no I/O. ``None`` for any missing / empty input (the
    receiving caller branches on ``turn_url is None`` to skip
    dispatch).
    """
    if not host:
        return None
    if port is None:
        return None
    return f"http://{host}:{port}/v1/turn"


def enrich_row_with_endpoint(row: dict) -> dict:
    """Add ``a2a_port`` and ``turn_url`` to ``row`` (idempotent).

    Reads ``row["name"]`` and computes both fields via the helpers
    above. Idempotency rule: if the row already carries a NON-NONE
    value for one of these keys, that value is KEPT — handles
    self-peer rows that already know their own endpoint (e.g. the
    lead's own ``listen_url`` neighbour writes a turn_url at
    discovery time and the registry refresh must not clobber it).
    """
    name = row.get("name") if isinstance(row, dict) else None
    if not isinstance(name, str) or not name:
        # No usable name → emit the keys as None so the row shape stays
        # uniform. (Callers iterate uniformly over agents[].)
        out = dict(row) if isinstance(row, dict) else {}
        out.setdefault("a2a_port", None)
        out.setdefault("turn_url", None)
        return out

    existing_port = row.get("a2a_port")
    existing_url = row.get("turn_url")

    a2a_port = existing_port if existing_port is not None else resolve_a2a_port(name)
    if existing_url is not None:
        turn_url = existing_url
    else:
        host = resolve_a2a_host(name)
        turn_url = derive_turn_url(host, a2a_port)

    out = dict(row)
    out["a2a_port"] = a2a_port
    out["turn_url"] = turn_url
    return out


__all__ = [
    "derive_turn_url",
    "enrich_row_with_endpoint",
    "resolve_a2a_host",
    "resolve_a2a_port",
]
