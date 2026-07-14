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

…and then, crucially, a host that names THIS MACHINE is normalised to
``127.0.0.1`` by :func:`derive_turn_url`. Do NOT "simplify" that away:
the canonical hostname resolves to ``127.0.1.1`` on stock Debian /
Ubuntu / WSL while the a2a sidecar binds ``127.0.0.1``, so the
un-normalised URL was refused for EVERY local consumer, deterministically.
See :mod:`._local_host` for the evidence and for why we normalise the
address rather than widen the sidecar's bind to ``0.0.0.0``.

This module is pure-logic on purpose: everything that touches state.db
is wrapped in ``try/except Exception → None`` so an unreadable registry
NEVER blocks the ``/agents`` response. The whole point of these fields
is to be additive — a missing port surfaces as ``a2a_port: null /
turn_url: null`` and the caller branches accordingly.
"""

from __future__ import annotations

from ._local_host import LOOPBACK_HOST, is_local_host


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


def derive_turn_url(
    host: str | None,
    port: int | None,
    *,
    local_aliases: frozenset[str] | None = None,
) -> str | None:
    """Return a REACHABLE ``http://<host>:<port>/v1/turn``, or ``None``.

    A LOCAL host is normalised to ``127.0.0.1`` — the address the a2a
    sidecar actually binds (``a2a/_server.py``: ``host: str =
    "127.0.0.1"``). This is the fix for a deterministic outage: the
    derived URL used to name the machine's own canonical hostname, which
    on stock Debian / Ubuntu / WSL resolves to ``127.0.1.1``::

        $ getent hosts ywata-note-win
        127.0.1.1   ywata-note-win.localdomain ywata-note-win

        127.0.0.1:19017        -> OPEN
        ywata-note-win:19017   -> CONNECTION REFUSED

    ``127.0.1.1`` is a loopback address, but it is a DIFFERENT loopback
    address from the one the sidecar listens on, so EVERY connection to
    the derived URL was refused — 100% of the time, for every local
    consumer. See :mod:`._local_host` for why we normalise the address
    rather than rebind the sidecar to ``0.0.0.0``.

    A genuinely REMOTE host keeps its name: a cross-host peer is not on
    our loopback, and rewriting its URL would point every cross-host
    dispatch back at ourselves.

    ``None`` for any missing / empty input (the receiving caller branches
    on ``turn_url is None`` to skip dispatch). An unresolvable host is
    NEVER silently promoted to loopback — that would advertise OUR
    sidecar as somebody else's endpoint.

    ``local_aliases`` is injectable so the derivation can be exercised
    without depending on the runner's real hostname.
    """
    if not host:
        return None
    if port is None:
        return None
    if is_local_host(host, aliases=local_aliases):
        host = LOOPBACK_HOST
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


def _load_spec_dict(agent_name: str) -> dict | None:
    """Return the raw v3 spec dict for ``agent_name``, or ``None``.

    Best-effort: resolves the agent's ``spec.yaml`` via the same
    :func:`config._resolve.resolve_config` the status route uses, then
    parses it. Every failure (unknown name, unreadable / malformed YAML,
    ambiguous registry) degrades to ``None`` so a peers row is NEVER
    blocked — the registry list is a discovery surface, not a gate.
    """
    try:
        import yaml

        from ..config._resolve import resolve_config

        path = resolve_config(agent_name)
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # stx-allow: fallback (reason: best-effort role/owner enrichment — a missing/unreadable spec surfaces as absent fields)
        return None


def resolve_agent_identity(agent_name: str) -> dict:
    """Return the spec-authored identity for ``agent_name``, best-effort.

    Operator directive 2026-07-06: an agent's ROLE (headline) +
    RESPONSIBILITIES (bullets), plus its groups / purpose / owned repo,
    must be discoverable fleet-wide via ``a2a peers`` so a peer sees who
    does what without asking.

    Field extraction is delegated to
    :func:`a2a._card_identity.spec_identity` — the SAME projection the
    AgentCard uses — so the two a2a surfaces (peer rows + AgentCard)
    never drift. Returns only the keys the spec declares
    (omit-if-missing); ``{}`` on any failure.
    """
    v3 = _load_spec_dict(agent_name)
    if v3 is None:
        return {}
    try:
        from ..a2a._card_identity import spec_identity

        return spec_identity(v3)
    except Exception:  # stx-allow: fallback (reason: best-effort — an identity-projection failure surfaces as absent fields)
        return {}


# Optional identity fields carried on a peers row IN ADDITION to the
# always-present ``role`` headline. Each is added only when the spec
# actually declares it (omit-if-missing), so a row never advertises a
# blank.
_OPTIONAL_IDENTITY_KEYS = ("responsibilities", "groups", "purpose", "project")


def enrich_row_with_role_owner(row: dict, *, resolver=resolve_agent_identity) -> dict:
    """Add the spec-authored identity fields to ``row`` (idempotent, best-effort).

    Mirrors :func:`enrich_row_with_endpoint`: a row that already carries a
    NON-NONE value for a field keeps its own (a self-peer / registry row
    is the authoritative source for what it already knows). ``resolver``
    is injectable so the pure enrichment logic is testable without an
    on-disk spec.

    ``role`` is ALWAYS emitted (``None`` when the agent has no resolvable
    role) so the ``GET /agents`` response shape stays uniform — ``role``
    is THE discovery headline. The remaining identity fields
    (``responsibilities`` / ``groups`` / ``purpose`` / ``project``) are
    added ONLY when the spec declares them (omit-if-missing).
    """
    if not isinstance(row, dict):
        return row
    out = dict(row)
    name = row.get("name")
    identity: dict = {}
    if isinstance(name, str) and name:
        identity = resolver(name) or {}
    # role — the headline; always present, pre-existing non-None wins.
    if out.get("role") is None:
        out["role"] = identity.get("role")
    # optional identity fields — omit-if-missing, pre-existing wins.
    for key in _OPTIONAL_IDENTITY_KEYS:
        if out.get(key) is None and key in identity:
            out[key] = identity[key]
    return out


def enrich_row(row: dict) -> dict:
    """Apply BOTH registry enrichments to ``row`` — the composed shape every
    registry surface ships.

    Layers the endpoint fields (``a2a_port`` / ``turn_url``) and the
    spec-authored identity (``role`` + ``responsibilities`` / ``groups`` /
    ``purpose`` / ``project``) in one call so the ``GET /agents`` list and
    ``GET /agents/<name>/status`` bodies carry an identical, uniform shape.
    Both layers are idempotent + best-effort, so this is safe to apply to
    rows that already carry some of the fields.
    """
    return enrich_row_with_role_owner(enrich_row_with_endpoint(row))


__all__ = [
    "derive_turn_url",
    "enrich_row",
    "enrich_row_with_endpoint",
    "enrich_row_with_role_owner",
    "resolve_a2a_host",
    "resolve_a2a_port",
    "resolve_agent_identity",
]
