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


def test_build_notification_source_is_the_fixed_system_identity():
    # Arrange — source must be "sac" regardless of who sent the message
    # (operator directive 2026-07-09: source names the adapter, not the
    # sender; matches cct's source="cct" / scitex-todo's source="stodo").
    event = {"from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["source"] == "sac"


def test_build_notification_source_ignores_missing_from_agent():
    # Arrange
    event = {"content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert — source is fixed; a missing sender does not change it.
    assert meta["source"] == "sac"


def test_build_notification_from_agent_carries_the_sender():
    # Arrange
    event = {"from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert — the sender's own identity moved here, not into source.
    assert meta["from_agent"] == "bob"


def test_build_notification_missing_from_agent_marks_unknown():
    # Arrange
    event = {"content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["from_agent"] == "unknown"


def test_build_notification_source_honours_env_override():
    # Arrange
    event = {"from_agent": "bob", "content": "x"}
    saved = os.environ.get("SAC_MCP_CHANNEL_SOURCE")
    os.environ["SAC_MCP_CHANNEL_SOURCE"] = "custom-label"
    try:
        # Act
        meta = _build_notification(event)["meta"]
    finally:
        if saved is None:
            os.environ.pop("SAC_MCP_CHANNEL_SOURCE", None)
        else:
            os.environ["SAC_MCP_CHANNEL_SOURCE"] = saved
    # Assert
    assert meta["source"] == "custom-label"


def test_build_notification_carries_msg_id():
    # Arrange
    event = {"msg_id": "abc123", "from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["msg_id"] == "abc123"


def test_build_notification_ts_renders_iso8601_utc_from_unix_seconds():
    # Arrange — 1_700_000_000 is 2023-11-14T22:13:20Z (no surprise epoch).
    event = {"ts": 1_700_000_000, "from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert — exact-round-trip: the rendered form is the canonical
    # ISO-8601 UTC string the formatter emits.
    assert meta["ts"] == "2023-11-14T22:13:20Z"


def test_build_notification_ts_matches_iso8601_shape():
    """Regression: channel-push timestamps must render as ISO-8601 (the
    operator-greenlit format fix). The old behaviour stringified the
    bus's raw unix-seconds float (e.g. ``"1234"``) which receiving
    sessions saw as an unreadable number in the ``<channel ts=...>``
    tag. Pin the rendered shape so a future regression to ``str(ts)``
    fails loudly here instead of silently degrading the display."""
    import re

    # Arrange — float ts (the actual bus type, see mint_event).
    event = {"ts": 1_777_766_006.95, "from_agent": "bob", "content": "x"}
    # Act
    rendered = _build_notification(event)["meta"]["ts"]
    # Assert — basic ISO-8601 shape (date 'T' time, optional fractional
    # seconds, optional timezone suffix).
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$",
        rendered,
    ), rendered


def test_build_notification_ts_empty_when_absent():
    """Missing ``ts`` stays empty — no surprise 1970 epoch."""
    # Arrange
    event = {"from_agent": "bob", "content": "x"}
    # Act
    meta = _build_notification(event)["meta"]
    # Assert
    assert meta["ts"] == ""


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
        # #16 — every outbound a2a message carries the sender's account
        # + live quota as STRUCTURED metadata. The receive-side
        # _build_notification must surface those fields onto the
        # <channel meta.*> tag so the receiving agent (and the human
        # reading the rendered tag) can see "this came from `ywatanabe`
        # at 5h:19% / 7d:3% / TTL=7.7h" without parsing free-form text.
        ("account", "ywatanabe", "ywatanabe"),
        ("used_pct_5h", 19.0, "19.0"),
        ("used_pct_7d", 3.0, "3.0"),
        ("token_ttl_hours", 7.74, "7.74"),
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
async def test_consume_sse_retries_after_connection_error(dead_port):
    """A port bound but never listened on forces a refused connection. The
    consumer must log + retry rather than crash."""
    # Arrange — the port is HELD for the whole test, so nothing can move in
    # behind us and turn this "refused" endpoint into a live one.
    url = dead_port.url("/agents/x/inbox/stream")
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
@pytest.mark.parametrize("blank_content", ["", "   ", "\n\t"])
async def test_push_channel_event_skips_notification_for_blank_content(
    fake_listen, blank_content
):
    """Bug 1 (sac-fleet-ux-misc-2026-06-24): an empty/whitespace-only
    content must not push a notification -- it used to render as a bare
    "<- sac:" line with nothing after it. Distinct from the wake-loop /
    auto-ack logic covered elsewhere in this module."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": blank_content, "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert — no notification was pushed for blank content.
    assert session.sent == []


def _contentless_ack_posts(fake_listen) -> list:
    """Filter the fake listen's posts down to contentless legacy acks.

    A "contentless ack" carries ``metadata.ack=True`` AND an empty (or
    missing) text body. The structural reaction-ack
    (``feat/comm-reaction-ack``) is excluded from this filter — it
    carries a non-empty 👀 marker so the sender-side noise filter
    deliberately lets it pass.
    """
    out = []
    for path, payload in fake_listen.posts:
        if not isinstance(payload, dict):
            continue
        params = payload.get("params") or {}
        metadata = params.get("metadata") or {}
        if not metadata.get("ack"):
            continue
        message = params.get("message") or {}
        parts = message.get("parts") or []
        text = ""
        if isinstance(parts, list) and parts and isinstance(parts[0], dict):
            text = parts[0].get("text", "") or ""
        if isinstance(text, str) and text.strip() == "":
            out.append((path, payload))
    return out


@pytest.mark.asyncio
async def test_push_channel_event_auto_ack_is_suppressed_at_sender(fake_listen):
    """Operator contract: the stage-2 read-receipt auto-ack is contentless
    (empty body + ``metadata.ack=True``), so the sender-side noise filter
    drops it BEFORE it leaves the outbound queue. The wire stays quiet for
    contentless acks — the structural reaction-ack
    (``feat/comm-reaction-ack``) carries a 👀 marker and is the operator's
    comm-miss-detectable signal; it is NOT suppressed (and is asserted
    elsewhere). Pre-filter contentless behaviour (a POST to
    ``/agents/bob/message:send``) is replaced by zero contentless POSTs."""
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
    # Assert — no CONTENTLESS auto-ack reached the listen. The structural
    # reaction-ack (non-empty 👀 marker) is a separate, intentional signal.
    assert _contentless_ack_posts(fake_listen) == []


@pytest.mark.asyncio
async def test_push_channel_event_still_injects_when_ack_suppressed(fake_listen):
    """Suppressing the auto-ack must not block the primary injection: the
    inbound event still reaches the session as a channel notification."""
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
    # Assert — the notification was delivered through the session.
    assert len(session.sent) == 1


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
async def test_two_adapters_emit_zero_contentless_acks_under_sender_side_filter(
    fake_listen,
):
    """End-to-end ping-pong cannot start under the sender-side filter: A's
    would-be CONTENTLESS auto-ack to B is dropped at A before it reaches
    the wire, so there is nothing for B's adapter to bounce back.
    Pre-filter behaviour (exactly one contentless ack on the wire) is
    replaced by zero contentless acks on the wire. The structural
    reaction-ack (``feat/comm-reaction-ack``) is a separate, intentional
    signal carrying a 👀 marker — it DOES reach the wire so the sender can
    detect comm-miss; the receive-side loop-guard on ``kind="reaction"``
    prevents the ping-pong (covered by the e2e module)."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session_a = _CapturingSession()
    inbound_to_a = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act — A receives B's message; the contentless ack is suppressed at A.
    await _push_channel_event(
        session_a,
        inbound_to_a,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert — no CONTENTLESS ack reached the wire.
    assert _contentless_ack_posts(fake_listen) == []


@pytest.mark.asyncio
async def test_auto_ack_disabled_via_env_emits_no_contentless_post(fake_listen):
    """Disabling the legacy contentless auto-ack via env: no contentless
    POST reaches the wire. The structural reaction-ack
    (``feat/comm-reaction-ack``) is gated by a SEPARATE env knob
    (``SAC_REACTION_ACK``) and remains on by default — the operator's
    comm-miss-detectable signal is intentional and asserted elsewhere."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act — auto-ack OFF; reaction-ack default (ON).
    with _env("SAC_CHANNEL_AUTO_ACK", "0"):
        await _push_channel_event(
            session,
            event,
            agent_name="alice",
            listen_url=fake_listen.base_url,
            bearer=None,
        )
    # Assert — injection happened, but no contentless auto-ack POST.
    assert _contentless_ack_posts(fake_listen) == []


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
async def test_auto_ack_failure_is_best_effort_does_not_raise(dead_port):
    """A failed auto-ack POST must be swallowed-with-a-loud-log, never
    re-raised: it can neither block injection nor kill the SSE consumer.
    Point the adapter at a refused port to force the failure."""
    # Arrange — a HELD, never-listened loopback port refuses connect.
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act — must complete without raising despite the ack POST failing.
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=dead_port.url(""),
        bearer=None,
    )
    # Assert — injection still succeeded (the failure was contained).
    assert len(session.sent) == 1


# ---------------------------------------------------------------------------
# WI-1 wake-on-push — a pushed message to an IDLE agent must DRIVE a turn.
#
# The notification-only push (covered above) renders a ``<channel>`` tag for
# an already-active turn but does NOT advance an idle session. When a
# ``turn_url`` (the agent's own colocated ``/v1/turn``) is configured, the
# adapter POSTs each qualifying event there so the runner enqueues it onto
# the persistent SDK conversation and processes it immediately — push behaves
# like the lead's Telegram channel. These tests pin: a real POST reaches the
# turn endpoint, acks/empty events do NOT drive a turn, and the wake path
# delivers exactly once (no duplicate notification).
# ---------------------------------------------------------------------------


class _FakeTurnServer:
    """A real asyncio TCP server standing in for the agent's ``/v1/turn``.

    Records every POST body so the wake path is directly observable. Not a
    mock — a genuine loopback HTTP/1.1 endpoint, same approach as
    ``_FakeListenServer``.
    """

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []
        self._server: asyncio.base_events.Server | None = None
        self.host = "127.0.0.1"
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host=self.host, port=0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def turn_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1/turn"

    async def _handle(self, reader, writer) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
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
            try:
                payload = json.loads(body.decode() or "{}")
            except json.JSONDecodeError:
                payload = {}
            self.turns.append(payload)
            resp = json.dumps({"text": "ok", "session_id": "s1"}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(resp)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + resp
            )
            await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


@pytest_asyncio.fixture
async def fake_turn():
    s = _FakeTurnServer()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


def test_should_wake_turn_true_for_normal_inbound_event():
    # Arrange
    from scitex_agent_container._mcp.channel import _should_wake_turn

    event = {"from_agent": "bob", "content": "do the thing", "msg_id": "m1"}
    # Act
    decision = _should_wake_turn(event)
    # Assert
    assert decision is True


def test_should_wake_turn_false_for_ack_event():
    """An ack carries no actionable content — driving a turn per receipt
    would burn turns and risk an auto-ack ping-pong."""
    # Arrange
    from scitex_agent_container._mcp.channel import _should_wake_turn

    event = {"from_agent": "bob", "content": "", "msg_id": "m1", "ack": True}
    # Act
    decision = _should_wake_turn(event)
    # Assert
    assert decision is False


def test_should_wake_turn_false_for_empty_content():
    # Arrange
    from scitex_agent_container._mcp.channel import _should_wake_turn

    event = {"from_agent": "bob", "content": "   ", "msg_id": "m1"}
    # Act
    decision = _should_wake_turn(event)
    # Assert
    assert decision is False


def test_should_wake_turn_false_for_completion_report():
    """A completion report informs the requester that work it already asked
    for is done — it is not a new request. Waking a turn on it restarts the
    cycle and two peers ping-pong forever (neurovista ⇆ scitex-writer,
    2026-06-24). It must DELIVER but not DRIVE a turn."""
    # Arrange
    from scitex_agent_container._mcp.channel import _should_wake_turn

    event = {
        "from_agent": "bob",
        "content": '{"agent": "bob", "status": "success"}',
        "msg_id": "m1",
        "kind": "completion",
    }
    # Act
    decision = _should_wake_turn(event)
    # Assert
    assert decision is False


def test_should_wake_turn_false_for_reaction_receipt():
    # Arrange
    from scitex_agent_container._mcp.channel import _should_wake_turn

    event = {
        "from_agent": "bob",
        "content": "👀",
        "msg_id": "m1",
        "kind": "reaction",
    }
    # Act
    decision = _should_wake_turn(event)
    # Assert
    assert decision is False


def test_wake_text_frames_content_with_source_and_msg_id():
    # Arrange
    from scitex_agent_container._mcp.channel import _wake_text

    event = {"from_agent": "bob", "content": "hello", "msg_id": "m1"}
    # Act
    text = _wake_text(event)
    # Assert — the sender attribution survives into the driven turn input.
    assert 'source="bob"' in text and "hello" in text


@pytest.mark.asyncio
async def test_push_with_turn_url_drives_a_turn(fake_turn):
    """The wake POST reaches the agent's own /v1/turn — the core WI-1 claim:
    a pushed message to an idle agent advances its turn without any external
    turn trigger."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "summarize commits", "msg_id": "m1"}
    # Act — turn_url set: the adapter must drive a turn.
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url="http://127.0.0.1:1",  # unused on the wake path
        bearer=None,
        turn_url=fake_turn.turn_url,
    )
    # Assert — exactly one turn was driven on the runner's endpoint.
    assert len(fake_turn.turns) == 1


@pytest.mark.asyncio
async def test_push_with_turn_url_carries_message_content_as_turn_text(fake_turn):
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "summarize commits", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url="http://127.0.0.1:1",
        bearer=None,
        turn_url=fake_turn.turn_url,
    )
    # Assert — the driven turn carries the original message body.
    assert "summarize commits" in fake_turn.turns[0]["text"]


@pytest.mark.asyncio
async def test_push_with_turn_url_skips_duplicate_notification(fake_turn):
    """Wake delivers the message as turn input; the notification push is
    skipped so the agent does not see the same message twice."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "do it", "msg_id": "m1"}
    # Act
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url="http://127.0.0.1:1",
        bearer=None,
        turn_url=fake_turn.turn_url,
    )
    # Assert — no notification was injected (turn input is the sole delivery).
    assert session.sent == []


@pytest.mark.asyncio
async def test_push_with_turn_url_ack_event_does_not_drive_turn(fake_turn):
    """An ack event must NOT wake a turn — it falls back to notification-only
    so the loop-guard / receipt semantics are unchanged."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    ack_event = {"from_agent": "bob", "content": "", "msg_id": "m1", "ack": True}
    # Act
    await _push_channel_event(
        session,
        ack_event,
        agent_name="alice",
        listen_url="http://127.0.0.1:1",
        bearer=None,
        turn_url=fake_turn.turn_url,
    )
    # Assert — zero turns driven for an ack.
    assert fake_turn.turns == []


@pytest.mark.asyncio
async def test_push_without_turn_url_falls_back_to_notification(fake_listen):
    """Backward-compat: with no turn_url (external node, no colocated
    runner) the adapter still pushes the channel notification."""
    # Arrange
    from scitex_agent_container._mcp.channel import _push_channel_event

    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}
    # Act — no turn_url.
    await _push_channel_event(
        session,
        event,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    # Assert — notification injected as before.
    assert len(session.sent) == 1


@pytest.mark.asyncio
async def test_wake_failure_propagates_to_caller(dead_port):
    """WI-2 fail-loud: when the wake POST cannot reach the runner (refused
    connection), ``_push_channel_event`` must RAISE rather than silently
    pretend the message was delivered."""
    # Arrange — a HELD, never-listened loopback port refuses connect. Holding
    # it matters here: if anything bound the port the wake would SUCCEED and
    # this fail-loud test would assert the opposite of what it means to.
    from scitex_agent_container._mcp.channel import _push_channel_event

    refused_port = dead_port()
    session = _CapturingSession()
    event = {"from_agent": "bob", "content": "hi", "msg_id": "m1"}

    async def _do() -> None:
        await _push_channel_event(
            session,
            event,
            agent_name="alice",
            listen_url="http://127.0.0.1:1",
            bearer=None,
            turn_url=f"http://127.0.0.1:{refused_port}/v1/turn",
        )

    # Act
    raised = False
    try:
        await _do()
    except Exception:
        raised = True
    # Assert — the unreachable wake surfaced loudly, not silently dropped.
    assert raised is True


# ---------------------------------------------------------------------------
# Auto-ack rate limiter (`_auto_ack_rate_allow`) — belt-and-suspenders loop
# breaker. Pure sync function; deterministic via the injectable ``now``. No
# sleeps, no mocks. Module-level window/latch state is reset in each Arrange.
# ---------------------------------------------------------------------------


@pytest.fixture
def _ack_rate_max_two():
    """Lower the auto-ack cap to 2 via env (yield save/restore, no monkeypatch)."""
    key = "SAC_AUTO_ACK_RATE_MAX"
    saved = os.environ.get(key)
    os.environ[key] = "2"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def test_auto_ack_rate_allows_calls_within_default_budget():
    """Up to the default cap (20) within the window are all permitted."""
    # Arrange
    channel_mod._auto_ack_window.clear()
    channel_mod._auto_ack_tripped.clear()
    # Act
    results = [
        channel_mod._auto_ack_rate_allow("peer", now=float(i)) for i in range(20)
    ]
    # Assert
    assert all(results) is True


def test_auto_ack_rate_blocks_calls_over_default_budget():
    """The 21st auto-ack to one sender within the window is refused."""
    # Arrange
    channel_mod._auto_ack_window.clear()
    channel_mod._auto_ack_tripped.clear()
    for i in range(20):
        channel_mod._auto_ack_rate_allow("peer", now=float(i))
    # Act
    over = channel_mod._auto_ack_rate_allow("peer", now=19.5)
    # Assert
    assert over is False


def test_auto_ack_rate_resumes_after_window_clears():
    """Once the window passes, emission to the same sender resumes."""
    # Arrange
    channel_mod._auto_ack_window.clear()
    channel_mod._auto_ack_tripped.clear()
    for i in range(20):
        channel_mod._auto_ack_rate_allow("peer", now=float(i))
    # Act — far past the 60s default window, the old timestamps drop out.
    after = channel_mod._auto_ack_rate_allow("peer", now=200.0)
    # Assert
    assert after is True


def test_auto_ack_rate_env_override_lowers_cap(_ack_rate_max_two):
    """``SAC_AUTO_ACK_RATE_MAX=2`` makes the 3rd call refuse."""
    # Arrange
    channel_mod._auto_ack_window.clear()
    channel_mod._auto_ack_tripped.clear()
    channel_mod._auto_ack_rate_allow("peer", now=0.0)
    channel_mod._auto_ack_rate_allow("peer", now=1.0)
    # Act
    third = channel_mod._auto_ack_rate_allow("peer", now=2.0)
    # Assert
    assert third is False
