"""Phase 1 import-surface lock for ``_mcp/_tools/_telegram.py``.

Each transport tool must raise ``NotImplementedError`` with the documented
Phase 2 port-target message. Registration must be feature-flag gated off
by default.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

import pytest

from scitex_agent_container._mcp._tools import _telegram as tg

_EXPECTED_STUB_MSG = "Phase 2: port from claude-code-telegrammer/ts/telegram-server.ts"


@contextlib.contextmanager
def _env(name: str, value: str | None) -> Iterator[None]:
    """Set or unset an env var for the duration of the block.

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


class _RecordingMCP:
    """Hand-rolled fake — counts ``tool()`` invocations and captures wrapped
    callables. Used in lieu of ``monkeypatch``-style mocking."""

    def __init__(self) -> None:
        self.registered: list = []

    def tool(self):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


def test_telegram_send_stub_raises_not_implemented() -> None:
    # Arrange
    args = {"chat_id": "1", "text": "hi"}

    # Act + Assert
    # Assert
    with pytest.raises(NotImplementedError, match=_EXPECTED_STUB_MSG):
        # Act
        tg.telegram_send(**args)


def test_telegram_reply_stub_raises_not_implemented() -> None:
    # Arrange
    args = {"chat_id": "1", "text": "hi"}

    # Act
    # (raising call is the act; assertion captures it)

    # Assert
    with pytest.raises(NotImplementedError, match=_EXPECTED_STUB_MSG):
        tg.telegram_reply(**args)


def test_telegram_react_stub_raises_not_implemented() -> None:
    # Arrange
    args = {"chat_id": "1", "message_id": 1, "emoji": "+1"}

    # Act
    # (call inside pytest.raises)

    # Assert
    with pytest.raises(NotImplementedError, match=_EXPECTED_STUB_MSG):
        tg.telegram_react(**args)


def test_telegram_edit_message_stub_raises_not_implemented() -> None:
    # Arrange
    args = {"chat_id": "1", "message_id": 1, "text": "x"}

    # Act
    # (call inside pytest.raises)

    # Assert
    with pytest.raises(NotImplementedError, match=_EXPECTED_STUB_MSG):
        tg.telegram_edit_message(**args)


def test_telegram_download_attachment_stub_raises_not_implemented() -> None:
    # Arrange
    args = {"file_id": "abc"}

    # Act
    # (call inside pytest.raises)

    # Assert
    with pytest.raises(NotImplementedError, match=_EXPECTED_STUB_MSG):
        tg.telegram_download_attachment(**args)


def test_telegram_send_document_stub_raises_not_implemented() -> None:
    # Arrange
    args = {"chat_id": "1", "path": "/tmp/x"}

    # Act
    # (call inside pytest.raises)

    # Assert
    with pytest.raises(NotImplementedError, match=_EXPECTED_STUB_MSG):
        tg.telegram_send_document(**args)


def test_register_is_no_op_when_feature_flag_unset() -> None:
    # Arrange
    mcp = _RecordingMCP()

    # Act
    with _env(tg.FEATURE_FLAG_ENV, None):
        tg.register_telegram_tools(mcp)

    # Assert: feature flag off -> nothing registered
    assert mcp.registered == []


def test_register_registers_six_tools_when_feature_flag_set() -> None:
    # Arrange
    mcp = _RecordingMCP()

    # Act
    with _env(tg.FEATURE_FLAG_ENV, "1"):
        tg.register_telegram_tools(mcp)

    # Assert: all six transport tools registered
    assert len(mcp.registered) == 6


def test_register_registers_expected_tool_names_when_feature_flag_set() -> None:
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
    with _env(tg.FEATURE_FLAG_ENV, "1"):
        tg.register_telegram_tools(mcp)

    # Assert
    assert {fn.__name__ for fn in mcp.registered} == expected
