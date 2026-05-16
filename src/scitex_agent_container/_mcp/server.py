"""MCP server entry-point for scitex-agent-container (F-CS15).

Tool definitions live under :mod:`._tools`. This module is a thin
shim: FastMCP init, tool registration, transport selection.

It also drives the Phase 2+3 Telegram fold boot-up: when the env carries
the lead's ``LEAD_TELEGRAM_AUTH_TOKEN`` and a ``TelegramSpec`` is
resolvable, :func:`_maybe_boot_telegram_bridge` instantiates a
:class:`TelegramBridge`, registers it in the shared runtime singleton so
the in-process ``telegram_*`` tools can find it, and (when running in an
async context) schedules its long-poll task on the current loop. The
boot is best-effort — every failure mode logs a WARN and proceeds, so a
mis-configured Telegram fold cannot break the rest of the MCP server.

Usage::

    sac mcp start                  # stdio
    sac mcp start --http --port 8970
    fastmcp run scitex_agent_container._mcp.server:mcp
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ._tools import register_all_tools

log = logging.getLogger(__name__)

_INSTRUCTIONS = """\
scitex-agent-container (sac) — declarative container wrapper for
agents. Tools mirror the `sac` CLI surface: `agent_*` for lifecycle
(list / status / start / stop / logs), `db_*` for SQLite state queries,
`host_*` for multi-host topology, `image_*` for container image
build, `template_*` for spec rendering, plus `account_*`, `skills_*`,
`mcp_*` and a few introspection helpers. Tool names mirror the CLI
verb-noun shape (e.g. `sac_agent_list`).
"""


def _build_server():
    """Construct + register the FastMCP server. Lazy-imported so that
    `import scitex_agent_container._mcp.server` succeeds even when
    `fastmcp` isn't installed (the CLI's `mcp doctor` then prints an
    actionable message instead of crashing on import)."""
    try:
        from fastmcp import FastMCP
    except Exception as exc:
        # Broad catch: fastmcp pulls in heavy transitive deps that can
        # raise non-ImportError errors at import-time on misconfigured
        # environments (e.g. version-mismatched mcp / pydantic). Surface
        # any failure as an actionable ImportError instead of crashing.
        raise ImportError(
            "fastmcp is required for the sac MCP server — "
            "install with `pip install scitex-agent-container[mcp]`"
        ) from exc

    server = FastMCP(name="scitex-agent-container", instructions=_INSTRUCTIONS)
    register_all_tools(server)
    _maybe_boot_telegram_bridge(server)
    return server


def _resolve_telegram_spec():
    """Look up ``TelegramSpec`` from the env-pinned agent name, if any.

    Returns ``None`` (with a debug log) whenever the spec cannot be
    located — the lead's launcher may inject the bot-token env without a
    spec file, in which case we still want the bridge to boot from
    defaults.
    """
    import os

    from ..config._types import TelegramSpec

    agent_name = os.environ.get("SAC_AGENT_NAME") or os.environ.get(
        "SCITEX_AGENT_CONTAINER_AGENT_NAME"
    )
    if not agent_name:
        return TelegramSpec()
    try:
        from ..config import load_config
        from ..config._resolve import resolve_config

        cfg = load_config(resolve_config(agent_name))
    except Exception as exc:  # stx-allow: fallback (reason: bridge boot must not depend on agent spec resolution)
        log.debug(
            "telegram: could not load spec for agent %s (%s); using defaults",
            agent_name,
            exc,
        )
        return TelegramSpec()
    return getattr(cfg, "telegram", TelegramSpec())


async def _emit_channel_notification(session: Any, payload: dict) -> None:
    """Emit a ``notifications/claude/channel`` event over a live MCP
    session.

    The session is duck-typed: any object exposing
    :py:meth:`send_message` accepting a :class:`SessionMessage` works
    (the real implementation is :class:`mcp.server.session.ServerSession`).
    Failures are logged at WARN level so a transient send error never
    crashes the calling background task.
    """
    try:
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage, JSONRPCNotification
    except (
        Exception
    ) as exc:  # stx-allow: fallback (reason: optional dep; degrade gracefully)
        log.warning(
            "telegram: mcp types unavailable (%s); cannot emit channel notification",
            exc,
        )
        return
    msg = JSONRPCMessage(
        JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={
                "content": payload.get("content", ""),
                "meta": payload.get("meta", {}),
            },
        )
    )
    try:
        await session.send_message(SessionMessage(msg))
        log.info(
            "telegram -> claude channel push delivered (chat_id=%s)",
            payload.get("meta", {}).get("chat_id"),
        )
    except Exception as exc:  # stx-allow: fallback (reason: a transient send failure must not crash bridge)
        log.warning("telegram: channel send_message failed: %s", exc)


def _make_telegram_notifier():
    """Build the bridge notifier closure. Exposed for unit-testing the
    "session present" and "session absent" branches independently."""
    from .._telegram._session_holder import get_active_session

    async def _notifier(payload: dict) -> None:
        session = get_active_session()
        if session is None:
            log.info(
                "telegram inbound (no session yet): %s",
                {
                    "content": payload.get("content", "")[:80],
                    "meta": payload.get("meta"),
                },
            )
            return
        await _emit_channel_notification(session, payload)

    return _notifier


def _maybe_boot_telegram_bridge(server: Any) -> None:
    """Boot the Telegram bridge in-process when env conditions allow.

    Installs a ServerSession-capture patch so the bridge can emit
    ``notifications/claude/channel`` from its background poll task. If
    the patch can't be installed (mcp lib missing) the notifier falls
    back to a log-only mode so the rest of the MCP surface still works.
    """
    try:
        from .._telegram._session_holder import install
        from .._telegram._startup import maybe_start_bridge
    except Exception as exc:  # stx-allow: fallback (reason: telegram module is optional; never fail MCP boot)
        log.debug("telegram: bridge module unavailable (%s)", exc)
        return

    spec = _resolve_telegram_spec()
    install()
    bridge = maybe_start_bridge(
        spec, notifier=_make_telegram_notifier(), target_agent="master"
    )
    if bridge is None:
        return

    # Schedule the actual long-poll only when we already have an event
    # loop. The MCP stdio transport runs ``server.run()`` which spins
    # one up internally — at that point we hook via FastMCP lifespan if
    # available; otherwise we let the bridge sit idle (constructed +
    # registered) until the operator calls ``start()`` explicitly.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(bridge.start())
    except RuntimeError:  # stx-allow: fallback (reason: no running loop at construction time is the common case)
        log.info(
            "telegram: bridge constructed but not started (no event loop); "
            "the caller must `await bridge.start()` in its lifespan"
        )


# Module-level singleton — built lazily on first attribute access so
# the import never raises when fastmcp is absent (doctor / list-tools
# check for it gracefully).
mcp = None  # type: ignore[assignment]


def get_server():
    """Return the lazily-constructed FastMCP server instance."""
    global mcp
    if mcp is None:
        mcp = _build_server()
    return mcp


def run_server(
    transport: str = "stdio", host: str = "127.0.0.1", port: int = 8970
) -> None:
    """Launch the MCP server on the requested transport.

    ``transport`` is one of ``"stdio"`` (default) or ``"http"``. The
    HTTP variant binds to ``host:port`` (loopback by default — agents
    on the same host share the docker network for peer-to-peer calls).
    """
    server = get_server()
    if transport == "http":
        server.run(transport="http", host=host, port=port)
    else:
        server.run()


__all__ = ["get_server", "mcp", "run_server"]
