"""Tests for the sender-side empty-content-ack noise filter.

Two surfaces are covered:

1. The pure structural predicate
   :func:`scitex_agent_container._mcp._channel_ack_filter.envelope_is_contentless_ack`
   — empty body + ``metadata.ack`` truthy ⇒ True; every other shape ⇒
   False. No I/O, no mocks, just structural checks.

2. The integration of that predicate at the two outbound chokepoints —
   ``_channel_tools._send_or_raise`` (driven through the ``a2a_send``
   tool) and ``channel._post_auto_ack`` (driven through
   ``_push_channel_event``) — verified end-to-end against the real
   asyncio HTTP/1.1 ``_FakeListenServer`` from
   ``tests/.../_mcp/test__channel_tools.py``. No mocks, no monkeypatch.

Per the operator's contract:
- (a) empty-content ack is dropped at the sender;
- (b) non-empty ack passes through;
- (c) empty content WITHOUT the ack marker is NOT dropped;
- (d) the drop is logged at DEBUG.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("mcp.types")

from scitex_agent_container._mcp._channel_ack_filter import (  # noqa: E402
    envelope_is_contentless_ack,
)
from scitex_agent_container._mcp._channel_tools import register_tools  # noqa: E402
from scitex_agent_container._mcp.channel import (  # noqa: E402
    _post_auto_ack,
    _recent,
)

# Reuse the real HTTP fake from the sibling test module — it's a real
# asyncio.start_server-backed HTTP/1.1 listener, no mocks.
from tests.scitex_agent_container._mcp.test__channel_tools import (  # noqa: E402
    _FakeListenServer,
    _ToolRecorder,
)

# ---------------------------------------------------------------------------
# (1) Pure structural predicate
# ---------------------------------------------------------------------------


def _envelope(
    *, content: str | None, ack: bool, include_parts: bool = True
) -> dict[str, Any]:
    """Build a minimal JSON-RPC ``SendMessage`` envelope for the predicate.

    ``include_parts=False`` models the no-parts edge case (the helper
    treats no-parts as empty body).
    """
    message: dict[str, Any] = {"message_id": "mid", "role": "ROLE_USER"}
    if include_parts:
        # ``content=None`` here means: parts present but with no ``text`` key,
        # which the predicate treats as structurally-malformed (False).
        if content is None:
            message["parts"] = [{}]
        else:
            message["parts"] = [{"text": content}]
    metadata: dict[str, Any] = {"from_agent": "alice"}
    if ack:
        metadata["ack"] = True
    return {
        "jsonrpc": "2.0",
        "id": "rid",
        "method": "SendMessage",
        "params": {"message": message, "metadata": metadata},
    }


def test_predicate_drops_empty_string_content_ack():
    # Arrange
    env = _envelope(content="", ack=True)
    # Act
    decision = envelope_is_contentless_ack(env)
    # Assert
    assert decision is True


def test_predicate_drops_whitespace_only_content_ack():
    # Arrange
    env = _envelope(content="   \n\t  ", ack=True)
    # Act
    decision = envelope_is_contentless_ack(env)
    # Assert
    assert decision is True


def test_predicate_keeps_non_empty_content_ack():
    """Operator constraint (b): an ack that carries actual content is a
    normal message — must pass through untouched."""
    # Arrange
    env = _envelope(content="got it", ack=True)
    # Act
    decision = envelope_is_contentless_ack(env)
    # Assert
    assert decision is False


def test_predicate_keeps_empty_content_without_ack_marker():
    """Operator constraint (c): an empty-content message that is NOT an
    ack (no ``ack`` flag, or ``ack=False``) is left alone — the empty
    payload may be intentional (wake ping, etc.)."""
    # Arrange
    env = _envelope(content="", ack=False)
    # Act
    decision = envelope_is_contentless_ack(env)
    # Assert
    assert decision is False


def test_predicate_handles_no_parts_at_all_as_empty():
    """No ``parts`` list at all is morally an empty body — drop alongside
    the explicit empty-string form so partial envelopes can't slip past
    the filter."""
    # Arrange
    env = _envelope(content="", ack=True, include_parts=False)
    # Act
    decision = envelope_is_contentless_ack(env)
    # Assert
    assert decision is True


def test_predicate_is_safe_on_malformed_envelope():
    """The helper is structural — anything it cannot recognise as the
    target shape returns False (caller decides). No crashes on garbage."""
    # Arrange
    bad: dict[str, Any] = {"params": "not-a-dict"}
    # Act
    decision = envelope_is_contentless_ack(bad)
    # Assert
    assert decision is False


# ---------------------------------------------------------------------------
# (2) Integration at the outbound chokepoints — real HTTP fake, no mocks.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_listen():
    server = _FakeListenServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def registered_tools(fake_listen):
    rec = _ToolRecorder()
    register_tools(
        rec, agent_name="alice", listen_url=fake_listen.base_url, bearer=None
    )
    return rec


@pytest.fixture(autouse=True)
def _clear_recent_ring():
    _recent.clear()
    yield
    _recent.clear()


@pytest.mark.asyncio
async def test_a2a_ack_tool_does_not_emit_a_network_post(
    registered_tools: _ToolRecorder, fake_listen
):
    """Constraint (a): an empty-content ack from the ``a2a_ack`` tool is
    dropped at the sender — zero HTTP POSTs reach the fake listen."""
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c1"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_ack", {"msg_id": "m1"})
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_a2a_send_with_real_content_still_reaches_listen(
    registered_tools: _ToolRecorder, fake_listen
):
    """Constraint: non-ack messages with content are NOT affected by the
    filter — the regular send path is untouched. Lock that in."""
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_send", {"target": "bob", "content": "hello"})
    paths = [p for p, _ in fake_listen.posts]
    # Assert
    assert "/agents/bob/message:send" in paths


@pytest.mark.asyncio
async def test_post_auto_ack_is_suppressed_at_sender(fake_listen):
    """Constraint (a): the receive-side adapter's stage-2 receipt always
    builds an empty + ``ack=True`` envelope — the sender-side filter must
    drop it before any HTTP POST is made to the fake listen."""
    # Arrange
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _post_auto_ack(
        event, agent_name="alice", listen_url=fake_listen.base_url, bearer=None
    )
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_a2a_ack_tool_drop_is_logged_at_debug(
    registered_tools: _ToolRecorder, fake_listen, caplog
):
    """Constraint (d): the drop is logged. The send-side helper logs at
    DEBUG ('suppressing empty-content ack ...'). Capture that record."""
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c1"})
    call_fn = registered_tools.call_tool_fn
    # Act
    with caplog.at_level(
        logging.DEBUG, logger="scitex_agent_container._mcp._channel_tools"
    ):
        await call_fn("a2a_ack", {"msg_id": "m1"})
    matched = [
        r for r in caplog.records if "suppressing empty-content ack" in r.message
    ]
    # Assert
    assert matched and matched[0].levelno == logging.DEBUG


@pytest.mark.asyncio
async def test_post_auto_ack_drop_is_logged_at_debug(fake_listen, caplog):
    """Constraint (d): the auto-ack drop is logged at DEBUG with the
    'suppressing empty-content auto-ack' message — distinct from the
    send-tool drop log so operators can tell the two chokepoints apart."""
    # Arrange
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    with caplog.at_level(
        logging.DEBUG, logger="scitex_agent_container._mcp._channel_auto_ack"
    ):
        await _post_auto_ack(
            event, agent_name="alice", listen_url=fake_listen.base_url, bearer=None
        )
    matched = [
        r for r in caplog.records if "suppressing empty-content auto-ack" in r.message
    ]
    # Assert
    assert matched and matched[0].levelno == logging.DEBUG


@pytest.mark.asyncio
async def test_a2a_ack_returns_structured_suppression_marker(
    registered_tools: _ToolRecorder, fake_listen
):
    """The dropped ack still returns a structured ``{suppressed: empty_ack}``
    body to the caller so an awaiting flow can distinguish 'we suppressed'
    from 'we delivered'. This is the sender-side contract: silent on the
    wire, but explicit to the caller."""
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c1"})
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_ack", {"msg_id": "m1"})
    body = json.loads(out[0].text)
    # Assert
    assert body.get("body", {}).get("suppressed") == "empty_ack"
