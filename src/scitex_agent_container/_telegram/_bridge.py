"""sac TelegramBridge — long-poll Telegram, relay to channel notifications.

Ported from ``scitex-orochi/_telegram_bridge.py``, restructured to:

* run inside the sac MCP server process (or any host that can emit MCP
  ``notifications/claude/channel`` to the running Claude session),
* publish inbound messages via an injected ``ChannelNotifier`` callable
  rather than the orochi message bus,
* enforce a ``TelegramSpec.allowed_users`` allowlist on the inbound path,
* hold a flock singleton against the bot token (stale-PID recovery built
  into ``_lock.py``).

The bridge owns the bot token exclusively per the Telegram API contract
(``getUpdates`` returns 409 Conflict if a second long-poller exists). For
the sac fleet, the lead's session is the singleton; subagents reach
Telegram only via the ``telegram_*`` MCP tools, which proxy through this
bridge in-process.

Outbound methods (``send_message``, ``send_document``, etc.) are the
backing implementation for the six ``telegram_*`` MCP tools.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ._lock import TelegramBridgeLock, TelegramLockError

log = logging.getLogger("scitex_agent_container.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Channel-notifier signature: takes a message dict shaped as
# ``{"content": str, "meta": {...}}`` and emits a Claude Code channel push.
# The MCP server wires this to ``session.send_notification(...)``; tests
# substitute a list-appender to capture emissions.
ChannelNotifier = Callable[[dict[str, Any]], Awaitable[None]]


async def _default_notifier(_payload: dict[str, Any]) -> None:
    """No-op default — when nobody wired up an emitter we don't crash.

    This path is hit when the bridge is constructed standalone (e.g. for
    outbound-only smoke tests). The caller almost always overrides via
    the constructor.
    """
    return None


class TelegramBridge:
    """Bi-directional relay between Telegram and Claude's channel surface.

    Construction is cheap (no I/O). Real work begins in ``start()``:

    1. acquire the per-token flock (``TelegramBridgeLock``),
    2. open an ``aiohttp`` session,
    3. ``getMe`` to validate the token + populate the bot username,
    4. flush stale long-poll state at Telegram's side
       (``deleteWebhook`` + ``getUpdates(offset=-1)``),
    5. spawn the background poll task.

    ``stop()`` reverses each step, releasing the lock last.
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
        notifier: ChannelNotifier | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token must be a non-empty string")
        self._token = bot_token
        self._allowed_users = [str(u) for u in (allowed_users or [])]
        self._target_agent = target_agent
        self._poll_timeout = poll_timeout
        self._webhook_url = webhook_url
        self._state_dir = state_dir
        self._notifier: ChannelNotifier = notifier or _default_notifier

        self._offset: int = 0
        # aiohttp pulled lazily so import-time cost stays low; tests can
        # stub the entire ``_api`` method without ever resolving the dep.
        self._session: Any | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._running = False
        self._bot_name: str = "unknown"
        self._lock = TelegramBridgeLock(bot_token)
        self._signature = (
            f"{getpass.getuser()}@{socket.gethostname()} (PID={os.getpid()})"
        )

    # -- introspection --------------------------------------------------

    @property
    def webhook_mode(self) -> bool:
        return bool(self._webhook_url)

    @property
    def allowed_users(self) -> list[str]:
        return list(self._allowed_users)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def bot_name(self) -> str:
        return self._bot_name

    def _is_user_allowed(self, user_id: str | int | None) -> bool:
        """Apply the allowlist.

        Empty allowlist = fail-closed (nobody allowed). Matches the design
        doc: ``allowed_users=[]`` means no Telegram inbound flows through.
        """
        if not self._allowed_users:
            return False
        return str(user_id) in self._allowed_users

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        """Sync wrapper for callers outside an event loop."""
        asyncio.run(self.start())

    def disconnect(self) -> None:
        """Sync wrapper for callers outside an event loop."""
        asyncio.run(self.stop())

    async def start(self) -> None:
        """Acquire the lock + open the API session + spawn poll task.

        Idempotent: a second call while already running is a no-op (the
        sac MCP server might re-enter this on hot-reload).
        """
        if self._running:
            return
        # 1. Lock acquisition. Bubbles ``TelegramLockError`` to the caller
        #    if another live PID owns the bot token already.
        self._lock.acquire()
        try:
            import aiohttp  # local import — keep the bridge importable on
            # systems without aiohttp until ``start`` is called.
        except ImportError as exc:
            self._lock.release()
            raise ImportError(
                "aiohttp is required for the Telegram bridge — `pip install aiohttp`"
            ) from exc

        self._session = aiohttp.ClientSession()
        self._running = True

        try:
            me = await self._api("getMe")
            self._bot_name = (me or {}).get("username", "unknown")

            if self._webhook_url:
                hook_endpoint = self._webhook_url.rstrip("/") + "/webhook/telegram"
                await self._api(
                    "setWebhook",
                    {"url": hook_endpoint, "allowed_updates": ["message"]},
                )
                log.info(
                    "telegram bridge active (webhook): @%s -> %s",
                    self._bot_name,
                    hook_endpoint,
                )
            else:
                # Force-flush stale connections at Telegram's side
                await self._api("deleteWebhook", {"drop_pending_updates": False})
                await self._api("getUpdates", {"timeout": 0, "offset": -1})
                # Brief grace for Telegram to release the previous slot
                await asyncio.sleep(2)
                self._poll_task = asyncio.create_task(self._poll_loop())
                log.info(
                    "telegram bridge active (polling): @%s -> agent %s",
                    self._bot_name,
                    self._target_agent,
                )
        except Exception:
            # Roll back partial init so we don't leak the lock + session.
            await self._cleanup_session()
            self._running = False
            self._lock.release()
            raise

    async def _cleanup_session(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (
                asyncio.CancelledError,
                Exception,
            ):  # pragma: no cover  # stx-allow: fallback (reason: shutdown must continue past poll-task error)
                pass
            self._poll_task = None
        if self._session is not None and not getattr(self._session, "closed", True):
            try:
                await self._session.close()
            except Exception:  # pragma: no cover  # stx-allow: fallback (reason: shutdown must continue past close error)
                pass
        self._session = None

    async def stop(self) -> None:
        """Gracefully shut down polling/webhook + close session, then unlock."""
        if not self._running and self._poll_task is None and self._session is None:
            self._lock.release()
            return
        self._running = False
        if self._webhook_url and self._session is not None:
            try:
                await self._api("deleteWebhook", {"drop_pending_updates": False})
            except Exception:  # pragma: no cover  # stx-allow: fallback (reason: shutdown must continue past API error)
                pass
        await self._cleanup_session()
        self._lock.release()
        log.info("telegram bridge stopped")

    # -- inbound: Telegram -> Claude channel notification ---------------

    async def handle_webhook_update(self, data: dict[str, Any]) -> None:
        """Webhook entry point — same shape as a poll-returned Update."""
        await self._process_update(data)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await self._api(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": self._poll_timeout,
                        "allowed_updates": ["message"],
                    },
                )
                if not updates:
                    await asyncio.sleep(3)
                    continue
                for update in updates:
                    self._offset = update["update_id"] + 1
                    await self._process_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram poll error, retrying in 5s")
                await asyncio.sleep(5)

    async def _process_update(self, update: dict[str, Any]) -> None:
        """Convert a Telegram update into a channel notification.

        Drops the update silently when the sender isn't on the allowlist.
        Channel notifications are emitted via the injected notifier.
        """
        tg_msg = update.get("message")
        if not tg_msg:
            return

        user = tg_msg.get("from") or {}
        user_id = user.get("id")
        if not self._is_user_allowed(user_id):
            log.warning(
                "telegram: dropping message from disallowed user_id=%s (username=%s)",
                user_id,
                user.get("username", ""),
            )
            return

        chat = tg_msg.get("chat") or {}
        chat_id = chat.get("id")
        message_id = tg_msg.get("message_id")
        username = user.get("username") or ""
        display = user.get("first_name") or username or "telegram-user"

        content = tg_msg.get("text") or tg_msg.get("caption") or ""

        attachments: list[dict[str, Any]] = []
        photos = tg_msg.get("photo")
        if photos:
            best = max(photos, key=lambda p: p.get("file_size", 0))
            fid = best.get("file_id", "")
            if fid:
                attachments.append({"type": "photo", "file_id": fid})
        doc = tg_msg.get("document")
        if doc:
            attachments.append(
                {
                    "type": "document",
                    "file_id": doc.get("file_id", ""),
                    "filename": doc.get("file_name", "file"),
                }
            )
        voice = tg_msg.get("voice")
        if voice:
            attachments.append(
                {
                    "type": "voice",
                    "file_id": voice.get("file_id", ""),
                    "duration": voice.get("duration", 0),
                }
            )

        if not content and not attachments:
            return

        meta: dict[str, Any] = {
            "source": "telegram",
            "chat_id": str(chat_id) if chat_id is not None else "",
            "message_id": str(message_id) if message_id is not None else "",
            "user_id": str(user_id) if user_id is not None else "",
            "username": username,
            "display_name": display,
            "ts": str(tg_msg.get("date") or int(time.time())),
        }
        if attachments:
            meta["attachments"] = attachments

        payload = {"content": content, "meta": meta}
        log.info(
            "[telegram->channel] %s (msg_id=%s): %s",
            display,
            message_id,
            content[:80],
        )
        await self._notifier(payload)

    # -- outbound: tool surface -----------------------------------------

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        return await self._api("sendMessage", params) or {}

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
    ) -> dict[str, Any]:
        return (
            await self._api(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": text},
            )
            or {}
        )

    async def react(
        self,
        chat_id: str,
        message_id: int,
        emoji: str,
    ) -> dict[str, Any]:
        return (
            await self._api(
                "setMessageReaction",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
            )
            or {}
        )

    async def download_attachment(
        self,
        file_id: str,
        dest_dir: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a file_id, fetch the bytes, write to ``dest_dir``.

        Returns ``{"path": "<local>", "size": <bytes>}`` on success or
        ``{"error": "..."}`` on failure.
        """
        info = await self._api("getFile", {"file_id": file_id})
        if not info or not info.get("file_path"):
            return {"error": f"could not resolve file_id={file_id}"}
        remote_path = info["file_path"]
        url = f"https://api.telegram.org/file/bot{self._token}/{remote_path}"
        target_dir = (
            Path(dest_dir)
            if dest_dir
            else Path.home()
            / ".scitex"
            / "agent-container"
            / "runtime"
            / "telegram"
            / "downloads"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        local = target_dir / Path(remote_path).name
        if self._session is None:
            return {"error": "telegram bridge not started"}
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return {"error": f"http {resp.status} fetching attachment"}
            data = await resp.read()
        local.write_bytes(data)
        return {"path": str(local), "size": len(data)}

    async def send_document(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file as a Telegram document.

        Uses multipart/form-data via aiohttp's ``FormData``.
        """
        if self._session is None:
            return {"error": "telegram bridge not started"}
        local = Path(path)
        if not local.is_file():
            return {"error": f"no such file: {path}"}
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover
            return {"error": f"aiohttp missing: {exc}"}
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption)
        form.add_field(
            "document",
            local.open("rb"),
            filename=local.name,
            content_type="application/octet-stream",
        )
        url = TELEGRAM_API.format(token=self._token, method="sendDocument")
        try:
            async with self._session.post(url, data=form) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    return {"error": data.get("description", "send failed")}
                return data.get("result") or {}
        except Exception as exc:  # stx-allow: fallback (reason: surface network errors as structured response)
            return {"error": str(exc)}

    # -- Telegram Bot API helper ----------------------------------------

    async def _api(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """POST ``params`` to ``method`` on the Bot API; return ``result``."""
        if self._session is None:
            return None
        try:
            import aiohttp
        except ImportError:  # pragma: no cover
            return None
        url = TELEGRAM_API.format(token=self._token, method=method)
        try:
            timeout = aiohttp.ClientTimeout(total=self._poll_timeout + 10)
            async with self._session.post(
                url, json=params or {}, timeout=timeout
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    desc = data.get("description", "")
                    log.error("telegram %s failed: %s", method, desc)
                    if data.get("error_code") == 409 and method == "getUpdates":
                        log.info(
                            "telegram 409 Conflict on getUpdates — waiting %ds",
                            self._poll_timeout,
                        )
                        await asyncio.sleep(self._poll_timeout)
                    return None
                return data.get("result")
        except asyncio.TimeoutError:
            log.warning("telegram %s timed out", method)
            return None
        except Exception:
            log.exception("telegram %s error", method)
            return None


__all__ = ["TelegramBridge", "ChannelNotifier", "TelegramLockError"]
