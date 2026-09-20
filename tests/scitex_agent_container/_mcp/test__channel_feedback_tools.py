"""Adversarial tests for model-authored A2A ACK and progress tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio
from mcp.types import CallToolResult

from scitex_agent_container._mcp._channel_tools import register_tools
from scitex_agent_container._mcp.channel import _push_channel_event, _recent


class _ToolRecorder:
    def __init__(self) -> None:
        self.list_tools_fn: Any = None
        self.call_tool_fn: Any = None

    def list_tools(self):
        def decorate(fn):
            self.list_tools_fn = fn
            return fn

        return decorate

    def call_tool(self):
        def decorate(fn):
            self.call_tool_fn = fn
            return fn

        return decorate


class _Session:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def send_message(self, message: Any) -> None:
        self.messages.append(message)


def _registered(listen_url: str = "http://127.0.0.1:1") -> _ToolRecorder:
    recorder = _ToolRecorder()
    register_tools(
        recorder,
        agent_name="bob",
        listen_url=listen_url,
        bearer=None,
    )
    return recorder


class _Listen:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readline()
            length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            body = await reader.readexactly(length)
            self.posts.append(json.loads(body))
            response = json.dumps({"delivered_subscriber_count": 1}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(response)}\r\nConnection: close\r\n\r\n".encode()
                + response
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest_asyncio.fixture
async def listen():
    server = _Listen()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _payload(result: Any) -> dict[str, Any]:
    blocks = result.content if isinstance(result, CallToolResult) else result
    return json.loads(getattr(blocks[0], "text"))


@pytest.fixture(autouse=True)
def _clear_inbox():
    _recent.clear()
    yield
    _recent.clear()


@pytest.mark.asyncio
async def test_tool_list_advertises_agentic_feedback_protocol() -> None:
    # Arrange
    recorder = _registered()
    # Act
    names = {tool.name for tool in await recorder.list_tools_fn()}
    # Assert
    assert {"a2a_agentic_ack", "a2a_progress", "a2a_dispatch_status"} <= names


@pytest.mark.asyncio
async def test_agentic_ack_refuses_wrong_or_stale_nonce() -> None:
    # Arrange
    recorder = _registered()
    # Act
    result = await recorder.call_tool_fn(
        "a2a_agentic_ack",
        {
            "dispatch_id": "not-in-inbox",
            "understood": "Do the requested work.",
            "owner": "bob",
            "next_checkpoint": "Report tests.",
        },
    )
    error = _payload(result).get("error", "")
    # Assert
    assert "unknown or stale dispatch_id" in error


@pytest.mark.asyncio
async def test_agentic_ack_posts_exact_nonce_and_model_fields(listen: _Listen) -> None:
    # Arrange
    _recent.append(
        {
            "dispatch_id": "nonce-123",
            "msg_id": "message-1",
            "from_agent": "alice",
            "conversation_id": "conversation-1",
        }
    )
    recorder = _registered(f"http://127.0.0.1:{listen.port}")
    # Act
    await recorder.call_tool_fn(
        "a2a_agentic_ack",
        {
            "dispatch_id": "nonce-123",
            "understood": "Implement and test nonce verification.",
            "owner": "bob",
            "next_checkpoint": "Focused tests green.",
        },
    )
    extra = listen.posts[0]["params"]["metadata"]["extra"] if listen.posts else {}
    # Assert
    assert extra == {
        "dispatch_id": "nonce-123",
        "understood": "Implement and test nonce verification.",
        "owner": "bob",
        "next_checkpoint": "Focused tests green.",
    }


@pytest.mark.asyncio
async def test_progress_posts_typed_status_for_exact_nonce(listen: _Listen) -> None:
    # Arrange
    _recent.append(
        {
            "dispatch_id": "nonce-456",
            "msg_id": "message-2",
            "from_agent": "alice",
        }
    )
    recorder = _registered(f"http://127.0.0.1:{listen.port}")
    await recorder.call_tool_fn(
        "a2a_agentic_ack",
        {
            "dispatch_id": "nonce-456",
            "understood": "Run adversarial tests.",
            "owner": "bob",
            "next_checkpoint": "Report test results.",
        },
    )
    listen.posts.clear()
    # Act
    await recorder.call_tool_fn(
        "a2a_progress",
        {
            "dispatch_id": "nonce-456",
            "status": "in_progress",
            "summary": "Adversarial nonce tests are running.",
            "blocker": "PostgreSQL fixture unavailable.",
        },
    )
    metadata = listen.posts[0]["params"]["metadata"] if listen.posts else {}
    # Assert
    assert metadata.get("kind") == "a2a_progress" and metadata.get("extra") == {
        "dispatch_id": "nonce-456",
        "status": "in_progress",
        "summary": "Adversarial nonce tests are running.",
        "blocker": "PostgreSQL fixture unavailable.",
    }


@pytest.mark.asyncio
async def test_http_200_send_returns_delivered_unacknowledged_hint(listen: _Listen) -> None:
    # Arrange
    recorder = _registered(f"http://127.0.0.1:{listen.port}")
    # Act
    result = await recorder.call_tool_fn(
        "a2a_send", {"target": "alice", "content": "Do the work."}
    )
    body = _payload(result)
    # Assert
    assert body.get("dispatch_status") == "delivered_unacknowledged" and (
        "understanding is unproven" in body.get("hint", "").lower()
    )


@pytest.mark.asyncio
async def test_automatic_receive_hooks_never_emit_agentic_ack(listen: _Listen) -> None:
    # Arrange
    event = {
        "msg_id": "message-auto",
        "dispatch_id": "nonce-auto",
        "from_agent": "alice",
        "content": "Do work.",
    }
    # Act
    await _push_channel_event(
        _Session(),
        event,
        agent_name="bob",
        listen_url=f"http://127.0.0.1:{listen.port}",
        bearer=None,
    )
    kinds = [post["params"]["metadata"].get("kind") for post in listen.posts]
    # Assert
    assert "agentic_ack" not in kinds
