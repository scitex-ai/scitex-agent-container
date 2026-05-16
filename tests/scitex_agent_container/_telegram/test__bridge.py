"""Behaviour tests for :class:`scitex_agent_container._telegram.TelegramBridge`.

Mocks the Telegram Bot API at the ``_api`` boundary so no real HTTP
traffic is generated.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from scitex_agent_container._telegram import TelegramBridge


class _FakeAPI:
    """Records calls and returns scripted responses keyed by method name."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses = responses or {}

    async def __call__(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        return self.responses.get(method)


def _make_bridge(**overrides: Any) -> TelegramBridge:
    defaults: dict[str, Any] = {
        "bot_token": "test-token",
        "allowed_users": ["123"],
        "target_agent": "master",
    }
    defaults.update(overrides)
    return TelegramBridge(**defaults)


def test_telegram_package_exposes_bridge_class() -> None:
    # Arrange
    import scitex_agent_container._telegram as pkg

    # Act
    cls = getattr(pkg, "TelegramBridge", None)

    # Assert
    assert cls is not None


def test_bridge_construction_defaults_to_polling_mode() -> None:
    # Arrange
    # (no setup beyond the helper)
    # Act
    bridge = _make_bridge()

    # Assert
    assert bridge.webhook_mode is False


def test_bridge_construction_is_not_running() -> None:
    # Arrange
    # (no setup beyond the helper)
    # Act
    bridge = _make_bridge()

    # Assert
    assert bridge.is_running is False


def test_bridge_construction_records_allowed_users() -> None:
    # Arrange
    # (no setup beyond the helper)
    # Act
    bridge = _make_bridge(allowed_users=["7", "9"])

    # Assert
    assert bridge.allowed_users == ["7", "9"]


def test_bridge_rejects_empty_bot_token() -> None:
    # Arrange
    args = {"bot_token": ""}

    # Act
    # (raising call inside pytest.raises)

    # Assert
    with pytest.raises(ValueError):
        TelegramBridge(**args)


def test_allowed_users_filter_accepts_integer_match() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["42"])

    # Act
    allowed = bridge._is_user_allowed(42)

    # Assert
    assert allowed is True


def test_allowed_users_filter_accepts_string_match() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["99"])

    # Act
    allowed = bridge._is_user_allowed("99")

    # Assert
    assert allowed is True


def test_allowed_users_filter_rejects_non_member() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["42"])

    # Act
    allowed = bridge._is_user_allowed(7)

    # Assert
    assert allowed is False


def test_allowed_users_filter_empty_list_fails_closed() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=[])

    # Act
    allowed = bridge._is_user_allowed(42)

    # Assert
    assert allowed is False


def _build_update(
    *,
    user_id: int = 1,
    chat_id: int = -1,
    text: str = "hi",
    message_id: int = 11,
    username: str = "alice",
    first_name: str = "Alice",
    date: int = 1700000000,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": message_id,
        "from": {"id": user_id, "username": username, "first_name": first_name},
        "chat": {"id": chat_id},
        "text": text,
        "date": date,
    }
    if extra:
        msg.update(extra)
    return {"update_id": message_id * 10, "message": msg}


def _run_with_notifier(bridge: TelegramBridge, update: dict[str, Any]) -> list:
    notifications: list[dict[str, Any]] = []

    async def notifier(payload: dict[str, Any]) -> None:
        notifications.append(payload)

    bridge._notifier = notifier  # type: ignore[assignment]
    asyncio.run(bridge._process_update(update))
    return notifications


def test_process_update_drops_when_user_not_allowed() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    update = _build_update(user_id=999)

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications == []


def test_process_update_publishes_content_when_user_allowed() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    update = _build_update(user_id=1, text="hello from alice")

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications[0]["content"] == "hello from alice"


def test_process_update_meta_marks_source_as_telegram() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    update = _build_update(user_id=1)

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications[0]["meta"]["source"] == "telegram"


def test_process_update_meta_contains_chat_id() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    update = _build_update(user_id=1, chat_id=-42)

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications[0]["meta"]["chat_id"] == "-42"


def test_process_update_meta_contains_message_id() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    update = _build_update(user_id=1, message_id=77)

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications[0]["meta"]["message_id"] == "77"


def test_process_update_meta_contains_user_id() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["5"])
    update = _build_update(user_id=5)

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications[0]["meta"]["user_id"] == "5"


def test_process_update_meta_contains_username() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    update = _build_update(user_id=1, username="alice")

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications[0]["meta"]["username"] == "alice"


