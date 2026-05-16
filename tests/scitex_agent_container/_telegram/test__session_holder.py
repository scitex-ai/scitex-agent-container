"""Tests for the ServerSession-capture monkey patch (telegram channel emit).

TQ cleanup: AAA-marked, one assertion per test, no unittest.mock — we
exercise the real ``mcp.server.session.ServerSession.__init__`` patch
against an in-test recorder.
"""

from __future__ import annotations

import asyncio

import pytest

# mcp may be absent on a minimal install — gate every test on it.
pytest.importorskip("mcp.server.session")

from scitex_agent_container._mcp.server import (  # noqa: E402
    _emit_channel_notification,
    _make_telegram_notifier,
)
from scitex_agent_container._telegram import (  # noqa: E402
    _session_holder as _holder_mod,
)
from scitex_agent_container._telegram._session_holder import (  # noqa: E402
    _reset_for_tests,
    get_active_session,
    install,
)


class _RecordingSession:
    """Real (non-mock) stand-in that captures send_message calls so we
    can assert the channel notification shape end-to-end."""

    def __init__(self):
        self.sent: list = []

    async def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _clear_holder_between_tests():
    """Reset the module-level holder so test order doesn't matter."""
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_install_returns_true_when_mcp_available():
    # Arrange
    # Act
    ok = install()
    # Assert
    assert ok is True


def test_install_is_idempotent():
    # Arrange
    install()
    # Act
    ok = install()
    # Assert
    assert ok is True


def test_get_active_session_is_none_before_any_session_constructed():
    # Arrange — patch installed but no ServerSession yet
    install()
    # Act
    found = get_active_session()
    # Assert
    assert found is None


def test_constructed_server_session_registers_in_holder():
    """Real ServerSession construction must populate the holder."""
    # Arrange
    install()
    from mcp.server.session import ServerSession

    async def _make_session():
        # ServerSession needs read/write streams + init_options.
        # anyio.create_memory_object_stream gives us a real pair.
        import anyio

        read_send, read_recv = anyio.create_memory_object_stream(0)
        write_send, _write_recv = anyio.create_memory_object_stream(0)
        from mcp.server.lowlevel import NotificationOptions
        from mcp.server.models import InitializationOptions
        from mcp.types import ServerCapabilities

        init_opts = InitializationOptions(
            server_name="test",
            server_version="0.0.0",
            capabilities=ServerCapabilities(),
        )
        # We don't run the session loop — only construction matters
        # for the holder patch.
        sess = ServerSession(read_recv, write_send, init_opts)
        # Suppress unused-var warnings
        del read_send
        del _write_recv
        del NotificationOptions
        return sess

    # Act
    sess = asyncio.run(_make_session())

    # Assert
    assert get_active_session() is sess


# ---------------------------------------------------------------------------
# Notifier integration: end-to-end shape of the emitted JSON-RPC message
# ---------------------------------------------------------------------------


def _payload(content="hi", chat_id="42"):
    return {
        "content": content,
        "meta": {"source": "telegram", "chat_id": chat_id},
    }


def test_emit_channel_notification_sends_one_message():
    # Arrange
    sess = _RecordingSession()
    # Act
    asyncio.run(_emit_channel_notification(sess, _payload()))
    # Assert
    assert len(sess.sent) == 1


def test_emit_channel_notification_uses_claude_channel_method():
    # Arrange
    sess = _RecordingSession()
    # Act
    asyncio.run(_emit_channel_notification(sess, _payload()))
    # Assert
    assert sess.sent[0].message.root.method == "notifications/claude/channel"


def test_emit_channel_notification_passes_content_through():
    # Arrange
    sess = _RecordingSession()
    # Act
    asyncio.run(_emit_channel_notification(sess, _payload(content="ピン")))
    # Assert
    assert sess.sent[0].message.root.params["content"] == "ピン"


def test_emit_channel_notification_passes_meta_through():
    # Arrange
    sess = _RecordingSession()
    # Act
    asyncio.run(_emit_channel_notification(sess, _payload(chat_id="999")))
    # Assert
    assert sess.sent[0].message.root.params["meta"]["chat_id"] == "999"


def test_notifier_skips_send_when_no_session_in_holder():
    # Arrange
    _reset_for_tests()
    notifier = _make_telegram_notifier()
    # Act — must not raise; just logs and returns
    asyncio.run(notifier(_payload()))
    # Assert — holder still empty
    assert get_active_session() is None


def test_notifier_dispatches_to_recording_session_when_holder_set():
    # Arrange
    sess = _RecordingSession()
    _holder_mod._active_session = sess
    notifier = _make_telegram_notifier()
    # Act
    asyncio.run(notifier(_payload(content="from-holder")))
    # Assert
    assert sess.sent[0].message.root.params["content"] == "from-holder"
