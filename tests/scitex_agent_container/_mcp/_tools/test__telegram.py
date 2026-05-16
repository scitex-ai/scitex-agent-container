"""Tests for the ``telegram_*`` MCP tool wrappers.

Covers:
* feature-flag default (Phase 3: on by default, opt-out via ``"0"``),
* auth gate (rejects when bridge token is unset or env mismatches),
* each tool delegates to the in-process bridge with the right method
  + arguments,
* registration creates exactly six tools.

The bridge is a hand-rolled fake set via the public ``set_bridge`` API —
no monkeypatch, no mocker. The fake exposes the same async methods as
the real bridge and records every call.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

import pytest

from scitex_agent_container._mcp._tools import _telegram as tg
from scitex_agent_container._telegram._runtime import (
    clear_bridge,
    set_bridge,
)


class _FakeBridge:
    """Async test double that mirrors the bridge's outbound surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict[str, Any]]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_to: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(("send_message", (chat_id, text), {"reply_to": reply_to}))
        return {"ok": True, "method": "send_message", "chat_id": chat_id}

    async def react(self, chat_id: str, message_id: int, emoji: str) -> dict[str, Any]:
        self.calls.append(("react", (chat_id, message_id, emoji), {}))
        return {"ok": True, "method": "react"}

    async def edit_message(
        self, chat_id: str, message_id: int, text: str
    ) -> dict[str, Any]:
        self.calls.append(("edit_message", (chat_id, message_id, text), {}))
        return {"ok": True, "method": "edit_message"}

    async def download_attachment(
        self, file_id: str, dest_dir: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("download_attachment", (file_id,), {"dest_dir": dest_dir}))
        return {"path": "/tmp/x"}

    async def send_document(
        self, chat_id: str, path: str, *, caption: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("send_document", (chat_id, path), {"caption": caption}))
        return {"ok": True, "method": "send_document"}


@contextlib.contextmanager
def _env(name: str, value: str | None) -> Iterator[None]:
    """Set or unset ``name`` for the duration of the block.

    Real ``os.environ`` mutation with explicit teardown — no monkeypatch.
    """
    sentinel = object()
    prev: Any = os.environ.get(name, sentinel)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if prev is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev  # type: ignore[assignment]


@pytest.fixture()
def wired_bridge() -> Iterator[_FakeBridge]:
    """Install a fake bridge with auth token ``"AUTH"``; tear it down."""
    fake = _FakeBridge()
    set_bridge(fake, auth_token="AUTH")  # type: ignore[arg-type]
    with _env(tg.LEAD_AUTH_TOKEN_ENV, "AUTH"):
        yield fake
    clear_bridge()


@pytest.fixture()
def unwired_bridge() -> Iterator[None]:
    """Ensure no bridge is registered + no caller auth env is set."""
    clear_bridge()
    with _env(tg.LEAD_AUTH_TOKEN_ENV, None):
        yield
    clear_bridge()


class _RecordingMCP:
    """Hand-rolled fake — counts ``tool()`` invocations and captures
    the wrapped callables."""

    def __init__(self) -> None:
        self.registered: list = []

    def tool(self):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


# --- registration --------------------------------------------------------


def test_register_is_no_op_when_feature_flag_off() -> None:
    # Arrange
    mcp = _RecordingMCP()

    # Act
    with _env(tg.FEATURE_FLAG_ENV, "0"):
        tg.register_telegram_tools(mcp)

    # Assert
    assert mcp.registered == []


def test_register_default_on_when_feature_flag_unset() -> None:
    # Arrange: Phase 3 flips default to ON
    mcp = _RecordingMCP()

    # Act
    with _env(tg.FEATURE_FLAG_ENV, None):
        tg.register_telegram_tools(mcp)

    # Assert
    assert len(mcp.registered) == 6


