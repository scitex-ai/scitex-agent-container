"""``sac.telegram`` — Telegram transport verbs as bare names.

Same function objects as the flat ``sac._mcp._tools._telegram.telegram_*``
tools; this module is the noun-grouped re-export shim for ergonomic
access (``sac.telegram.send(...)`` reads like the CLI tree) and the
§6 Python-API parity check the scitex MCP auditor enforces.

Each verb shares the same auth / bridge-singleton wiring as the MCP
tool; calling from a subagent process still returns ``{"error": ...}``
because the bridge handle isn't installed there.
"""

from .._mcp._tools._telegram import (
    telegram_download_attachment as download_attachment,
)
from .._mcp._tools._telegram import (
    telegram_edit_message as edit_message,
)
from .._mcp._tools._telegram import (
    telegram_react as react,
)
from .._mcp._tools._telegram import (
    telegram_reply as reply,
)
from .._mcp._tools._telegram import (
    telegram_send as send,
)
from .._mcp._tools._telegram import (
    telegram_send_document as send_document,
)

__all__ = [
    "send",
    "reply",
    "react",
    "edit_message",
    "download_attachment",
    "send_document",
]
