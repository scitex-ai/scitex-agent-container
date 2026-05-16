"""End-to-end shape test for the Telegram -> channel-notification path.

The bridge's ``_process_update`` is the one place we mint the payload that
will become a ``notifications/claude/channel`` push. This test pins the
shape so a future refactor can't silently drop or rename a meta field.

The launcher must invoke Claude Code with
``--dangerously-load-development-channels server:scitex-agent-container``
for the receiver to render this. The bridge logs that requirement at
startup; here we just verify the payload's structure.
"""

from __future__ import annotations

import asyncio
from typing import Any

from scitex_agent_container._telegram import TelegramBridge


def _build_bridge_with_sink() -> tuple[TelegramBridge, list[dict[str, Any]]]:
    bridge = TelegramBridge(bot_token="t", allowed_users=["1"], target_agent="master")
    sink: list[dict[str, Any]] = []

    async def notifier(payload: dict[str, Any]) -> None:
        sink.append(payload)

    bridge._notifier = notifier  # type: ignore[assignment]
    return bridge, sink


def _golden_update() -> dict[str, Any]:
    return {
        "update_id": 99,
        "message": {
            "message_id": 444,
            "date": 1700000000,
            "from": {"id": 1, "username": "yu", "first_name": "Yu"},
            "chat": {"id": -10042},
            "text": "ping",
        },
    }


def test_payload_top_level_keys_match_channel_notification_shape() -> None:
    # Arrange
    bridge, sink = _build_bridge_with_sink()

    # Act
    asyncio.run(bridge._process_update(_golden_update()))

    # Assert
    assert set(sink[0].keys()) == {"content", "meta"}


def test_payload_content_is_message_text() -> None:
    # Arrange
    bridge, sink = _build_bridge_with_sink()

    # Act
    asyncio.run(bridge._process_update(_golden_update()))

    # Assert
    assert sink[0]["content"] == "ping"


def test_meta_source_is_telegram() -> None:
    # Arrange
    bridge, sink = _build_bridge_with_sink()

    # Act
    asyncio.run(bridge._process_update(_golden_update()))

    # Assert
    assert sink[0]["meta"]["source"] == "telegram"


def test_meta_required_fields_present() -> None:
    # Arrange
    bridge, sink = _build_bridge_with_sink()
    required = {
        "source",
        "chat_id",
        "message_id",
        "user_id",
        "username",
        "display_name",
        "ts",
    }

    # Act
    asyncio.run(bridge._process_update(_golden_update()))

    # Assert
    assert required.issubset(set(sink[0]["meta"].keys()))


def test_meta_ts_is_string() -> None:
    # Arrange
    bridge, sink = _build_bridge_with_sink()

    # Act
    asyncio.run(bridge._process_update(_golden_update()))

    # Assert
    assert isinstance(sink[0]["meta"]["ts"], str)
