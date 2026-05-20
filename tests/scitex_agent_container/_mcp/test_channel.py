"""Tests for the sac MCP **channel** server (``_mcp.channel``).

The channel server is a stdio MCP subprocess that:

1. Opens an HTTP/SSE connection to ``sac listen`` at
   ``/agents/<name>/inbox/stream`` and converts every event into an
   MCP ``notifications/claude/channel`` JSON-RPC notification.
2. On injecting a received event, emits an automatic ``a2a_ack`` back
   to the sender (stage-2 "read" receipt) — infra-automatic, the agent
   is unaware. The loop-guard skips events that are themselves acks.

The send-side ``a2a_*`` tool surface lives in ``_channel_tools`` and is
covered by ``test__channel_tools.py``; only the wrapper delegation is
asserted here.

Per the "no cut corners" principle these tests use **real** asyncio +
real ``httpx`` + a real ``asyncio.start_server``-backed HTTP/1.1 server
on loopback that speaks SSE and JSON. No mocks, no monkeypatch.

AAA markers, one-assert per test (TQ002, TQ007).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

import pytest
import pytest_asyncio

mcp_types = pytest.importorskip("mcp.types")  # gates entire module on `mcp`
from mcp.types import Tool  # noqa: E402

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
            elif method == "GET" and path.rstrip("/") == "/agents":
                # a2a_peers hits `/agents` (no trailing slash) to dodge the
                # 307 redirect httpx won't follow; accept both shapes so the
                # fake mirrors the real sac listen route.
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
    "key,raw,expected",
    [
        ("conversation_id", "conv-42", "conv-42"),
        ("in_reply_to", "msg-7", "msg-7"),
        ("priority", "high", "high"),
        # requires_reply arrives as a bool off the bus but must be
        # stringified — a raw bool trips the client's Zod validator and
        # the pushed turn is silently dropped.
        ("requires_reply", True, "true"),
        ("requires_reply", False, "false"),
    ],
)
def test_build_notification_propagates_optional_meta_key(
    key: str, raw: Any, expected: str
):
    # Arrange
    event = {"from_agent": "bob", "content": "x", key: raw}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta[key] == expected


def test_build_notification_meta_values_are_all_strings():
    """Regression: Claude Code's channel-notification schema types every
    ``meta`` value as a string. A raw bool (``requires_reply``) made the
    client's notification handler throw a ZodError and silently drop the
    pushed turn. Pin the contract: nothing non-string reaches ``meta``."""
    # Arrange — include the boolean offender plus a non-str ts.
    event = {
        "from_agent": "bob",
        "content": "x",
        "ts": 1234,
        "msg_id": "m1",
        "requires_reply": True,
        "priority": "high",
        "conversation_id": "c1",
    }
    # Act
    meta = _build_notification(event)["meta"]
    non_strings = {k: v for k, v in meta.items() if not isinstance(v, str)}
    # Assert
    assert non_strings == {}


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
# _register_tools — thin re-export wrapper delegation
#
# The a2a_* tool surface lives in ``_channel_tools`` (see
# ``test__channel_tools.py`` for its full coverage). ``channel`` keeps a
# ``_register_tools`` wrapper preserving the historical import path; this
# test pins that the wrapper actually wires the tools onto the server.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_tools_wrapper_delegates_to_channel_tools(fake_listen):
    # Arrange — a structural recorder for the MCP decoration contract.
    class _Rec:
        def __init__(self):
            self.list_tools_fn = None

        def list_tools(self):
            def _d(fn):
                self.list_tools_fn = fn
                return fn

            return _d

        def call_tool(self):
            def _d(fn):
                return fn

            return _d

    rec = _Rec()
    # Act — the wrapper must register the tool surface on the recorder.
    _register_tools(
        rec, agent_name="alice", listen_url=fake_listen.base_url, bearer=None
    )
    tools = await rec.list_tools_fn()
    # Assert — wiring reached _channel_tools.register_tools.
    assert {t.name for t in tools if isinstance(t, Tool)} == {
        "a2a_send",
        "a2a_reply",
        "a2a_ack",
        "a2a_peers",
        "a2a_inbox",
    }


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


# ---------------------------------------------------------------------------
# _serve — receive→inject seam, end-to-end over real MCP memory streams.
#
# This is the path a ``server._session`` lookup silently dropped: the
# inbox SSE consumer must push each event to the connected client as a
# ``notifications/claude/channel`` message. The earlier tests covered
# the projection (_build_notification) and the SSE consumer in
# isolation but never asserted that an event actually reaches a client
# through the live session — so the drop shipped green. This test
# closes that gap: a real event flows fake_listen → _serve's owned
# ServerSession → the client stream, and we assert it arrives.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_pushes_sse_event_to_client_as_channel_notification(
    fake_listen,
):
    # Arrange — fake listen will emit one inbox event on SSE connect.
    import anyio
    from mcp.shared.memory import create_client_server_memory_streams

    from scitex_agent_container._mcp.channel import _serve

    fake_listen.sse_events = [
        {"from_agent": "bob", "content": "hello channel", "msg_id": "m-e2e"}
    ]

    got: dict[str, Any] = {}

    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        client_read, _client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:

            async def _run_serve() -> None:
                await _serve(
                    server_read,
                    server_write,
                    name="alice",
                    listen_url=fake_listen.base_url,
                    bearer=None,
                )

            tg.start_soon(_run_serve)

            # Act — read raw frames off the client side until the channel
            # notification surfaces (no ClientSession: the method is a
            # Claude Code extension the stock client would not parse).
            with anyio.move_on_after(5.0):
                async for session_msg in client_read:
                    if isinstance(session_msg, Exception):
                        continue
                    root = session_msg.message.root
                    if getattr(root, "method", None) == "notifications/claude/channel":
                        got["params"] = root.params
                        break

            tg.cancel_scope.cancel()

    # Assert — the event reached the client through the live session.
    assert got.get("params", {}).get("content") == "hello channel"


# ---------------------------------------------------------------------------
# _serve — initialize handshake must advertise the `claude/channel`
# experimental capability.
#
# Distinct seam from the push test above: even when the server *sends*
# notifications/claude/channel, Claude Code drops every one of them if
# the initialize response did not declare `claude/channel` in
# experimental capabilities ("Channel notifications skipped: server did
# not declare claude/channel capability"). The raw-frame push test reads
# without a real client and cannot catch that client-side gate — only a
# real initialize handshake exercises the capability declaration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_initialize_declares_claude_channel_capability(
    fake_listen,
):
    # Arrange — no SSE events needed; we only drive the handshake.
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from scitex_agent_container._mcp.channel import _serve

    fake_listen.sse_events = []
    caps: dict[str, Any] = {}

    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:

            async def _run_serve() -> None:
                await _serve(
                    server_read,
                    server_write,
                    name="alice",
                    listen_url=fake_listen.base_url,
                    bearer=None,
                )

            tg.start_soon(_run_serve)

            # Act — a real client initialize handshake returns the server's
            # advertised capabilities.
            async with ClientSession(client_read, client_write) as client:
                result = await client.initialize()
                caps["experimental"] = result.capabilities.experimental

            tg.cancel_scope.cancel()

    # Assert — the `claude/channel` capability is advertised, so Claude
    # Code will accept the pushed notifications instead of dropping them.
    assert caps["experimental"] == {"claude/channel": {}}


# ---------------------------------------------------------------------------
# Auto-ack — infra-automatic stage-2 "read" receipt.
#
# When the receive-side adapter injects an inbound event into the
# session, it automatically POSTs an ``a2a_ack`` back to the original
# sender — the receiving agent calls nothing. These tests pin the
# enable gate, the loop-guard (acks are never re-acked; no ping-pong),
# the best-effort failure mode, and the end-to-end POST through a real
# fake listen server.
# ---------------------------------------------------------------------------


class _CapturingSession:
    """Real collaborator standing in for an MCP ServerSession.

    Records every ``send_message`` so the injection half of
    ``_push_channel_event`` is observable. Not a mock — plain capture,
    no auto-spec or call assertions.
    """

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_message(self, msg: Any) -> None:
        self.sent.append(msg)


@contextlib.contextmanager
def _env(name: str, value: str | None):
    """Set (or unset) a real ``os.environ`` entry and restore on exit.

    Used in place of the ecosystem-forbidden ``monkeypatch`` fixture
    (STX-NM002): production reads the genuine ``os.environ``, and we put
    a real value there, then put the prior state back.
    """
    sentinel = object()
    prior: Any = os.environ.get(name, sentinel)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if prior is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


def test_auto_ack_enabled_default_on_when_env_unset():
    # Arrange
    from scitex_agent_container._mcp.channel import _auto_ack_enabled

    # Act — with the env var genuinely absent.
    with _env("SAC_CHANNEL_AUTO_ACK", None):
        enabled = _auto_ack_enabled()
    # Assert
    assert enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " Off "])
def test_auto_ack_disabled_by_falsey_env(raw):
    # Arrange
    from scitex_agent_container._mcp.channel import _auto_ack_enabled

    # Act
    with _env("SAC_CHANNEL_AUTO_ACK", raw):
        enabled = _auto_ack_enabled()
    # Assert
    assert enabled is False


def test_should_auto_ack_true_for_normal_inbound_event():
    # Arrange
    from scitex_agent_container._mcp.channel import _should_auto_ack

    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    decision = _should_auto_ack(event)
    # Assert
    assert decision is True


def test_should_auto_ack_false_for_event_that_is_itself_an_ack():
    """Loop-guard: an auto-ack is itself a message; re-acking it would
    ping-pong forever. An event carrying ``ack`` truthy must be skipped."""
    # Arrange
    from scitex_agent_container._mcp.channel import _should_auto_ack

    event = {"from_agent": "bob", "content": "", "msg_id": "m1", "ack": True}
    # Act
    decision = _should_auto_ack(event)
    # Assert
    assert decision is False


def test_should_auto_ack_false_when_sender_missing():
    # Arrange — no from_agent: nowhere to send the receipt.
    from scitex_agent_container._mcp.channel import _should_auto_ack

    event = {"content": "x", "msg_id": "m1"}
    # Act
    decision = _should_auto_ack(event)
    # Assert
    assert decision is False


@pytest.mark.asyncio
async def test_push_channel_event_still_injects_notification(fake_listen):
    """Existing push behavior is preserved: the notification reaches the
    session regardless of the auto-ack side-effect."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert — the channel notification was sent through the session.
    assert len(session.sent) == 1


