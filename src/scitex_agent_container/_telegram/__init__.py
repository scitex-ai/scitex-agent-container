"""sac Telegram bridge package.

Phase 2+3 complete: the bridge ports the orochi implementation onto sac's
channel-notification surface, the singleton lock prevents the dual-poller
409 trap, and the six ``telegram_*`` MCP tools speak directly to the
in-process bridge.

See ``docs/design/telegram-fold.md`` for the full plan.
"""

from __future__ import annotations

from ._bridge import ChannelNotifier, TelegramBridge
from ._lock import TelegramBridgeLock, TelegramLockError
from ._runtime import (
    clear_bridge,
    get_auth_token,
    get_bridge,
    set_bridge,
)
from ._startup import maybe_start_bridge

__all__ = [
    "TelegramBridge",
    "TelegramBridgeLock",
    "TelegramLockError",
    "ChannelNotifier",
    "set_bridge",
    "clear_bridge",
    "get_bridge",
    "get_auth_token",
    "maybe_start_bridge",
]