def test_process_update_picks_largest_photo() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    extra = {
        "photo": [
            {"file_id": "small", "file_size": 100},
            {"file_id": "best", "file_size": 9999},
        ],
        "caption": "look",
    }
    update = _build_update(user_id=1, text="", extra=extra)
    update["message"].pop("text", None)

    # Act
    notifications = _run_with_notifier(bridge, update)

    # Assert
    assert notifications[0]["meta"]["attachments"][0]["file_id"] == "best"


def test_process_update_returns_when_message_field_missing() -> None:
    # Arrange
    bridge = _make_bridge(allowed_users=["1"])
    sink: list[dict[str, Any]] = []

    async def notifier(p: dict[str, Any]) -> None:
        sink.append(p)

    bridge._notifier = notifier  # type: ignore[assignment]

    # Act
    asyncio.run(bridge._process_update({"update_id": 1}))

    # Assert
    assert sink == []


def test_send_message_returns_api_result() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"sendMessage": {"message_id": 99}})
    bridge._api = fake  # type: ignore[assignment]
    bridge._session = object()  # type: ignore[assignment]

    # Act
    result = asyncio.run(bridge.send_message("123", "hi"))

    # Assert
    assert result == {"message_id": 99}


def test_send_message_calls_sendMessage_method() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"sendMessage": {"message_id": 99}})
    bridge._api = fake  # type: ignore[assignment]
    bridge._session = object()  # type: ignore[assignment]

    # Act
    asyncio.run(bridge.send_message("123", "hi"))

    # Assert
    assert fake.calls[0][0] == "sendMessage"


def test_send_message_with_reply_to_passes_through() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"sendMessage": {"message_id": 1}})
    bridge._api = fake  # type: ignore[assignment]
    bridge._session = object()  # type: ignore[assignment]

    # Act
    asyncio.run(bridge.send_message("c", "t", reply_to=42))

    # Assert
    assert fake.calls[0][1] == {
        "chat_id": "c",
        "text": "t",
        "reply_to_message_id": 42,
    }


def test_edit_message_returns_api_result() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"editMessageText": {"edited": True}})
    bridge._api = fake  # type: ignore[assignment]

    # Act
    out = asyncio.run(bridge.edit_message("c", 5, "new"))

    # Assert
    assert out == {"edited": True}


def test_edit_message_routes_to_editMessageText_method() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"editMessageText": {"edited": True}})
    bridge._api = fake  # type: ignore[assignment]

    # Act
    asyncio.run(bridge.edit_message("c", 5, "new"))

    # Assert
    assert fake.calls[0][0] == "editMessageText"


def test_react_returns_api_result() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"setMessageReaction": {"ok": True}})
    bridge._api = fake  # type: ignore[assignment]

    # Act
    out = asyncio.run(bridge.react("c", 1, "+1"))

    # Assert
    assert out == {"ok": True}


def test_react_passes_emoji_in_reaction_payload() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"setMessageReaction": {"ok": True}})
    bridge._api = fake  # type: ignore[assignment]

    # Act
    asyncio.run(bridge.react("c", 1, "+1"))

    # Assert
    assert fake.calls[0][1]["reaction"] == [{"type": "emoji", "emoji": "+1"}]


def test_download_attachment_errors_when_session_missing() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"getFile": {"file_path": "photos/x.jpg"}})
    bridge._api = fake  # type: ignore[assignment]

    # Act
    out = asyncio.run(bridge.download_attachment("abc"))

    # Assert
    assert "error" in out


def test_download_attachment_errors_when_file_unresolved() -> None:
    # Arrange
    bridge = _make_bridge()
    fake = _FakeAPI({"getFile": None})
    bridge._api = fake  # type: ignore[assignment]
    bridge._session = object()  # type: ignore[assignment]

    # Act
    out = asyncio.run(bridge.download_attachment("abc"))

    # Assert
    assert "error" in out


def test_send_document_errors_on_missing_session() -> None:
    # Arrange
    bridge = _make_bridge()

    # Act
    out = asyncio.run(bridge.send_document("c", "/tmp/nonexistent"))

    # Assert
    assert "error" in out


def test_send_document_errors_on_missing_file(tmp_path) -> None:
    # Arrange
    bridge = _make_bridge()
    bridge._session = object()  # type: ignore[assignment]

    # Act
    out = asyncio.run(bridge.send_document("c", str(tmp_path / "doesnotexist")))

    # Assert
    assert "no such file" in out.get("error", "")
