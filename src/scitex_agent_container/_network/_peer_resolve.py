"""Agent-name → ``/v1/turn`` URL resolution for the peer client.

Extracted from :mod:`peer` (which would otherwise exceed the 512-line
per-file cap after the cross-host ``_lookup_instance_endpoint``
read-back landed). Mirrors the existing ``_peer_timeout`` /
``_peer_dispatch`` extractions: the transport machinery
(``post_turn`` / ``post_turn_to_url``) stays in :mod:`peer`; this module
owns the "where does this agent listen?" resolution.

:class:`PeerError` is imported from :mod:`peer` — the same direction
``_peer_timeout`` already imports it, so there is no cycle (nothing in
:mod:`peer` imports this module at its own load time; the re-export at
the bottom of :mod:`peer` runs after ``PeerError`` is defined).
"""

from __future__ import annotations

from typing import Any

from .peer import PeerError


def resolve_peer_url(agent_name: str) -> str:
    """Resolve the ``/v1/turn`` URL for a named agent.

    Looks up the agent's YAML via the standard discovery chain
    (project-local → ``~/.scitex/agent-container/agents/`` → env →
    fleet dirs), reads ``spec.a2a.{host,port}`` and ``spec.host``,
    and returns the URL the caller should POST to.

    When the YAML pins ``spec.a2a.port: auto`` the actual bound port
    isn't in the YAML — it lives in the ``a2a_ports`` claim ledger,
    per-host PostgreSQL since 2026-08-28, where the port allocator
    persists the claim at agent_start.  We consult that ledger by
    ``agent_name`` to discover the real port.
    See foundation-polish bug 1.

    For **cross-host** agents (``spec.host`` is set to a non-local peer
    name) the returned URL is a synthetic ``ssh://<host>:<port>/v1/turn``
    form that :func:`peer.post_turn_to_url` recognises and dispatches via
    ``ssh <host> curl http://127.0.0.1:<port>/...``. This way the agent
    can keep ``spec.a2a.host: 127.0.0.1`` (more secure) and remote
    callers still reach it through the ssh control plane — no LAN
    exposure required, no DNS resolution needed for ssh aliases.

    The same ``spec.host`` field is consulted by ``sac start`` for
    dispatch, so post-turn cannot disagree about where the agent
    lives. The legacy ``spec.remote`` block was deleted in WI-6
    (handoff §6, 2026-05-20).
    """
    from ..config._resolve import resolve_config

    try:
        yaml_path = resolve_config(agent_name)
    except FileNotFoundError as exc:
        raise PeerError(str(exc)) from exc

    a2a_host, a2a_port, dest_host = _read_yaml_endpoints(yaml_path)
    if a2a_port is None:
        a2a_port = _lookup_bound_port(agent_name)
    if a2a_port is None:
        # Cross-host fallback (sac-agent-spawn design, Rule B/F): the
        # local port allocator only holds claims for agents that ran on
        # THIS host. A remote-dispatched agent's bound port + host live
        # in the ``instances`` table (written by the cross-host
        # dispatcher's ``record_instance_start`` with ``remote=True``).
        # Consult it so post-turn resolves a remote agent instead of
        # raising "port: auto and no bound port recorded" — the exact
        # gap that left the lead unable to reach clew on Spartan
        # (2026-05-23).
        inst_port, inst_host = _lookup_instance_endpoint(agent_name)
        if inst_port is not None:
            a2a_port = inst_port
            if inst_host and not dest_host:
                dest_host = inst_host
    if a2a_port is None:
        # FAIL LOUD (#192): no live endpoint resolved. Don't raise a bare
        # "is the agent running?" — name the last-known host + timestamp +
        # locality from the cross-host registry, and explicitly refuse to
        # assume local.
        from ._peer_faillloud import raise_unresolvable_instance

        raise_unresolvable_instance(
            agent_name, port_is_auto=_yaml_port_is_auto(yaml_path)
        )
    if dest_host and not _is_local_host(dest_host):
        # Tunnel via ssh — agent's a2a.host can stay loopback (default).
        return f"ssh://{dest_host}:{a2a_port}/v1/turn"
    # About to resolve LOCAL (spec.host empty or pointing at this machine).
    # Before trusting that, check the cross-host registry for a FRESH
    # remote=True instance row that contradicts the local resolution — the
    # #192 unbreakable wrong state (a stale local-allocator port made the
    # resolver land on 127.0.0.1 while the agent was actually running on
    # another host). If one exists, FAIL LOUD rather than silently send to
    # the wrong endpoint.
    from ._peer_faillloud import detect_contradicting_remote_instance

    contradiction = detect_contradicting_remote_instance(
        agent_name, resolved_local=True
    )
    if contradiction is not None:
        c_host = contradiction.get("host") or "<unknown-host>"
        c_port = contradiction.get("bound_port") or contradiction.get("a2a_port")
        raise PeerError(
            f"agent {agent_name!r}: local resolution yielded "
            f"http://{a2a_host or '127.0.0.1'}:{a2a_port}, but the cross-host "
            f"registry holds a live remote=True instance on host {c_host!r} "
            f"(bound_port={c_port}). Refusing to send to a stale local "
            f"endpoint. Reach it via the holding host, or stop the stale "
            f"local row if the remote one is wrong."
        )
    # Local agent (spec.host empty or pointing at this machine).
    host = a2a_host or "127.0.0.1"
    return f"http://{host}:{a2a_port}/v1/turn"


