"""Process-wide singleton handle for the active ``TelegramBridge``.

The MCP server boots the bridge once (when env conditions are met) and
stores the instance here. The ``telegram_*`` MCP tools resolve it via
:func:`get_bridge` so they don't have to thread the object through every
call site.

Auth model: the bridge is initialised with a ``bridge_auth_token`` derived
from the lead's ``~/.scitex/lead/.env``'s ``LEAD_TELEGRAM_AUTH_TOKEN``.
Tools compare their caller's env-supplied token against it; mismatch ->
the tool returns ``{"error": "..."}`` instead of touching the bridge.
Subagents inherit a sanitised env without the auth token, so they cannot
satisfy this check even when they are co-located on the same host.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from ._bridge import TelegramBridge


@dataclass
class _BridgeHandle:
    bridge: TelegramBridge
    auth_token: str


_lock = threading.Lock()
_handle: Optional[_BridgeHandle] = None


def set_bridge(bridge: TelegramBridge, auth_token: str) -> None:
    """Register the active bridge + the token that gates outbound tools."""
    global _handle
    with _lock:
        _handle = _BridgeHandle(bridge=bridge, auth_token=auth_token)


def clear_bridge() -> None:
    """Drop the registered handle (used at MCP server shutdown + in tests)."""
    global _handle
    with _lock:
        _handle = None


def get_bridge() -> Optional[TelegramBridge]:
    """Return the active bridge, or None if uninitialised."""
    with _lock:
        return _handle.bridge if _handle is not None else None


def get_auth_token() -> Optional[str]:
    """Return the bridge's auth token (used by tools to gate calls)."""
    with _lock:
        return _handle.auth_token if _handle is not None else None


__all__ = [
    "set_bridge",
    "clear_bridge",
    "get_bridge",
    "get_auth_token",
]
