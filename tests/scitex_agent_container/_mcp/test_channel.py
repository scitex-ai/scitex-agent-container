"""Tests for the sac MCP **channel** server (``_mcp.channel``).

The channel server is a stdio MCP subprocess that:

1. Opens an HTTP/SSE connection to ``sac listen`` at
   ``/agents/<name>/inbox/stream`` and converts every event into an
   MCP ``notifications/claude/channel`` JSON-RPC notification.
2. Registers the ``a2a_*`` tool surface (``a2a_send``, ``a2a_reply``,
   ``a2a_ack``, ``a2a_peers``, ``a2a_inbox``), which speak HTTP to the
   same ``sac listen`` HTTP base.

Per the "no cut corners" principle these tests use **real** asyncio +
real ``httpx`` + a real ``asyncio.start_server``-backed HTTP/1.1 server
on loopback that speaks SSE and JSON. No mocks, no monkeypatch — the
only test double is an explicit ``_ToolRecorder`` mirroring the MCP
``@server.list_tools()`` / ``@server.call_tool()`` registration
contract (a real collaborator, not a mock).

AAA markers, one-assert per test (TQ002, TQ007).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

mcp_types = pytest.importorskip("mcp.types")  # gates entire module on `mcp`
from mcp.types import TextContent, Tool  # noqa: E402

from scitex_agent_container._mcp import channel as channel_mod  # noqa: E402
from scitex_agent_container._mcp.channel import (  # noqa: E402
    _build_notification,
    _consume_sse,
    _recent,
    _register_tools,
)

# ---------------------------------------------------------------------------
# Real in-process HTTP/1.1 + SSE server (no aiohttp dependency).
#
# Speaks just enough HTTP to satisfy httpx.AsyncClient: a one-line
# request parser, header collection, then dispatch on (method, path).
# SSE responses keep the connection open and write `data: ...\n\n`
# frames; JSON responses Content-Length their body and close.
# ---------------------------------------------------------------------------


class _FakeListenServer:
    """A real asyncio TCP server speaking minimal HTTP/1.1 for tests."""

    def __init__(self) -> None:
        self.sse_events: list[dict[str, Any]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.peers_payload: dict[str, Any] = {"agents": []}
        self._server: asyncio.base_events.Server | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host=self.host, port=0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, path, _ = request_line.decode().rstrip("\r\n").split(" ")
            except ValueError:
                return
            # Drain headers.
            content_length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            body = b""
            if content_length:
                body = await reader.readexactly(content_length)

            if method == "GET" and path.endswith("/inbox/stream"):
                await self._serve_sse(writer)
            elif method == "GET" and path == "/agents/":
                await self._serve_json(writer, self.peers_payload)
            elif method == "POST" and "/message:send" in path:
                try:
                    payload = json.loads(body.decode() or "{}")
                except json.JSONDecodeError:
                    payload = {}
                self.posts.append((path, payload))
                await self._serve_json(writer, {"ok": True})
            else:
                await self._serve_status(writer, 404, b"not found")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve_sse(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
            b": keep-alive\n\n"
        )
        await writer.drain()
        for ev in self.sse_events:
            writer.write(f"data: {json.dumps(ev)}\n\n".encode())
            await writer.drain()
        # Hold the connection open briefly so the client has time to
        # read the buffered frames before EOF.
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _serve_json(
        self, writer: asyncio.StreamWriter, payload: dict[str, Any]
    ) -> None:
        body = json.dumps(payload).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )
        await writer.drain()

    async def _serve_status(
        self, writer: asyncio.StreamWriter, code: int, body: bytes
    ) -> None:
        writer.write(
            f"HTTP/1.1 {code} X\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()


@pytest_asyncio.fixture
async def fake_listen():
    server = _FakeListenServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


class _ToolRecorder:
    """Captures ``@server.list_tools()`` and ``@server.call_tool()``
    decorations onto a structural stand-in.

    Mirrors the MCP server contract closely enough that
    ``_register_tools`` runs unchanged, and the captured callables can
    be invoked directly. Not a mock — no auto-spec, no call tracking
    magic; just attribute storage.
    """

    def __init__(self) -> None:
        self.list_tools_fn = None
        self.call_tool_fn = None

    def list_tools(self):
        def _decorate(fn):
            self.list_tools_fn = fn
            return fn

        return _decorate

    def call_tool(self):
        def _decorate(fn):
            self.call_tool_fn = fn
            return fn

        return _decorate


@pytest.fixture
def tool_recorder() -> _ToolRecorder:
    rec = _ToolRecorder()
    return rec


@pytest.fixture
def registered_tools(tool_recorder: _ToolRecorder, fake_listen):
    """Register tools against the recorder, pointing at the fake server."""
    _register_tools(
        tool_recorder,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    return tool_recorder


@pytest.fixture(autouse=True)
def _clear_recent_ring():
    """Each test sees an empty inbox ring buffer."""
    _recent.clear()
    yield
    _recent.clear()


# ---------------------------------------------------------------------------
# _build_notification — pure projection
# ---------------------------------------------------------------------------


def test_build_notification_content_passes_through_verbatim():
    # Arrange
    event = {"content": "hello world", "from_agent": "bob"}
    # Act
    notif = _build_notification(event)
    # Assert
    assert notif["content"] == "hello world"


def test_build_notification_missing_content_defaults_to_empty_string():
    # Arrange
    event: dict[str, Any] = {"from_agent": "bob"}
    # Act
    notif = _build_notification(event)
    # Assert
    assert notif["content"] == ""


def test_build_notification_source_carries_from_agent():
    # Arrange
    event = {"from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["source"] == "bob"


def test_build_notification_missing_from_agent_marks_unknown_source():
    # Arrange
    event = {"content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["source"] == "unknown"


def test_build_notification_carries_msg_id():
    # Arrange
    event = {"msg_id": "abc123", "from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["msg_id"] == "abc123"


def test_build_notification_ts_is_stringified():
    # Arrange
    event = {"ts": 1_234, "from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["ts"] == "1234"


@pytest.mark.parametrize(
    "key,value",
    [
        ("conversation_id", "conv-42"),
        ("in_reply_to", "msg-7"),
        ("priority", "high"),
        ("requires_reply", True),
    ],
)
def test_build_notification_propagates_optional_meta_key(key: str, value: Any):
    # Arrange
    event = {"from_agent": "bob", "content": "x", key: value}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta[key] == value


def test_build_notification_omits_optional_keys_when_absent():
    # Arrange
    event = {"from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert "conversation_id" not in meta


# ---------------------------------------------------------------------------
# _consume_sse — real SSE over a real asyncio TCP server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_sse_dispatches_event_to_callback(fake_listen):
    # Arrange
    fake_listen.sse_events = [{"from_agent": "bob", "content": "hi", "msg_id": "m1"}]
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{fake_listen.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert
    assert received and received[0]["msg_id"] == "m1"


@pytest.mark.asyncio
async def test_consume_sse_ignores_comment_keepalive_frames(fake_listen):
    """The fake server emits one ``: keep-alive`` comment before data;
    only the real event must reach the callback."""
    # Arrange
    fake_listen.sse_events = [{"from_agent": "bob", "content": "x", "msg_id": "m2"}]
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{fake_listen.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert: exactly one event delivered (the comment was filtered).
    assert len(received) == 1


@pytest.mark.asyncio
async def test_consume_sse_dispatches_multiple_events_in_order(fake_listen):
    # Arrange
    fake_listen.sse_events = [
        {"from_agent": "b", "content": "1", "msg_id": "a"},
        {"from_agent": "b", "content": "2", "msg_id": "b"},
        {"from_agent": "b", "content": "3", "msg_id": "c"},
    ]
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{fake_listen.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act
    for _ in range(60):
        if len(received) >= 3:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert
    assert [e["msg_id"] for e in received] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _register_tools — list_tools surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_returns_five_tools(registered_tools: _ToolRecorder):
    # Arrange
    list_fn = registered_tools.list_tools_fn
    # Act
    tools = await list_fn()
    # Assert
    assert len(tools) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected_name",
    ["a2a_send", "a2a_reply", "a2a_ack", "a2a_peers", "a2a_inbox"],
)
async def test_list_tools_includes_expected_tool(
    registered_tools: _ToolRecorder, expected_name: str
):
    # Arrange
    list_fn = registered_tools.list_tools_fn
    # Act
    names = {t.name for t in await list_fn()}
    # Assert
    assert expected_name in names


@pytest.mark.asyncio
async def test_list_tools_every_entry_is_a_tool_instance(
    registered_tools: _ToolRecorder,
):
    # Arrange
    list_fn = registered_tools.list_tools_fn
    # Act
    tools = await list_fn()
    bad = [t for t in tools if not isinstance(t, Tool)]
    # Assert
    assert bad == []


# ---------------------------------------------------------------------------
# _register_tools — call_tool dispatch (real HTTP to fake_listen)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_unknown_name_returns_error_payload(
    registered_tools: _ToolRecorder,
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("not_a_tool", {})
    body = json.loads(out[0].text)
    # Assert
    assert "error" in body


@pytest.mark.asyncio
async def test_call_tool_a2a_send_posts_to_target_path(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_send", {"target": "bob", "content": "hello"})
    paths = [p for p, _ in fake_listen.posts]
    # Assert
    assert "/agents/bob/message:send" in paths


@pytest.mark.asyncio
async def test_call_tool_a2a_send_sets_from_agent_in_metadata(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    _, payload = fake_listen.posts[-1]
    # Assert
    assert payload["params"]["metadata"]["from_agent"] == "alice"


@pytest.mark.asyncio
async def test_call_tool_a2a_send_uses_send_message_method(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    _, payload = fake_listen.posts[-1]
    # Assert
    assert payload["method"] == "SendMessage"


@pytest.mark.asyncio
async def test_call_tool_a2a_send_returns_status_field(
    registered_tools: _ToolRecorder,
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    body = json.loads(out[0].text)
    # Assert
    assert body["status"] == 200


@pytest.mark.asyncio
async def test_call_tool_a2a_reply_unknown_msg_id_returns_error(
    registered_tools: _ToolRecorder,
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_reply", {"in_reply_to": "ghost", "content": "x"})
    body = json.loads(out[0].text)
    # Assert
    assert "error" in body


@pytest.mark.asyncio
async def test_call_tool_a2a_reply_routes_to_original_sender(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange — seed the ring buffer with a received event.
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c1"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_reply", {"in_reply_to": "m1", "content": "thanks"})
    paths = [p for p, _ in fake_listen.posts]
    # Assert
    assert "/agents/carol/message:send" in paths


@pytest.mark.asyncio
async def test_call_tool_a2a_reply_carries_conversation_id(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c-orig"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_reply", {"in_reply_to": "m1", "content": "y"})
    _, payload = fake_listen.posts[-1]
    # Assert
    assert payload["params"]["metadata"]["conversation_id"] == "c-orig"


@pytest.mark.asyncio
async def test_call_tool_a2a_reply_sets_in_reply_to_metadata(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c-orig"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_reply", {"in_reply_to": "m1", "content": "y"})
    _, payload = fake_listen.posts[-1]
    # Assert
    assert payload["params"]["metadata"]["in_reply_to"] == "m1"


@pytest.mark.asyncio
async def test_call_tool_a2a_reply_unknown_sender_returns_error(
    registered_tools: _ToolRecorder,
):
    # Arrange — event with no from_agent.
    _recent.append({"msg_id": "m9", "conversation_id": "c"})
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_reply", {"in_reply_to": "m9", "content": "x"})
    body = json.loads(out[0].text)
    # Assert
    assert "error" in body


@pytest.mark.asyncio
async def test_call_tool_a2a_ack_unknown_msg_id_returns_error(
    registered_tools: _ToolRecorder,
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_ack", {"msg_id": "nope"})
    body = json.loads(out[0].text)
    # Assert
    assert "error" in body


@pytest.mark.asyncio
async def test_call_tool_a2a_ack_unknown_sender_returns_error(
    registered_tools: _ToolRecorder,
):
    # Arrange — event present but no from_agent
    _recent.append({"msg_id": "m11"})
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_ack", {"msg_id": "m11"})
    body = json.loads(out[0].text)
    # Assert
    assert "error" in body


@pytest.mark.asyncio
async def test_call_tool_a2a_ack_posts_ack_metadata(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c1"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_ack", {"msg_id": "m1"})
    _, payload = fake_listen.posts[-1]
    # Assert
    assert payload["params"]["metadata"]["ack"] is True


@pytest.mark.asyncio
async def test_call_tool_a2a_peers_returns_status(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    fake_listen.peers_payload = {"agents": ["alice", "bob"]}
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_peers", {})
    body = json.loads(out[0].text)
    # Assert
    assert body["status"] == 200


@pytest.mark.asyncio
async def test_call_tool_a2a_peers_returns_peers_body(
    registered_tools: _ToolRecorder, fake_listen
):
    # Arrange
    fake_listen.peers_payload = {"agents": ["alice", "bob"]}
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_peers", {})
    body = json.loads(out[0].text)
    # Assert
    assert body["body"] == {"agents": ["alice", "bob"]}


@pytest.mark.asyncio
async def test_call_tool_a2a_inbox_returns_count(
    registered_tools: _ToolRecorder,
):
    # Arrange
    _recent.extend(
        [
            {"msg_id": "a", "from_agent": "x"},
            {"msg_id": "b", "from_agent": "x"},
            {"msg_id": "c", "from_agent": "x"},
        ]
    )
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_inbox", {"limit": 10})
    body = json.loads(out[0].text)
    # Assert
    assert body["count"] == 3


@pytest.mark.asyncio
async def test_call_tool_a2a_inbox_respects_limit(
    registered_tools: _ToolRecorder,
):
    # Arrange
    for i in range(20):
        _recent.append({"msg_id": str(i), "from_agent": "x"})
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_inbox", {"limit": 5})
    body = json.loads(out[0].text)
    # Assert
    assert body["count"] == 5


@pytest.mark.asyncio
async def test_call_tool_returns_text_content_instance(
    registered_tools: _ToolRecorder,
):
    # Arrange
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_inbox", {})
    # Assert
    assert isinstance(out[0], TextContent)


# ---------------------------------------------------------------------------
# bearer plumbing — Authorization header on POST
# ---------------------------------------------------------------------------


class _BearerEchoServer(_FakeListenServer):
    """Records the Authorization header seen on POST requests."""

    def __init__(self) -> None:
        super().__init__()
        self.last_auth: str | None = None

    async def _handle(self, reader, writer):  # type: ignore[override]
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, path, _ = request_line.decode().rstrip("\r\n").split(" ")
            except ValueError:
                return
            content_length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                low = line.lower()
                if low.startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
                elif low.startswith(b"authorization:"):
                    self.last_auth = line.split(b":", 1)[1].decode().strip()
            if content_length:
                await reader.readexactly(content_length)
            if method == "POST":
                await self._serve_json(writer, {"ok": True})
            else:
                await self._serve_status(writer, 404, b"x")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


@pytest_asyncio.fixture
async def bearer_server():
    s = _BearerEchoServer()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_register_tools_forwards_bearer_token_on_post(bearer_server):
    # Arrange
    rec = _ToolRecorder()
    _register_tools(
        rec,
        agent_name="alice",
        listen_url=bearer_server.base_url,
        bearer="s3cret",
    )
    # Act
    await rec.call_tool_fn("a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert bearer_server.last_auth == "Bearer s3cret"


# ---------------------------------------------------------------------------
# main() — public entry point smoke
# ---------------------------------------------------------------------------


def test_main_is_exported():
    # Arrange
    public = set(channel_mod.__all__)
    # Act
    has_main = "main" in public
    # Assert
    assert has_main


def test_module_recent_buffer_is_bounded():
    # Arrange
    cap = _recent.maxlen
    # Act
    is_bounded = cap is not None and cap > 0
    # Assert
    assert is_bounded


# ---------------------------------------------------------------------------
# _consume_sse — bearer + error-path coverage on real servers
# ---------------------------------------------------------------------------


class _SSEBearerServer(_FakeListenServer):
    """Records the Authorization header on the SSE GET."""

    def __init__(self) -> None:
        super().__init__()
        self.sse_auth: str | None = None

    async def _handle(self, reader, writer):  # type: ignore[override]
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, path, _ = request_line.decode().rstrip("\r\n").split(" ")
            except ValueError:
                return
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"authorization:"):
                    self.sse_auth = line.split(b":", 1)[1].decode().strip()
            if method == "GET" and path.endswith("/inbox/stream"):
                await self._serve_sse(writer)
            else:
                await self._serve_status(writer, 404, b"x")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


@pytest_asyncio.fixture
async def sse_bearer_server():
    s = _SSEBearerServer()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_consume_sse_forwards_bearer_authorization_header(
    sse_bearer_server: _SSEBearerServer,
):
    # Arrange
    sse_bearer_server.sse_events = [{"from_agent": "x", "content": "y", "msg_id": "m"}]
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{sse_bearer_server.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer="tok", on_event=on_event))
    # Act
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert
    assert sse_bearer_server.sse_auth == "Bearer tok"


class _SSENon200Server(_FakeListenServer):
    """SSE endpoint that returns 503 instead of 200 — exercises the
    warning + reconnect branch in ``_consume_sse``."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def _handle(self, reader, writer):  # type: ignore[override]
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            self.attempts += 1
            body = b"down"
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