@pytest.mark.asyncio
async def test_push_channel_event_auto_acks_to_sender_path(fake_listen):
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    paths = [p for p, _ in fake_listen.posts]
    # Assert — the receipt went back to the original sender's send path.
    assert "/agents/bob/message:send" in paths


@pytest.mark.asyncio
async def test_push_channel_event_auto_ack_carries_ack_marker(fake_listen):
    """The auto-ack must stamp ``ack=True`` — that flag IS the loop-guard
    marker the receiving adapter checks before re-acking."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    _, payload = fake_listen.posts[-1]
    # Assert
    assert payload["params"]["metadata"]["ack"] is True


@pytest.mark.asyncio
async def test_push_channel_event_auto_ack_references_original_msg_id(fake_listen):
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    _, payload = fake_listen.posts[-1]
    # Assert
    assert payload["params"]["metadata"]["in_reply_to"] == "m1"


@pytest.mark.asyncio
async def test_push_channel_event_does_not_auto_ack_an_ack(fake_listen):
    """Loop-guard end-to-end: injecting an inbound event that is itself
    an ack must NOT generate another ack POST."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    ack_event = {"from_agent": "bob", "content": "", "msg_id": "m1", "ack": True}
    # Act
    await _push_channel_event(
        session,
        ack_event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert — zero POSTs: an ack does not beget an ack.
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_two_adapters_do_not_ping_pong_to_a_fixed_point(fake_listen):
    """Drive the full bounce: adapter A injects B's message → auto-acks B;
    then feed A's ack into adapter B's inject path. B must NOT ack back, so
    the exchange terminates after exactly one ack rather than diverging."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session_a = _CapturingSession()
    session_b = _CapturingSession()
    inbound_to_a = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act — A receives B's message and auto-acks (1 POST expected).
    await _push_channel_event(
        session_a,
        inbound_to_a,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    _, ack_payload = fake_listen.posts[-1]
    # Reconstruct the bus event B's adapter would see from A's ack POST.
    ack_meta = ack_payload["params"]["metadata"]
    inbound_to_b = {
        "from_agent": ack_meta["from_agent"],
        "content": "",
        "msg_id": "m2",
        "ack": ack_meta.get("ack"),
    }
    # B injects A's ack — the loop-guard must stop here.
    await _push_channel_event(
        session_b,
        inbound_to_b,
        agent_name="bob",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert — exactly one ack total; B did not bounce another back.
    assert len(fake_listen.posts) == 1


@pytest.mark.asyncio
async def test_auto_ack_disabled_via_env_emits_no_post(fake_listen):
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    with _env("SAC_CHANNEL_AUTO_ACK", "0"):
        await _push_channel_event(
            session,
            event,
            agent_name="alice",
            listen_url=fake_listen.base_url,
            bearer=None,
        )
    # Assert — injection happened, but no auto-ack POST.
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_push_channel_event_skips_auto_ack_without_send_config(fake_listen):
    """Backward-compat: called without agent_name/listen_url (the old
    2-arg signature path) injects but cannot auto-ack — no POST, no crash."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act
    await _push_channel_event(session, event)
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_auto_ack_failure_is_best_effort_does_not_raise():
    """A failed auto-ack POST must be swallowed-with-a-loud-log, never
    re-raised: it can neither block injection nor kill the SSE consumer.
    Point the adapter at a refused port to force the failure."""
    # Arrange — a closed loopback port (bind then close) refuses connect.
    import socket

    from scitex_agent_container._mcp.channel import _push_channel_event

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    refused_port = s.getsockname()[1]
    s.close()
    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act — must complete without raising despite the ack POST failing.
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=f"http://127.0.0.1:{refused_port}",
        bearer=None,
    )
    # Assert — injection still succeeded (the failure was contained).
    assert len(session.sent) == 1