def _is_local_host(dest_host: str) -> bool:
    """Return True iff ``dest_host`` names the current machine.

    Consults host_config's canonical hostname so an agent pinned to
    its own host is reached via http://127.0.0.1, not via ssh-to-self.
    Any resolution failure raises — we do not silently treat unknown
    hosts as local (would mask config drift).
    """
    from .._state.host_config import load as load_host_config

    cfg = load_host_config()
    canonical = cfg.canonical_host()
    return dest_host == canonical or dest_host in cfg.host.aliases


def _lookup_bound_port(agent_name: str) -> int | None:
    """Return the port the allocator persisted for ``agent_name``, else None.

    The YAML may say ``spec.a2a.port: auto`` (or omit ``port`` entirely)
    when the spec author wants the runtime to pick a free port. The
    actual port is recorded in the ``a2a_ports`` claim ledger (per-host
    PostgreSQL) by :func:`_state.port_allocator.claim_port` at
    agent_start. The peer client consults the same ledger so it can talk to an
    auto-port agent without having to re-parse + reproduce the
    allocator's logic.

    Failure modes (registry missing, schema not yet created, sqlite
    locked) degrade to ``None`` so the caller raises the same "no
    port recorded" PeerError it would for a static-port misconfig.
    """
    try:
        from .._state.port_allocator import get_port

        return get_port(agent_name)
    except Exception:  # stx-allow: fallback (reason: best-effort lookup — caller raises a clear PeerError when None)
        return None


def _lookup_instance_endpoint(agent_name: str) -> tuple[int | None, str | None]:
    """Return ``(bound_port, host)`` from the active ``instances`` row.

    The cross-host dispatcher records a lead-side ``instances`` row with
    ``remote=True``, the peer ``host``, and the peer-resolved
    ``bound_port`` (``record_instance_start`` in
    ``cli_pkg/lifecycle/_dispatch.py``). When the local port allocator
    has no claim for ``agent_name`` — always the case for an agent that
    ran on a DIFFERENT host — this is where the bound port + host live.

    Prefers the ``bound_port`` column, falling back to legacy
    ``a2a_port`` for rows written before the family-tree columns
    existed. Returns ``(None, None)`` when no active row exists or any
    lookup fails (caller raises the same clear PeerError it would for a
    missing port).
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
    except Exception:  # stx-allow: fallback (reason: best-effort cross-host lookup — caller raises a clear PeerError when None)
        return (None, None)


def _yaml_port_is_auto(yaml_path: str) -> bool:
    """Return True iff ``spec.a2a.port`` is the literal string ``"auto"``.

    Used to decide which PeerError to raise when no bound port is
    available: an auto-port spec with no registry entry means "agent
    isn't running", while a missing port means "the spec is incomplete".
    Best-effort — any IO / parse failure returns False.
    """
    try:
        from pathlib import Path

        import yaml as _yaml

        raw = _yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except Exception:  # stx-allow: fallback (reason: best-effort detection; falls through to generic no-port error)
        return False
    spec = (raw.get("spec") or {}) if isinstance(raw, dict) else {}
    a2a = spec.get("a2a") or {}
    port = a2a.get("port") if isinstance(a2a, dict) else None
    return isinstance(port, str) and port.strip().lower() == "auto"


def _read_yaml_endpoints(yaml_path: str) -> tuple[str | None, int | None, str | None]:
    """Return ``(a2a_host, a2a_port, dest_host)`` from a v3 YAML file.

    ``dest_host`` is the agent's destination peer name (the value of
    ``spec.host``). It is the same lookup key used by cross-host
    dispatch, so peer routing and start dispatch agree on a single
    field. SSH alias resolution happens at the SSH layer
    (``~/.ssh/config``), not here.

    Best-effort: any IO / parse failure produces ``(None, None, None)``.
    """
    try:
        from pathlib import Path

        import yaml as _yaml

        v3 = _yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except Exception:  # stx-allow: fallback (reason: malformed YAML degrades to "no endpoints" — caller raises a clear PeerError)
        return (None, None, None)
    spec = (v3.get("spec") or {}) if isinstance(v3, dict) else {}
    a2a: dict[str, Any] = spec.get("a2a") or {}
    a2a_port = a2a.get("port")
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        a2a_port = None
    a2a_host = a2a.get("host")
    if not isinstance(a2a_host, str) or not a2a_host.strip():
        a2a_host = None
    # spec.host (HostsSpec) is the single source of truth for the
    # destination peer. Can be empty (local), a string (one host),
    # or a list (priority chain — last entry wins for routing).
    raw_host = spec.get("host")
    dest_host: str | None = None
    if isinstance(raw_host, str) and raw_host.strip():
        dest_host = raw_host.strip()
    elif isinstance(raw_host, list) and raw_host:
        last = raw_host[-1]
        if isinstance(last, str) and last.strip():
            dest_host = last.strip()
    return (a2a_host, a2a_port, dest_host)


__all__ = [
    "resolve_peer_url",
    "_is_local_host",
    "_lookup_bound_port",
    "_lookup_instance_endpoint",
    "_yaml_port_is_auto",
    "_read_yaml_endpoints",
]
