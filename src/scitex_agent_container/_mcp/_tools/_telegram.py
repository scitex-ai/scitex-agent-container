"""``telegram_*`` MCP tools — Phase 3 implementations.

Each tool is a thin wrapper that:

1. Verifies the caller's env carries ``LEAD_TELEGRAM_AUTH_TOKEN``
   matching the value the bridge was initialised with at lead-session
   startup. Subagents inherit a sanitised env without the token, so they
   always fail this check (returning a structured ``{"error": ...}``
   instead of pretending to send).
2. Resolves the in-process :class:`TelegramBridge` instance.
3. Delegates to the matching bridge method, returning its dict result
   verbatim.

Registration is gated by ``SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD``. Default
flipped to ON in Phase 3 (set to ``"0"`` to opt-out).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ..._telegram._runtime import get_auth_token, get_bridge

FEATURE_FLAG_ENV = "SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD"
LEAD_AUTH_TOKEN_ENV = "SCITEX_LEAD_TELEGRAM_AUTH_TOKEN"

# Error payloads — structured rather than raised so the MCP tool's caller
# always receives a JSON-RPC ``result``. Raising would surface as an
# ``error`` envelope which Claude renders as a tool failure.
_ERR_NO_BRIDGE = {"error": "telegram bridge is not initialised on this host"}
_ERR_AUTH = {
    "error": (
        "telegram tools are lead-only; LEAD_TELEGRAM_AUTH_TOKEN missing or mismatched"
    )
}


def _authorize() -> dict[str, Any] | None:
    """Return None when the caller is allowed; an error dict otherwise."""
    bridge_token = get_auth_token()
    caller_token = os.environ.get(LEAD_AUTH_TOKEN_ENV)
    if bridge_token is None:
        return _ERR_NO_BRIDGE
    if not caller_token or caller_token != bridge_token:
        return _ERR_AUTH
    return None


def _run(coro: Any) -> Any:
    """Run a coroutine to completion regardless of caller context.

    The bridge's aiohttp ClientSession is bound to the MCP server's
    main asyncio loop. FastMCP schedules sync tools onto a worker
    thread, so we cannot simply ``asyncio.run(coro)`` here — that would
    create a NEW loop, and aiohttp would refuse to reuse the session
    (returning None silently, which the tool wrapped as ``{}``).

    Instead, look up the bridge's owning loop and submit the coroutine
    onto it via ``run_coroutine_threadsafe``. The blocking ``.result()``
    waits for the coroutine to complete on the proper loop.
    """
    import concurrent.futures

    from ..._telegram._runtime import get_bridge

    # Try to find the bridge's owning loop. It was stored when the
    # bridge.start() opened the aiohttp session in the lifespan task.
    bridge = get_bridge()
    bridge_loop = None
    if bridge is not None:
        # The session was created on the loop that ran bridge.start().
        sess = getattr(bridge, "_session", None)
        if sess is not None:
            # aiohttp.ClientSession stashes its loop on `_loop`.
            bridge_loop = getattr(sess, "_loop", None)
    if bridge_loop is not None and bridge_loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro, bridge_loop)
        return fut.result(timeout=30)
    # No bridge loop available — fall back to fresh loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def telegram_send(
    chat_id: str,
    text: str,
    reply_to: int | None = None,
) -> dict[str, Any]:
    """Send a new Telegram message (or threaded reply via ``reply_to``)."""
    err = _authorize()
    if err is not None:
        return err
    bridge = get_bridge()
    assert bridge is not None  # _authorize guaranteed
    return _run(bridge.send_message(chat_id, text, reply_to=reply_to))


def telegram_reply(
    chat_id: str,
    text: str,
    row_id: int | None = None,
    reply_to: int | None = None,
    mark_read: bool = True,
) -> dict[str, Any]:
    """Reply to an inbound Telegram message (telegrammer-shaped).

    ``row_id`` + ``mark_read`` carry over from the standalone telegrammer
    surface. The sac bridge does not own a SQLite persistence layer, so
    ``row_id`` is accepted but unused (kept for shape compatibility with
    callers that target both surfaces) and ``mark_read`` is a no-op.
    """
    err = _authorize()
    if err is not None:
        return err
    bridge = get_bridge()
    assert bridge is not None
    return _run(bridge.send_message(chat_id, text, reply_to=reply_to))


def telegram_react(
    chat_id: str,
    message_id: int,
    emoji: str,
) -> dict[str, Any]:
    """Set an emoji reaction on a Telegram message."""
    err = _authorize()
    if err is not None:
        return err
    bridge = get_bridge()
    assert bridge is not None
    return _run(bridge.react(chat_id, message_id, emoji))


def telegram_edit_message(
    chat_id: str,
    message_id: int,
    text: str,
) -> dict[str, Any]:
    """Edit a prior bot message."""
    err = _authorize()
    if err is not None:
        return err
    bridge = get_bridge()
    assert bridge is not None
    return _run(bridge.edit_message(chat_id, message_id, text))


def telegram_download_attachment(
    file_id: str,
    dest_dir: str | None = None,
) -> dict[str, Any]:
    """Resolve a Telegram file_id, download the bytes, return local path."""
    err = _authorize()
    if err is not None:
        return err
    bridge = get_bridge()
    assert bridge is not None
    return _run(bridge.download_attachment(file_id, dest_dir))


def telegram_send_document(
    chat_id: str,
    path: str,
    caption: str | None = None,
) -> dict[str, Any]:
    """Upload a local file as a Telegram document."""
    err = _authorize()
    if err is not None:
        return err
    bridge = get_bridge()
    assert bridge is not None
    return _run(bridge.send_document(chat_id, path, caption=caption))


_TELEGRAM_TOOLS = (
    telegram_send,
    telegram_reply,
    telegram_react,
    telegram_edit_message,
    telegram_download_attachment,
    telegram_send_document,
)


def _feature_flag_enabled() -> bool:
    """Phase 3 flip: default ON. Set ``=0`` to opt out."""
    val = os.getenv(FEATURE_FLAG_ENV)
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "off", "no")


def register_telegram_tools(mcp) -> None:
    """Register telegram_* MCP tools when the feature flag is on (default)."""
    if not _feature_flag_enabled():
        return
    for fn in _TELEGRAM_TOOLS:
        mcp.tool()(fn)


__all__ = [
    "telegram_send",
    "telegram_reply",
    "telegram_react",
    "telegram_edit_message",
    "telegram_download_attachment",
    "telegram_send_document",
    "register_telegram_tools",
    "FEATURE_FLAG_ENV",
    "LEAD_AUTH_TOKEN_ENV",
]
