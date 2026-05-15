"""``telegram_*`` MCP tools — Phase 1 stubs only.

These functions are the final tool surface for the Telegram fold (audit:
``/home/ywatanabe/proj/lead/GITIGNORED/dev/05_sac-mcp-telegram.md`` — Option
A). They will be wired to ``scitex_agent_container._telegram.TelegramBridge``
in Phase 3.

Phase 1: each function raises ``NotImplementedError`` with a message naming
the port target (``claude-code-telegrammer/ts/telegram-server.ts``).
Registration is feature-flagged off (``SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=1``
to opt-in) so this scaffolding does not change sac's user-visible behaviour.
"""

from __future__ import annotations

import os
from typing import Any

_STUB_MSG = "Phase 2: port from claude-code-telegrammer/ts/telegram-server.ts"

FEATURE_FLAG_ENV = "SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD"


def telegram_send(
    chat_id: str,
    text: str,
    reply_to: int | None = None,
) -> dict[str, Any]:
    """Send a new Telegram message (or threaded reply via ``reply_to``).

    Phase 3 implements. See ``docs/design/telegram-fold.md``.
    """
    raise NotImplementedError(_STUB_MSG)


def telegram_reply(
    chat_id: str,
    text: str,
    row_id: int | None = None,
    reply_to: int | None = None,
    mark_read: bool = True,
) -> dict[str, Any]:
    """Reply to an inbound Telegram message (telegrammer-shaped).

    Carries ``row_id`` + ``mark_read`` semantics from
    ``claude-code-telegrammer``. Phase 3 implements.
    """
    raise NotImplementedError(_STUB_MSG)


def telegram_react(
    chat_id: str,
    message_id: int,
    emoji: str,
) -> dict[str, Any]:
    """Set an emoji reaction on a Telegram message. Phase 3 implements."""
    raise NotImplementedError(_STUB_MSG)


def telegram_edit_message(
    chat_id: str,
    message_id: int,
    text: str,
) -> dict[str, Any]:
    """Edit a prior bot message. Phase 3 implements."""
    raise NotImplementedError(_STUB_MSG)


def telegram_download_attachment(
    file_id: str,
    dest_dir: str | None = None,
) -> dict[str, Any]:
    """Resolve a Telegram file_id, download the bytes, return local path.

    Phase 3 implements.
    """
    raise NotImplementedError(_STUB_MSG)


def telegram_send_document(
    chat_id: str,
    path: str,
    caption: str | None = None,
) -> dict[str, Any]:
    """Upload a local file as a Telegram document. Phase 3 implements."""
    raise NotImplementedError(_STUB_MSG)


_TELEGRAM_TOOLS = (
    telegram_send,
    telegram_reply,
    telegram_react,
    telegram_edit_message,
    telegram_download_attachment,
    telegram_send_document,
)


def register_telegram_tools(mcp) -> None:
    """Register telegram_* MCP tools.

    Phase 1: only registers when ``SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=1``
    so the scaffolding stays invisible to existing users. Phase 3 flips the
    default on once the transport is real.
    """
    if os.getenv(FEATURE_FLAG_ENV) != "1":
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
]