def test_register_registers_expected_tool_names() -> None:
    # Arrange
    mcp = _RecordingMCP()
    expected = {
        "telegram_send",
        "telegram_reply",
        "telegram_react",
        "telegram_edit_message",
        "telegram_download_attachment",
        "telegram_send_document",
    }

    # Act
    with _env(tg.FEATURE_FLAG_ENV, None):
        tg.register_telegram_tools(mcp)

    # Assert
    assert {fn.__name__ for fn in mcp.registered} == expected


# --- auth gate -----------------------------------------------------------


def test_telegram_send_returns_error_when_bridge_uninitialised(
    unwired_bridge,
) -> None:
    # Arrange
    # (no bridge wired by the fixture)

    # Act
    out = tg.telegram_send(chat_id="1", text="hi")

    # Assert
    assert "not initialised" in out.get("error", "")


def test_telegram_send_returns_error_when_caller_token_missing(
    wired_bridge,
) -> None:
    # Arrange
    out: dict[str, Any] = {}

    # Act
    with _env(tg.LEAD_AUTH_TOKEN_ENV, None):
        out = tg.telegram_send(chat_id="1", text="hi")

    # Assert
    assert "error" in out


def test_telegram_send_returns_error_when_caller_token_wrong(
    wired_bridge,
) -> None:
    # Arrange
    out: dict[str, Any] = {}

    # Act
    with _env(tg.LEAD_AUTH_TOKEN_ENV, "WRONG"):
        out = tg.telegram_send(chat_id="1", text="hi")

    # Assert
    assert "error" in out


# --- happy-path delegation ----------------------------------------------


def test_telegram_send_calls_bridge_send_message(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_send(chat_id="123", text="hello")

    # Assert
    assert wired_bridge.calls[0][0] == "send_message"


def test_telegram_send_passes_chat_id_and_text(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_send(chat_id="123", text="hello")

    # Assert
    assert wired_bridge.calls[0][1] == ("123", "hello")


def test_telegram_send_passes_reply_to(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_send(chat_id="123", text="hi", reply_to=42)

    # Assert
    assert wired_bridge.calls[0][2]["reply_to"] == 42


def test_telegram_send_returns_bridge_result(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    out = tg.telegram_send(chat_id="c", text="t")

    # Assert
    assert out.get("ok") is True


def test_telegram_reply_calls_send_message(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_reply(chat_id="c", text="t", row_id=1, reply_to=2)

    # Assert
    assert wired_bridge.calls[0][0] == "send_message"


def test_telegram_react_calls_bridge_react(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_react(chat_id="c", message_id=1, emoji="+1")

    # Assert
    assert wired_bridge.calls[0][0] == "react"


def test_telegram_react_passes_emoji(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_react(chat_id="c", message_id=1, emoji="+1")

    # Assert
    assert wired_bridge.calls[0][1] == ("c", 1, "+1")


def test_telegram_edit_message_calls_bridge_edit(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_edit_message(chat_id="c", message_id=5, text="new")

    # Assert
    assert wired_bridge.calls[0][0] == "edit_message"


def test_telegram_edit_message_passes_text(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_edit_message(chat_id="c", message_id=5, text="new")

    # Assert
    assert wired_bridge.calls[0][1] == ("c", 5, "new")


def test_telegram_download_attachment_calls_bridge_download(
    wired_bridge,
) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_download_attachment(file_id="abc")

    # Assert
    assert wired_bridge.calls[0][0] == "download_attachment"


def test_telegram_download_attachment_passes_dest_dir(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_download_attachment(file_id="abc", dest_dir="/tmp/x")

    # Assert
    assert wired_bridge.calls[0][2]["dest_dir"] == "/tmp/x"


def test_telegram_send_document_calls_bridge_send_document(
    wired_bridge,
) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_send_document(chat_id="c", path="/tmp/file.txt")

    # Assert
    assert wired_bridge.calls[0][0] == "send_document"


def test_telegram_send_document_passes_caption(wired_bridge) -> None:
    # Arrange
    # (fixture installs bridge + auth env)

    # Act
    tg.telegram_send_document(chat_id="c", path="/tmp/file.txt", caption="cap")

    # Assert
    assert wired_bridge.calls[0][2]["caption"] == "cap"
