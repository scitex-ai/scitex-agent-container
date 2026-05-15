"""sac TelegramBridge skeleton — Phase 1 scaffolding.

This file is intentionally a skeleton. Every public method raises
``NotImplementedError`` with a message pointing at the Phase 2 port target.

Phase 2 will port the implementation from::

    /home/ywatanabe/proj/scitex-orochi/src/scitex_orochi/_telegram_bridge.py

The orochi bridge owns the Telegram bot token, long-polls Telegram, posts
inbound messages onto an Orochi channel, and relays outbound channel
messages back to Telegram. The sac port replaces "Orochi channel" with
"sac per-agent broker on the SSE inbox bus" (see
``scitex_agent_container.a2a._inbox_bus``).

Singleton model: only one process may long-poll a given bot token (Telegram
returns 409 Conflict otherwise). The bridge takes an exclusive flock on
``${state_dir}/telegram-bridge.lock`` and recovers stale locks by checking
the recorded PID with ``kill(pid, 0)``.

See ``docs/design/telegram-fold.md`` for the full plan.
"""

from __future__ import annotations

from typing import Any

_PHASE2_PORT_TARGET = (
    "Phase 2: port from "
    "/home/ywatanabe/proj/scitex-orochi/src/scitex_orochi/_telegram_bridge.py"
)


class TelegramBridge:
    """Bi-directional relay between Telegram and sac's inbox bus.

    Public API mirrors orochi's ``TelegramBridge`` (see module docstring for
    the Phase 2 port target). All methods raise ``NotImplementedError`` in
    Phase 1 — they exist only to lock the import surface so that downstream
    code (MCP tool stubs, tests, examples) can reference the final shape.

    Constructor signature is provisional and may evolve in Phase 2.
    """

    def __init__(
        self,
        bot_token: str,
        *,
        allowed_users: list[str] | None = None,
        target_agent: str = "master",
        poll_timeout: int = 30,
        webhook_url: str = "",
        state_dir: str | None = None,
    ) -> None:
        # Phase 2: store config, prepare aiohttp session lazily, set up lock
        # file path. For Phase 1 we record the args so tests can introspect.
        self._token = bot_token
        self._allowed_users = list(allowed_users or [])
        self._target_agent = target_agent
        self._poll_timeout = poll_timeout
        self._webhook_url = webhook_url
        self._state_dir = state_dir
        self._running = False

    # -- lifecycle --------------------------------------------------------

    @property
    def webhook_mode(self) -> bool:
        """True when running in webhook mode (no polling)."""
        return bool(self._webhook_url)

    def connect(self) -> None:
        """Acquire the singleton lock, start polling or register webhook.

        Phase 2 implements. Sync wrapper around the async ``start`` coroutine
        so callers from synchronous sac contexts can use it directly.
        """
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    async def start(self) -> None:
        """Async: open aiohttp session, ``getMe``, flush stale offsets,
        spawn poll task (or set webhook). Phase 2 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    async def stop(self) -> None:
        """Async: cancel poll task, deleteWebhook if applicable, close session,
        release lock. Phase 2 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    def disconnect(self) -> None:
        """Sync wrapper for ``stop``. Phase 2 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    # -- inbound: Telegram -> sac broker ---------------------------------

    async def handle_webhook_update(self, data: dict[str, Any]) -> None:
        """Process a single Telegram Update received via webhook POST.
        Phase 2 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    async def _process_update(self, update: dict[str, Any]) -> None:
        """Convert a Telegram update into a sac A2A SendMessage on the
        target agent's broker with ``metadata.source="telegram"``. Phase 2
        implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    # -- outbound: sac -> Telegram ---------------------------------------

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a Telegram message. Backs ``telegram_send`` /
        ``telegram_reply`` MCP tools. Phase 3 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    async def send_document(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file as a Telegram document. Phase 3 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    async def download_attachment(
        self,
        file_id: str,
        dest_dir: str | None = None,
    ) -> dict[str, Any]:
        """Download a Telegram file by file_id. Phase 3 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    async def react(
        self,
        chat_id: str,
        message_id: int,
        emoji: str,
    ) -> dict[str, Any]:
        """Set an emoji reaction on a Telegram message. Phase 3 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
    ) -> dict[str, Any]:
        """Edit a prior bot message. Phase 3 implements."""
        raise NotImplementedError(_PHASE2_PORT_TARGET)


__all__ = ["TelegramBridge"]
