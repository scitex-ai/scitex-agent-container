"""``GET /agents`` — the peer-discovery route (extracted from server.py).

Backs the ``a2a_peers`` MCP tool: this is THE surface an agent consults to
answer "is this peer alive and able to act?" before handing it work. It is
therefore the surface where "registered" and "reachable" must not be
conflated — see :mod:`._reachability` for why that conflation was a P1 bug
(deaf agents advertising themselves as ``active`` while silently swallowing
every message).

Split out of :mod:`scitex_agent_container._listen.server` (which sits at the
per-file line cap), mirroring the existing extractions of ``agent_send`` /
``agents_start`` / ``agent_tail`` / the node inbox channel. ``server.py``
re-imports :func:`list_agents` so route registration and the historical
``from ..._listen.server import list_agents`` import path keep working.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._state.registry import Registry
from .._state.state_db_instances_store import INSTANCES_STORE

__all__ = ["list_agents"]

log = logging.getLogger(__name__)


def _resolved_store() -> str:
    """WHICH store answered, as a printable locator.

    ``host_store`` RESOLVES a target and does not connect, so naming the
    store costs this route nothing. All three sources below read the same
    per-host PostgreSQL, so one locator describes every one of them.

    This printed ``state_db.DEFAULT_DB_PATH`` until 2026-08-29 — a SQLite
    path this route had stopped opening, and by then never opened at all.
    Dropping the field was the wrong repair: the field exists because on
    2026-08-09 an empty ``agents`` list was read as "the fleet is gone"
    when the honest reading was "you asked the wrong database", and a
    caller that cannot see WHICH database cannot tell those apart. So it
    names the real one instead.
    """
    from scitex_dev.store import host_store

    return str(host_store(pkg="scitex_agent_container", name=INSTANCES_STORE).locator)


async def list_agents(request: Request) -> JSONResponse:
    """List agents the local Registry knows about + self-peers.

    Every row carries the INBOX-SUBSCRIBER OBSERVATION — ``inbox_subscribers``
    and ``inbox_reachable`` — alongside the registry's declarations.

    All three sources below report what the fleet has DECLARED about an agent
    (a pid, a port, a group, a start time). None of them can tell you whether
    a message sent to that agent would actually wake anybody: that depends on
    whether its inbox adapter is subscribed to the channel bus, which only the
    broker knows. Publishing the declaration alone is what let two of four
    agents advertise ``active`` while silently swallowing every ``a2a_send``.
    So we publish the observation next to it, distinctly. See
    :mod:`._reachability`.

    Three sources, concatenated in this order:

    1. Container-agent rows from :meth:`Registry.list_all` — the
       traditional ``sac a2a peers`` shape (``name`` / ``config`` /
       ``pid`` / ``started_at`` / ``screen``).
    2. Self-peer rows from :func:`_self_peers.discover_self_peers`
       — any agent dir whose ``spec.yaml`` carries only a
       ``listen_url`` (no container ``spec`` block, no
       ``apiVersion``). These have no ``pid`` / ``screen`` — they
       are external listen sessions that own a port and want to be
       discoverable. Carries ``"kind": "self-peer"`` so peer-aware
       clients can branch on the source.
    3. Comms-node self-registrations from ``comms_nodes`` (ADR-0014).

    Dedup: a self-peer whose ``name`` already appears in the
    container-row list loses to the container row (the running
    container is the more authoritative source). Order matches
    operator-facing convention: container rows first, then
    self-peers alphabetically by ``name``.
    """
    rows: list[dict] = []
    seen_names: set[str] = set()
    try:
        reg = Registry()
        for row in reg.list_all():
            rows.append(row)
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str):
                seen_names.add(name)
    except Exception as exc:  # stx-allow: fallback (reason: surface a JSON error to the caller rather than ASGI 500 stack)
        return JSONResponse({"error": str(exc)}, status_code=500)

    n_registry = len(rows)
    _append_self_peers(rows, seen_names)
    n_self_peers = len(rows) - n_registry
    _append_comms_nodes(rows, seen_names)
    n_comms_nodes = len(rows) - n_registry - n_self_peers

    # Q1 (lead dispatch a2a dc6fd23387f64e329049d218cf85a4d4): surface
    # ``a2a_port`` + derived ``turn_url`` on every row so scitex-todo's
    # notify resolver (P3a-b) can dispatch nudge→turn without redeploy.
    # Idempotent: self-peer rows that already carry a non-None value
    # keep theirs (the discovery layer is the authoritative source for
    # those).
    from ._registry_endpoints import enrich_row, port_claims_map

    # ONE query for every port claim, instead of one state.db lookup per row.
    # Measured 2026-08-09 (warm, wall clock, 19 rows): the per-row path cost
    # 222.2ms of a ~635ms enrichment. This is FIX A#1 from the July card
    # `sac-agents-list-slowness-measured`, which landed in the CLI's row builder
    # and never reached this one. Best-effort — an empty map simply falls back
    # to the per-row lookup, which also carries the cross-host instances-table
    # fallback that the claims table alone does not.
    _ports = port_claims_map()
    rows = [enrich_row(row, ports=_ports) for row in rows]
    rows = await _annotate_reachability(request, rows)
    rows = _annotate_faults(rows)

    # AN EMPTY LIST IS NOT AN ANSWER ON ITS OWN.
    #
    # INCIDENT 2026-08-09: this route returned {"agents": []} with HTTP 200
    # while twelve agents were demonstrably alive and exchanging a2a
    # messages — the registry had briefly lost its registrations. Two
    # independent sessions read that empty list as "there are no agents",
    # and one of them (mine) escalated fleet-wide data loss off the back of
    # it. "I cannot see the registry" and "there are no agents" are
    # different facts and this route rendered them identically.
    #
    # We cannot tell those apart from row counts alone — both are zero. So
    # publish what the caller needs to judge for itself: WHICH store was
    # consulted, and what each source contributed. `sac db show` carried the
    # same remedy until it was deleted with the SQLite read surface on
    # 2026-08-29. `agents` keeps its shape, so existing consumers are
    # untouched.
    sources = {
        "store": _resolved_store(),
        "registry_rows": n_registry,
        "self_peer_rows": n_self_peers,
        "comms_node_rows": n_comms_nodes,
    }
    if not rows:
        # Zero from EVERY source, on a host whose listen daemon is up enough
        # to answer this request. That is unusual rather than impossible, and
        # saying so is the difference between a caller believing it and a
        # caller checking.
        sources["note"] = (
            "ALL THREE sources returned zero rows. This may genuinely mean no "
            "agents are registered here, but it is also what a registry the "
            "daemon cannot currently see looks like — a restart that has not "
            "replayed registrations, or a daemon resolving a different store "
            "than the agents register into. Do NOT read this as proof that no "
            "agent is alive: check `tmux ls` on the host, which is independent "
            "of this registry."
        )
        log.warning(
            "GET /agents returned ZERO rows from all three sources "
            "(store=%s). If agents are running, the registry is not being "
            "seen — this is not evidence that none exist.",
            _resolved_store(),
        )
    return JSONResponse({"agents": rows, "sources": sources})


def _annotate_faults(rows: list[dict]) -> list[dict]:
    """Add ``fault`` / ``fault_detail`` — the CAUSE behind a zero.

    ``inbox_subscribers: 0`` is confounded: it means a detached inbox adapter
    OR an agent that is not running, and the broker cannot tell those apart.
    Publishing the raw zero here and trusting every caller to remember the
    caveat has failed repeatedly — most recently on 2026-08-12, when 9 of the
    15 rows on this host reported ``unreachable`` and every one of them was a
    STOPPED agent, read by a peer as a fleet going deaf.

    So pair it with the host's tmux table, which is independent of the broker,
    and name the result. See :mod:`._inbox_fault`.

    Best-effort in the SAFE direction, like reachability above: a snapshot we
    could not take yields NO faults, never a fleet-wide "not running".
    """
    from ._inbox_fault import annotate_faults, session_snapshot

    try:
        return annotate_faults(rows, snapshot=session_snapshot())
    except Exception as exc:  # stx-allow: fallback (reason: fault classification is an ADVISORY overlay — it must never take down the peer-discovery route the fleet depends on)
        log.warning(
            "list_agents: fault classification failed (returning rows without "
            "the `fault` overlay): %s",
            exc,
        )
        return rows


async def _annotate_reachability(request: Request, rows: list[dict]) -> list[dict]:
    """Add ``inbox_subscribers`` / ``inbox_reachable`` to every row.

    Reads the live broker — the ONLY authority on whether an agent's inbox
    adapter is actually attached. One snapshot (a single lock take on an
    in-memory dict), so this adds no I/O per row and cannot stall the route.

    Best-effort in the SAFE direction: if the broker cannot be read at all,
    every row is annotated ``unknown`` rather than ``unreachable``. "I could
    not check" must never be rendered as death — that error would accuse
    healthy agents of being deaf, and the remedy a caller reaches for on a
    false death verdict is destructive.
    """
    from ._reachability import UNKNOWN, annotate_rows, resolve_annotation_host

    try:
        broker = request.app.state.inbox
        counts = await broker.subscriber_counts()
        # NOT a bare ``app.state.local_host`` read. That is ``None`` in
        # production (see ``resolve_annotation_host``), and reading it alone is
        # what kept every row on THIS host annotated ``unknown`` on the very
        # endpoint the reachability reports come from.
        local_host = resolve_annotation_host(request.app.state)
    except Exception as exc:  # stx-allow: fallback (reason: an unreadable broker must degrade to UNKNOWN, never to a false 'unreachable' verdict against healthy agents)
        log.warning(
            "list_agents: could not read inbox broker (reporting reachability "
            "as %r for every row — NOT as unreachable): %s",
            UNKNOWN,
            exc,
        )
        return [
            {**row, "inbox_subscribers": None, "inbox_reachable": UNKNOWN}
            for row in rows
        ]
    return annotate_rows(rows, subscriber_counts=counts, local_host=local_host)


def _append_self_peers(rows: list[dict], seen_names: set[str]) -> None:
    """Self-peers — best-effort. Failures here must NOT mask a healthy
    container-row response (an unreadable agents dir is operator state, not a
    listen failure).

    Runtime self-identity is derived from host_config — the same source the
    existing channel/listen self-registration paths consult. Missing
    host_config / missing ``lead:`` block degrades to ``None``;
    :func:`discover_self_peers` then surfaces the literal ``self`` dir as
    ``"self"`` with a logged warning, which is the loudest signal short of
    failing the request.
    """
    try:
        from ..config._resolve import _search_dirs
        from ._self_peers import discover_self_peers

        primary, env_dirs, fleet_dirs = _search_dirs()
        search_dirs = [*env_dirs, primary, *fleet_dirs]
        self_identity = _resolve_runtime_self_identity()
        for peer in discover_self_peers(search_dirs, self_identity=self_identity):
            if peer["name"] in seen_names:
                continue
            rows.append(peer)
            seen_names.add(peer["name"])
    except Exception as exc:  # stx-allow: fallback (reason: self-peer discovery must never block the registry response)
        log.warning(
            "list_agents: self-peer discovery failed (returning registry rows only): %s",
            exc,
        )


def _append_comms_nodes(rows: list[dict], seen_names: set[str]) -> None:
    """Comms-node self-registrations (operator mandate 2026-06-14): ANY
    process that loaded the sac MCP and self-registered into the
    ``comms_nodes`` table (e.g. ``sac mcp channel --name lead``, or any
    CLI/SDK session running the channel adapter) MUST appear in ``a2a peers``
    at startup -- no exceptions. Such nodes are not in the Registry (no
    container) and can live outside the filesystem self-peer search dirs, so
    without this source the lead (and any bare sac-MCP session) is invisible
    here. Best-effort: a read failure must not mask the rest of the response.
    """
    try:
        from .._state.state_db_comms_nodes import list_comms_nodes

        for node in list_comms_nodes():
            if node["name"] in seen_names:
                continue
            rows.append(
                {
                    "name": node["name"],
                    "host": node["host"],
                    "a2a_port": node["a2a_port"],
                    "kind": "comms-node",
                    "registered_at": node.get("registered_at"),
                    "updated_at": node.get("updated_at"),
                }
            )
            seen_names.add(node["name"])
    except Exception as exc:  # stx-allow: fallback (reason: comms-node surfacing must never block the registry response)
        log.warning(
            "list_agents: comms_nodes surfacing failed (returning prior rows): %s",
            exc,
        )


def _resolve_runtime_self_identity() -> str | None:
    """Return the running listen's runtime identity, or ``None``.

    Reads :func:`host_config.load().lead.name` — the same source the
    existing channel/listen self-registration paths
    (:mod:`_mcp._channel_self_register`,
    :func:`cli_pkg.listen_cmds._register_self_comms_node`) consult.
    A missing ``lead:`` block in host_config returns ``None`` — the
    self-peer discovery downstream then surfaces the literal
    ``self`` dir as ``"self"`` so the operator sees the gap rather
    than getting a silently-renamed peer row.

    Generic on purpose: there is no name-specific branching here.
    ``host_config.lead.name`` is THE host's "who am I" answer for
    operator-class sessions; a future evolution that supports
    multiple self-identities on one host would extend the host_config
    shape, not insert per-name special cases here.
    """
    try:
        from .._state.host_config import load as load_host_config

        cfg = load_host_config()
        lead = getattr(cfg, "lead", None)
        if lead is not None:
            name = getattr(lead, "name", None)
            if isinstance(name, str) and name.strip():
                return name
    except Exception:  # stx-allow: fallback (reason: host_config errors must never block the /agents response)
        pass
    return None
