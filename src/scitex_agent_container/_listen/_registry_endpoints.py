"""Per-row a2a_port + turn_url enrichment for the registry list/status routes.

The lead dispatch a2a dc6fd23387f64e329049d218cf85a4d4 (Q1) asked for
``GET /agents`` and ``GET /agents/<name>/status`` to surface two extra
fields on every row:

* ``a2a_port`` — the port the agent's local sidecar is bound to.
* ``turn_url`` — the derived ``http://<host>:<port>/v1/turn`` URL the
  caller can POST to (used by scitex-todo's notify resolver P3a-b).

Sourcing chain — the single source of truth for "where does an agent
listen" is, in order:

1. ``_state.port_allocator.get_port(name)`` — the ``a2a_ports`` claim
   ledger, per-host PostgreSQL since 2026-08-28 (always populated on
   local agent_start).
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


def port_claims_map() -> dict[str, int]:
    """Return ``{agent_name: port}`` for every active claim, in ONE query.

    The batched counterpart to :func:`resolve_a2a_port`, for callers that
    enrich many rows. Pass the result to ``enrich_row*(…, ports=…)``.

    WHY THIS EXISTS — measured 2026-08-09, warm, wall clock, 19 rows:

        enrich_row over all rows          635.6ms
          resolve_a2a_port x all          222.2ms   (~11.7ms/row)
          resolve_agent_identity x all    256.9ms

    `resolve_a2a_port` calls `port_allocator.get_port(name)` PER ROW, and each
    of those opens state.db (the CLI-side note records ~3 opens, ~62ms/agent on
    a full host). `list_claims()` answers for every agent in one query.

    This is FIX A#1 from `sac-agents-list-slowness-measured` (July), which was
    applied to `cli_pkg/_helpers/_agent_list.py` and never reached this path —
    the third instance in one night of a fix landing on one of two parallel
    listing implementations. Best-effort: any failure yields ``{}`` and every
    caller falls back to the per-row lookup, so this can only be faster, never
    wronger.
    """
    try:
        from .._state.port_allocator import list_claims

        out: dict[str, int] = {}
        for claim in list_claims():
            nm = claim.get("name")
            pt = claim.get("port")
            if isinstance(nm, str) and nm and pt is not None:
                out[nm] = int(pt)
        return out
    except Exception:  # stx-allow: fallback (reason: best-effort batch — callers degrade to the per-row resolve_a2a_port)
        return {}


def enrich_row_with_endpoint(row: dict, *, ports: dict[str, int] | None = None) -> dict:
    """Add ``a2a_port`` and ``turn_url`` to ``row`` (idempotent).

    Reads ``row["name"]`` and computes both fields via the helpers
    above. Idempotency rule: if the row already carries a NON-NONE
    value for one of these keys, that value is KEPT — handles
    self-peer rows that already know their own endpoint (e.g. the
    lead's own ``listen_url`` neighbour writes a turn_url at
    discovery time and the registry refresh must not clobber it).

    ``ports`` is an optional pre-computed ``{name: port}`` from
    :func:`port_claims_map`. A name ABSENT from it falls through to the
    per-row :func:`resolve_a2a_port`, which also carries the cross-host
    instances-table fallback — so a partial map degrades in speed only, never
    in correctness. Omitting it preserves the original per-row behaviour
    exactly, which is why every existing caller is unaffected.
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

    if existing_port is not None:
        a2a_port = existing_port
    elif ports is not None and name in ports:
        a2a_port = ports[name]
    else:
        a2a_port = resolve_a2a_port(name)
    if existing_url is not None:
        turn_url = existing_url
    else:
        host = resolve_a2a_host(name)
        turn_url = derive_turn_url(host, a2a_port)

    out = dict(row)
    out["a2a_port"] = a2a_port
    out["turn_url"] = turn_url
    return out


# Parsed-spec cache, keyed on the file's IDENTITY so an edited spec is never
# served stale. Measured 2026-08-09 (warm, WALL CLOCK, 19 rows):
#
#     resolve_agent_identity x all rows   256.9ms   (~13.5ms each)
#
# One read+parse per agent per request, with no duplication WITHIN a request —
# so this buys nothing on a single call and everything on the next one. Agents
# poll this listing, so repeat requests are the normal case.
#
# Deliberately NOT justified by parse COUNT. PR #903 removed 23 of 41 parses per
# request (host_config) and moved wall time not at all, because the cProfile
# figure that motivated it had inflated PyYAML's call-dense pure-Python work.
# The 256.9ms above is direct wall-clock measurement of this specific call, which
# is why it is expected to pay where that one did not — and the A/B must confirm
# it before anyone claims it did.
_SPEC_CACHE: dict[tuple[str, int, int], dict | None] = {}


def _load_spec_dict(agent_name: str) -> dict | None:
    """Return the raw v3 spec dict for ``agent_name``, or ``None``.

    Best-effort: resolves the agent's ``spec.yaml`` via the same
    :func:`config._resolve.resolve_config` the status route uses, then
    parses it. Every failure (unknown name, unreadable / malformed YAML,
    ambiguous registry) degrades to ``None`` so a peers row is NEVER
    blocked — the registry list is a discovery surface, not a gate.
    """
    try:
        import os

        import yaml

        from ..config._resolve import resolve_config

        path = resolve_config(agent_name)
        # Cache on the file's IDENTITY, not the agent name — an edited spec
        # must be picked up, and two names resolving to one file share an entry.
        try:
            _st = os.stat(path)
            _key: tuple[str, int, int] | None = (
                str(path),
                _st.st_mtime_ns,
                _st.st_size,
            )
        except OSError:  # stx-allow: fallback (reason: stat failure degrades to an uncached read, never to a wrong answer)
            _key = None
        if _key is not None and _key in _SPEC_CACHE:
            return _SPEC_CACHE[_key]
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        out = data if isinstance(data, dict) else None
        if _key is not None:
            if len(_SPEC_CACHE) >= 256:
                for _stale in list(_SPEC_CACHE)[:128]:
                    _SPEC_CACHE.pop(_stale, None)
            _SPEC_CACHE[_key] = out
        return out
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


def enrich_row(row: dict, *, ports: dict[str, int] | None = None) -> dict:
    """Apply BOTH registry enrichments to ``row`` — the composed shape every
    registry surface ships.

    Layers the endpoint fields (``a2a_port`` / ``turn_url``) and the
    spec-authored identity (``role`` + ``responsibilities`` / ``groups`` /
    ``purpose`` / ``project``) in one call so the ``GET /agents`` list and
    ``GET /agents/<name>/status`` bodies carry an identical, uniform shape.
    Both layers are idempotent + best-effort, so this is safe to apply to
    rows that already carry some of the fields.

    ``ports`` is forwarded to :func:`enrich_row_with_endpoint` — pass
    :func:`port_claims_map` once when enriching many rows. Omitted, behaviour is
    unchanged from before the batch existed.
    """
    return enrich_row_with_role_owner(enrich_row_with_endpoint(row, ports=ports))


__all__ = [
    "derive_turn_url",
    "enrich_row",
    "enrich_row_with_endpoint",
    "enrich_row_with_role_owner",
    "port_claims_map",
    "resolve_a2a_host",
    "resolve_a2a_port",
    "resolve_agent_identity",
]
