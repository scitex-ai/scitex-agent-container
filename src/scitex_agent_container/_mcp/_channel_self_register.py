"""Self-registration of ``sac mcp channel`` nodes into ``comms_nodes``.

ADR-0014 + production lead-row bug (2026-06-01 → 2026-06-03):

The ``lead`` row in the production ``comms_nodes`` table had
``a2a_port=0`` and ``updated_at == registered_at`` (never refreshed
since first registration). Real agent rows have live 19xxx ports and
recent ``updated_at`` because their startup writes ``comms_nodes``
via ``_lifecycle/_instances.record_local_instance``. The lead does NOT
go through that path — it runs

    sac mcp channel --name lead --listen-url http://127.0.0.1:7878

which until now did ZERO ``comms_nodes`` interaction. Consequence: the
lead was not in ``sac a2a peers``, was not endpoint-discoverable, and
agent→lead delivery worked ONLY while the channel's push subscription
was live. On reboot it silently dropped from the federated graph.

This module fills the gap. It is the channel-side counterpart of
``cli_pkg/listen_cmds._register_self_comms_node`` (listen-side):

  * ``parse_listen_port(url)`` — extract the int port from the
    ``--listen-url`` string the channel was launched with. Returns
    ``None`` for the production-bug signature (port=0, portless URL,
    empty string) so the caller can refuse to write a row rather than
    re-introduce the silent port=0 footgun.

  * ``register_self_node(...)`` — best-effort idempotent UPSERT.
    Hits ``register_comms_node`` (the same helper the listen side
    uses) so any future schema/ACL tightening at the storage layer is
    enforced uniformly across both registration sites.

  * ``refresh_node(...)`` — long-lived async loop that re-UPSERTs
    every ``interval_s`` seconds, matching the agent runner's
    ``DEFAULT_TICK_SECONDS = 10.0`` heartbeat so a single staleness
    threshold applies to both sources.

Best-effort everywhere: a registry-write failure must never block the
channel's MCP handshake or its SSE consumption. The existing channel
codebase swallows side-effect failures with ``log.warning``; this
module follows the same convention.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Refresh cadence matches the agent runner's heartbeat tick
# (``_runners/_session_state.DEFAULT_TICK_SECONDS = 10.0``). Aligning
# means a single "row is stale" threshold can apply to both sources
# — agent runner heartbeats and lead/channel heartbeats — without
# downstream consumers having to special-case the channel side.
DEFAULT_REFRESH_INTERVAL_S = 10.0


def parse_listen_port(listen_url: str) -> int | None:
    """Return the TCP port from a sac listen URL, or ``None``.

    Accepts the canonical ``http[s]://host:port[/path]`` form that
    ``sac mcp channel --listen-url`` consumes. Returns the ``int``
    port on success; returns ``None`` when the URL is empty,
    malformed, missing a port, or carries an explicit port of ``0``.

    The ``None`` return is loud — :func:`register_self_node` translates
    it into "skip the write entirely" rather than persist a 0 port.
    That zero-port write was the EXACT production-bug signature this
    fix is targeting; refusing to round-trip it through the helper
    closes the regression door at the deepest layer.
    """
    if not listen_url:
        return None
    try:
        parsed = urlparse(listen_url)
    except (TypeError, ValueError):
        return None
    port = parsed.port
    if port is None or port <= 0:
        return None
    return int(port)


def register_self_node(
    *,
    name: str,
    listen_url: str,
) -> bool:
    """Best-effort UPSERT this channel's ``comms_nodes`` row.

    ``name`` is the channel's ``--name`` (``"lead"`` for the lead,
    arbitrary string for any other ``sac mcp channel`` node). The
    host is resolved via ``host_config.canonical_host()`` so a row's
    advertised host matches the rest of the host's ``comms_nodes``
    entries (which is what cross-host A2A POSTs dial). The port comes
    from :func:`parse_listen_port` against ``listen_url``.

    Returns ``True`` iff a row was written (or successfully re-UPSERTed)
    and ``False`` for any reason no row was committed (empty inputs,
    portless URL, conflict, DB error). NEVER raises — registry-write
    failures must not block the channel's MCP handshake or its SSE
    consumption.

    Logging policy: every refusal logs a ``warning`` so a future
    operator can tell WHY a row didn't land. The original bug was
    silent — no log, no row, hence the five-day undetected drift.
    """
    if not name:
        log.warning("channel self-register: name is empty; skipping")
        return False
    port = parse_listen_port(listen_url)
    if port is None:
        log.warning(
            "channel self-register: could not parse a non-zero port from "
            "listen_url=%r; refusing to write (would have persisted port=0, "
            "the EXACT production-bug signature this guard targets)",
            listen_url,
        )
        return False
    try:
        from .._state.host_config import load as load_host_config
        from .._state.state_db_nodes import (
            CommsNodeConflictError,
            register_comms_node,
        )

        cfg = load_host_config()
        host = cfg.canonical_host()
        try:
            register_comms_node(
                name=name,
                host=host,
                a2a_port=port,
                source_host=None,  # locally-registered
                # No ``db_path``: comms_nodes is a PostgreSQL store now.
                # PR L1 (operator directive 12847) — discriminator
                # for the loud-collision error message. The channel
                # is a self-peer registration source by definition.
                # ``source_path`` carries the listen_url so a
                # collision error visibly names the URL the channel
                # tried to bind.
                kind="self-peer",
                source_path=listen_url,
            )
            return True
        except CommsNodeConflictError as exc:
            log.warning(
                "channel self-register: comms_nodes conflict for name=%r: %s",
                name,
                exc,
            )
            return False
    except Exception as exc:  # stx-allow: fallback (reason: never block channel start on registry write — the channel must keep MCP + SSE working even if state.db is briefly unreachable)
        log.warning(
            "channel self-register: failed for name=%r listen_url=%r: %r",
            name,
            listen_url,
            exc,
        )
        return False


async def refresh_node(
    *,
    name: str,
    listen_url: str,
    interval_s: float = DEFAULT_REFRESH_INTERVAL_S,
) -> None:
    """Periodically re-UPSERT to keep ``updated_at`` fresh.

    Long-lived task — runs until cancelled. Each iteration shares the
    semantics of :func:`register_self_node` (best-effort, never raises),
    so a transient ``state.db`` outage triggers one logged warning but
    does NOT kill the heartbeat. The next interval retries.

    The cancellation contract: ``asyncio.CancelledError`` propagates
    out so the channel's shutdown path can tear the task down cleanly
    in its ``finally`` block (alongside the SSE consumer task). No
    orphan tasks survive the channel exit.

    The first iteration runs IMMEDIATELY (no leading sleep) so a
    caller can drop ``refresh_node`` in place of a separate
    ``register_self_node()`` + loop pair. Saves one bug class
    ("oh, we started the loop but forgot the initial register").
    """
    while True:
        register_self_node(name=name, listen_url=listen_url)
        await asyncio.sleep(interval_s)


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_S",
    "parse_listen_port",
    "refresh_node",
    "register_self_node",
]
