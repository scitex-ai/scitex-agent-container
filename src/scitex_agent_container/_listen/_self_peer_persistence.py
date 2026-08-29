"""Persist self-peers discovered by :func:`_self_peers.discover_self_peers`
into the ``comms_nodes`` table.

Listen-side counterpart of :mod:`_mcp._channel_self_register`. The
channel path UPSERTs the running ``sac mcp channel`` session into
``comms_nodes`` so it survives a reboot. This module does the same
for every peer the LISTEN discovers via the generic cwd-walk for
``agents/self/spec.yaml`` — closing the deferred-follow-up flagged
inside :mod:`_listen._self_peers`:

    Out of scope (deferred follow-ups):
      * Cross-host comms_nodes UPSERT — _mcp._channel_self_register
        already covers the channel path. This module is the
        listen-side analogue.

Before this module, ``discover_self_peers`` rows were only visible to
clients that hit ``GET /agents`` against a live listen — restart the
listen and the federated graph forgot every self-peer until the next
client request re-warmed the in-memory list. With persistence in
place, the rows survive the restart in ``comms_nodes`` and
``sac a2a peers`` keeps reporting them across reboots.

DESIGN
------

* **Idempotent**: :func:`_state.state_db_nodes.register_comms_node`
  is an UPSERT keyed on ``name``. Re-running on every listen start
  bumps ``updated_at`` for existing rows; it never duplicates.
* **Best-effort**: every failure (port parse, DB error, conflict) is
  logged at ``warning`` and the loop continues. The listen MUST
  start even when persistence cannot — the previous bug class was
  "self-peer discovery wedges the API"; the inverse hazard
  ("persistence wedges the listen") is the one to avoid here.
* **No fallback that silently drops data**: a non-parseable port
  triggers a loud ``log.warning`` and a row skip. We do not invent a
  default port — that would re-introduce the ``port=0`` production
  bug :mod:`_channel_self_register` was created to close.
* **source_host = None**: same convention the channel-side helper
  uses for locally-registered rows. The listen is the authoritative
  reporter for the rows it discovers under its own filesystem walk.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

log = logging.getLogger(__name__)


def _parse_listen_port(listen_url: str) -> int | None:
    """Extract the int TCP port from a ``http[s]://host:port[/path]`` URL.

    Returns ``None`` for the production-bug signatures (port=0,
    portless URL, empty string, parse error). Mirrors
    :func:`_mcp._channel_self_register.parse_listen_port` — kept as a
    private duplicate to avoid pulling the MCP-channel subgraph into
    every listen startup just to read a port number.
    """
    if not listen_url:
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(listen_url)
    except (TypeError, ValueError):
        return None
    port = parsed.port
    if port is None or port <= 0:
        return None
    return int(port)


def _resolve_canonical_host() -> str | None:
    """Return the host this listen advertises in its ``comms_nodes`` rows.

    Same source the channel-side helper uses
    (:func:`_state.host_config.load().canonical_host`). On any error
    return ``None`` so the caller can log + skip rather than persist
    an empty / wrong host.
    """
    try:
        from .._state.host_config import load as load_host_config

        cfg = load_host_config()
        return cfg.canonical_host()
    except Exception:  # stx-allow: fallback (reason: a missing host_config must not block self-peer persistence — log + skip the whole batch is the correct fail-soft.)
        return None


def persist_discovered_self_peers(
    peers: Iterable[Mapping[str, object]],
    *,
    canonical_host: str | None = None,
    skip_names: frozenset[str] = frozenset(),
) -> int:
    """Best-effort UPSERT every discovered self-peer into ``comms_nodes``.

    Parameters
    ----------
    peers:
        The list :func:`_self_peers.discover_self_peers` returns.
        Each element is the dict shape
        ``{"name": str, "listen_url": str, ...}``; extra keys are
        ignored.

    canonical_host:
        Host string to record as the row's ``host`` column. ``None``
        means "ask :func:`_resolve_canonical_host`"; a failure there
        SKIPS the entire batch (loudly, with a single ``warning``) —
        persisting rows with an empty / wrong host is worse than
        persisting nothing, because cross-host A2A POSTs dial that
        column.
    skip_names:
        Peer names to skip (no UPSERT) — typically ``{self_identity}``
        so the running listen does NOT double-register its OWN row
        via this persistence path. The listen's runtime identity has
        a dedicated registration site
        (:mod:`cli_pkg.listen_cmds._register_self_comms_node`); going
        through both writes a row with the cwd-walk's resolved host
        instead of the listen-side host_config, which then causes
        cross-host forward refusals in single-host test environments
        (the regression CI run 27435959935 caught).

    Returns
    -------
    int
        The number of rows successfully written. A return of ``0`` is
        legitimate (no peers / host unresolved / every peer rejected)
        and the listen continues.

    This function NEVER raises. All failure modes log at ``warning``
    and continue.

    ``db_path`` is GONE from this signature (2026-08-28). It existed only to
    thread a SQLite file into ``register_comms_node``, and the ADR-0014
    directory is now the shared PostgreSQL store; there is no file to point
    at. Test isolation comes from ``SCITEX_STORE_DSN``.
    """
    host = canonical_host if canonical_host is not None else _resolve_canonical_host()
    if not host:
        log.warning(
            "self-peer persistence: host_config.canonical_host() unresolved; "
            "skipping the whole batch (advertising rows with a wrong host is "
            "worse than persisting nothing — cross-host A2A POSTs dial that "
            "column)"
        )
        return 0

    try:
        from .._state.state_db_nodes import (
            CommsNodeConflictError,
            register_comms_node,
        )
    except Exception as exc:  # stx-allow: fallback (reason: state.db helper import failure must not crash listen startup; log + skip persistence entirely.)
        log.warning(
            "self-peer persistence: cannot import register_comms_node "
            "(%r); skipping the whole batch",
            exc,
        )
        return 0

    written = 0
    for peer in peers:
        name = peer.get("name") if isinstance(peer, Mapping) else None
        listen_url = peer.get("listen_url") if isinstance(peer, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            log.warning(
                "self-peer persistence: skipping peer with no name: %r",
                peer,
            )
            continue
        if name in skip_names:
            # Running listen's own identity — already registered via the
            # listen-side ``_register_self_comms_node`` path with the
            # authoritative host. Persisting it again here would race
            # the canonical-host-vs-cwd-walk-host write and break
            # single-host POST flows that expect ``host`` to match
            # ``$SAC_HOST`` / host_config.canonical_host.
            continue
        if not isinstance(listen_url, str) or not listen_url.strip():
            log.warning(
                "self-peer persistence: skipping %r — no listen_url",
                name,
            )
            continue
        port = _parse_listen_port(listen_url)
        if port is None:
            log.warning(
                "self-peer persistence: skipping %r — could not parse a "
                "non-zero port from listen_url=%r (refusing to persist port=0, "
                "the production-bug signature the channel-side helper was "
                "created to close)",
                name,
                listen_url,
            )
            continue
        try:
            spec_path = peer.get("config") if isinstance(peer, Mapping) else None
            register_comms_node(
                name=name,
                host=host,
                a2a_port=port,
                source_host=None,  # locally-discovered
                # PR L1 (operator directive 12847) — discriminator for
                # the loud-collision error message. Q4 always writes
                # self-peer rows; the discovered spec file's path goes
                # in ``source_path`` so a collision visibly names WHICH
                # spec.yaml tried to register.
                kind="self-peer",
                source_path=str(spec_path) if spec_path else None,
            )
            written += 1
        except CommsNodeConflictError as exc:
            log.warning(
                "self-peer persistence: comms_nodes conflict for name=%r: %s",
                name,
                exc,
            )
        except Exception as exc:  # stx-allow: fallback (reason: never block listen startup on a single peer's persistence failure — log + continue.)
            log.warning(
                "self-peer persistence: failed for name=%r listen_url=%r: %r",
                name,
                listen_url,
                exc,
            )
    return written


async def persist_self_peers_on_listen_startup() -> int:
    """Q4 listen-startup hook: discover + persist in one call.

    Discovers self-peers via the same cwd-walk that the
    ``GET /agents`` handler uses
    (:func:`_self_peers.discover_self_peers`), then UPSERTs each into
    ``comms_nodes`` via :func:`persist_discovered_self_peers`. Wired
    through :func:`_listen.server.create_app`'s Starlette
    ``on_startup`` list.

    Best-effort across the board — every failure mode logs at
    ``warning`` and returns ``0`` so the listen bind proceeds. The
    federated graph degrades softly (in-memory ``GET /agents`` still
    surfaces self-peers) rather than the API failing to come up.
    """
    try:
        from ..config._resolve import _search_dirs
        from ._self_peers import discover_self_peers
        from .server import _resolve_runtime_self_identity

        primary, env_dirs, fleet_dirs = _search_dirs()
        search_dirs = [*env_dirs, primary, *fleet_dirs]
        self_identity = _resolve_runtime_self_identity()
        peers = discover_self_peers(search_dirs, self_identity=self_identity)
    except Exception as exc:  # stx-allow: fallback (reason: discovery-side crash must not block listen startup; degrade to in-memory /agents only.)
        log.warning(
            "self-peer persistence on_startup: discovery failed (%r); "
            "listen continues without persisted self-peers",
            exc,
        )
        return 0
    # Skip the running listen's OWN identity — that row is owned by
    # the listen-side ``_register_self_comms_node`` path with the
    # authoritative ``canonical_host``. Persisting it again here would
    # race the canonical-host-vs-cwd-walk-host write and break
    # single-host POST flows (regression CI run 27435959935).
    skip_names: frozenset[str] = (
        frozenset({self_identity}) if isinstance(self_identity, str) else frozenset()
    )
    written = persist_discovered_self_peers(peers, skip_names=skip_names)
    log.info(
        "self-peer persistence on_startup: persisted %d row(s) into comms_nodes",
        written,
    )
    return written


__all__ = [
    "persist_discovered_self_peers",
    "persist_self_peers_on_listen_startup",
]