@pytest_asyncio.fixture
async def sse_non200_server():
    s = _SSENon200Server()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_consume_sse_reconnects_on_non_200_response(
    sse_non200_server: _SSENon200Server,
):
    # Arrange
    async def on_event(ev: dict[str, Any]) -> None:
        pass

    url = f"{sse_non200_server.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act — let it attempt at least once.
    for _ in range(40):
        if sse_non200_server.attempts >= 1:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert
    assert sse_non200_server.attempts >= 1


class _SSEBadJSONServer(_FakeListenServer):
    """Emits a malformed JSON SSE frame followed by a valid one — only
    the valid event must reach the callback."""

    async def _serve_sse(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Connection: keep-alive\r\n\r\n"
            b"data: {not json}\n\n"
            b'data: {"msg_id": "ok"}\n\n'
        )
        await writer.drain()
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass


@pytest_asyncio.fixture
async def sse_bad_json_server():
    s = _SSEBadJSONServer()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_consume_sse_skips_malformed_json_frames(
    sse_bad_json_server: _SSEBadJSONServer,
):
    # Arrange
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{sse_bad_json_server.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert — only the valid frame survives.
    assert [e.get("msg_id") for e in received] == ["ok"]


# ---------------------------------------------------------------------------
# Non-JSON HTTP responses fall back to .text in _post / _get
# ---------------------------------------------------------------------------


class _PlainTextServer(_FakeListenServer):
    """Returns a non-JSON ``text/plain`` body for both POST and GET."""

    async def _handle(self, reader, writer):  # type: ignore[override]
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, _path, _ = request_line.decode().rstrip("\r\n").split(" ")
            except ValueError:
                return
            content_length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            if content_length:
                await reader.readexactly(content_length)
            body = b"plain-text-body"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
            del method
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


@pytest_asyncio.fixture
async def plain_text_server():
    s = _PlainTextServer()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_call_tool_a2a_send_handles_non_json_response_body(
    plain_text_server: _PlainTextServer,
):
    # Arrange
    rec = _ToolRecorder()
    _register_tools(
        rec,
        agent_name="alice",
        listen_url=plain_text_server.base_url,
        bearer=None,
    )
    # Act
    out = await rec.call_tool_fn("a2a_send", {"target": "bob", "content": "x"})
    body = json.loads(out[0].text)
    # Assert
    assert body["body"] == "plain-text-body"


@pytest.mark.asyncio
async def test_consume_sse_retries_after_connection_error():
    """Bind a loopback socket then immediately close to force refused
    connection. The consumer must log + retry rather than crash."""
    # Arrange — find a closed port by binding and closing.
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    refused_port = s.getsockname()[1]
    s.close()
    url = f"http://127.0.0.1:{refused_port}/agents/x/inbox/stream"
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act — give it time for ConnectError + log + backoff sleep.
    await asyncio.sleep(1.0)
    is_alive = not task.done()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert — the consumer survived the connection error and kept looping.
    assert is_alive


@pytest.mark.asyncio
async def test_call_tool_a2a_peers_handles_non_json_response_body(
    plain_text_server: _PlainTextServer,
):
    # Arrange
    rec = _ToolRecorder()
    _register_tools(
        rec,
        agent_name="alice",
        listen_url=plain_text_server.base_url,
        bearer=None,
    )
    # Act
    out = await rec.call_tool_fn("a2a_peers", {})
    body = json.loads(out[0].text)
    # Assert
    assert body["body"] == "plain-text-body"
