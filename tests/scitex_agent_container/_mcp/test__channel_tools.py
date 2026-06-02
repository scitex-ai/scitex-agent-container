"""Tests for the sac MCP channel **send-side** ``a2a_*`` tool surface
(``_mcp._channel_tools``).

This module hosts the tools the agent calls explicitly — ``a2a_send``,
``a2a_reply``, ``a2a_ack``, ``a2a_peers``, ``a2a_inbox`` — extracted from
``_mcp.channel`` (which kept the receive-side adapter). The receive-side
push + auto-ack path is covered by ``test_channel.py``.

Per the "no cut corners" principle these tests use **real** asyncio +
real ``httpx`` + a real ``asyncio.start_server``-backed HTTP/1.1 server
on loopback. No mocks, no monkeypatch — the only test double is an
explicit ``_ToolRecorder`` mirroring the MCP ``@server.list_tools()`` /
``@server.call_tool()`` registration contract (a real collaborator).

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

from scitex_agent_container._mcp._channel_tools import register_tools  # noqa: E402
from scitex_agent_container._mcp.channel import _recent  # noqa: E402

# ---------------------------------------------------------------------------
# Real in-process HTTP/1.1 + JSON server (no aiohttp dependency).
# ---------------------------------------------------------------------------


class _FakeListenServer:
    """A real asyncio TCP server speaking minimal HTTP/1.1 for tests."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.peers_payload: dict[str, Any] = {"agents": []}
        self.last_auth: str | None = None
        self._server: asyncio.base_events.Server | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        # Response the fake returns for a successful ``message:send`` POST.
        # Default mirrors the historical fake (200 + ``{"ok": True}``). WI-2
        # tests override this to model a real publish reply carrying
        # ``delivered_subscriber_count``.
        self.send_response: dict[str, Any] = {"ok": True}
        # When set, ``message:send`` returns this HTTP status with a tiny
        # body instead of a 200 JSON envelope (models a delivery error).
        self.send_status: int = 200

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
            body = b""
            if content_length:
                body = await reader.readexactly(content_length)

            if method == "GET" and path.rstrip("/") == "/agents":
                await self._serve_json(writer, self.peers_payload)
            elif method == "POST" and "/message:send" in path:
                try:
                    payload = json.loads(body.decode() or "{}")
                except json.JSONDecodeError:
                    payload = {}
                self.posts.append((path, payload))
                if self.send_status != 200:
                    await self._serve_status(
                        writer, self.send_status, b"delivery error"
                    )
                else:
                    await self._serve_json(writer, self.send_response)
            else:
                await self._serve_status(writer, 404, b"not found")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
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

    Mirrors the MCP server contract closely enough that ``register_tools``
    runs unchanged, and the captured callables can be invoked directly.
    Not a mock — just attribute storage.
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
def registered_tools(fake_listen):
    """Register tools against a fresh recorder, pointing at the fake."""
    rec = _ToolRecorder()
    register_tools(
        rec,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer=None,
    )
    return rec


@pytest.fixture(autouse=True)
def _clear_recent_ring():
    """Each test sees an empty inbox ring buffer."""
    _recent.clear()
    yield
    _recent.clear()


# ---------------------------------------------------------------------------
# list_tools surface
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
# call_tool dispatch (real HTTP to fake_listen)
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


# ---------------------------------------------------------------------------
# #16 PART 3 — every outbound a2a payload carries the sender's account +
# live quota as STRUCTURED metadata. Read at SEND time from the bound
# quota-cache.json (via _account.quota_cache.build_a2a_metadata).
# ---------------------------------------------------------------------------


def _write_quota_fixture(tmp_path, short: str = "alice", h5=42.0, d7=7.0, ttl_h=3.5):
    """Drop a minimal quota-cache.json containing one matching entry."""
    p = tmp_path / "quota-cache.json"
    p.write_text(
        json.dumps(
            {
                "written_at": 1.0,
                "accounts": {
                    f"{short}@gmail.com": {
                        "short": short,
                        "h5": h5,
                        "d7": d7,
                        "ttl_h": ttl_h,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,expected",
    [
        ("account", "alice"),
        ("used_pct_5h", 42.0),
        ("used_pct_7d", 7.0),
        ("token_ttl_hours", 3.5),
    ],
)
async def test_call_tool_a2a_send_metadata_includes_account_quota_field(
    registered_tools: _ToolRecorder,
    fake_listen,
    tmp_path,
    env_save_restore,
    key: str,
    expected,
):
    # Arrange — point the shared reader at a fixture matching the test's
    # agent_name ("alice", per the registered_tools fixture). One test
    # per wire-field name pins each field to operator's #16 contract.
    fixture = _write_quota_fixture(tmp_path, short="alice", h5=42.0, d7=7.0, ttl_h=3.5)
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", str(fixture))
    env_save_restore.set("CLAUDE_AGENT_ACCOUNT", "alice-gmail-com")
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    _, payload = fake_listen.posts[-1]
    metadata = payload["params"]["metadata"]
    # Assert
    assert (
        metadata[key] == pytest.approx(expected)
        if isinstance(expected, float)
        else metadata[key] == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "absent_key",
    ["account", "used_pct_5h", "used_pct_7d", "token_ttl_hours"],
)
async def test_call_tool_a2a_send_metadata_omits_quota_key_when_unavailable(
    registered_tools: _ToolRecorder,
    fake_listen,
    env_save_restore,
    absent_key: str,
):
    # Arrange — no account env, no cache file → graceful empty merge.
    env_save_restore.delete("CLAUDE_AGENT_ACCOUNT")
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", "/nonexistent/quota-cache.json")
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    _, payload = fake_listen.posts[-1]
    metadata = payload["params"]["metadata"]
    # Assert
    assert absent_key not in metadata


@pytest.mark.asyncio
async def test_call_tool_a2a_send_keeps_from_agent_when_quota_unavailable(
    registered_tools: _ToolRecorder,
    fake_listen,
    env_save_restore,
):
    # Arrange — quota fields drop out, but the from_agent invariant
    # (pre-#16 contract) must survive every merge order.
    env_save_restore.delete("CLAUDE_AGENT_ACCOUNT")
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", "/nonexistent/quota-cache.json")
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    _, payload = fake_listen.posts[-1]
    metadata = payload["params"]["metadata"]
    # Assert
    assert metadata["from_agent"] == "alice"


@pytest.mark.asyncio
async def test_call_tool_a2a_reply_metadata_includes_account(
    registered_tools: _ToolRecorder,
    fake_listen,
    tmp_path,
    env_save_restore,
):
    # Arrange — replies go through the same wrapper, so they MUST carry
    # the same metadata (peer-side back-pressure has to see ALL traffic).
    fixture = _write_quota_fixture(tmp_path, short="alice", h5=11.0, d7=2.0)
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", str(fixture))
    env_save_restore.set("CLAUDE_AGENT_ACCOUNT", "alice-gmail-com")
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c-orig"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_reply", {"in_reply_to": "m1", "content": "y"})
    _, payload = fake_listen.posts[-1]
    metadata = payload["params"]["metadata"]
    # Assert
    assert metadata["account"] == "alice"


@pytest.mark.asyncio
async def test_call_tool_a2a_reply_metadata_includes_used_pct_5h(
    registered_tools: _ToolRecorder,
    fake_listen,
    tmp_path,
    env_save_restore,
):
    # Arrange
    fixture = _write_quota_fixture(tmp_path, short="alice", h5=11.0, d7=2.0)
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", str(fixture))
    env_save_restore.set("CLAUDE_AGENT_ACCOUNT", "alice-gmail-com")
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c-orig"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_reply", {"in_reply_to": "m1", "content": "y"})
    _, payload = fake_listen.posts[-1]
    metadata = payload["params"]["metadata"]
    # Assert
    assert metadata["used_pct_5h"] == pytest.approx(11.0)


# NB: a2a_ack is intentionally suppressed at the sender (the sender-side
# noise filter drops empty-content + ack=True payloads — see
# ``test_call_tool_a2a_ack_is_suppressed_at_sender`` below), so there is
# no wire-level metadata to assert on the ack path. The send + reply
# coverage above is sufficient for the operator's #16 contract: every
# message that actually leaves the bridge carries the quota fields.


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
async def test_call_tool_a2a_ack_is_suppressed_at_sender(
    registered_tools: _ToolRecorder, fake_listen
):
    """The ``a2a_ack`` tool always builds an empty-content + ``ack=True``
    payload — the exact shape the sender-side noise filter is designed to
    drop. The HTTP POST must NOT reach the listen (the wire stays quiet).
    """
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c1"})
    call_fn = registered_tools.call_tool_fn
    # Act
    await call_fn("a2a_ack", {"msg_id": "m1"})
    # Assert
    assert fake_listen.posts == []


@pytest.mark.asyncio
async def test_call_tool_a2a_ack_returns_suppression_marker(
    registered_tools: _ToolRecorder, fake_listen
):
    """The dropped ack still returns a structured result to the caller so a
    flow that ``await``ed it can distinguish "suppressed" from "delivered"."""
    # Arrange
    _recent.append({"msg_id": "m1", "from_agent": "carol", "conversation_id": "c1"})
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_ack", {"msg_id": "m1"})
    body = json.loads(out[0].text)
    # Assert
    assert body.get("body", {}).get("suppressed") == "empty_ack"


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


@pytest.mark.asyncio
async def test_register_tools_forwards_bearer_token_on_post(fake_listen):
    # Arrange
    rec = _ToolRecorder()
    register_tools(
        rec,
        agent_name="alice",
        listen_url=fake_listen.base_url,
        bearer="s3cret",
    )
    # Act
    await rec.call_tool_fn("a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert fake_listen.last_auth == "Bearer s3cret"


# ---------------------------------------------------------------------------
# WI-2 fail-loud — a send/push that cannot reach or wake the target must
# surface a loud, explicit error to the caller, never a misleading success.
#
# Three failure modes from the work-item: no inbox subscriber
# (delivered_subscriber_count == 0), agent stopped (connection refused),
# delivery error (non-2xx). Real loopback servers, no mocks.
# ---------------------------------------------------------------------------


def _err(out: "list[TextContent]") -> str | None:
    """Pull the ``error`` field out of a tool result, or None."""
    body = json.loads(out[0].text)
    return body.get("error") if isinstance(body, dict) else None


@pytest.mark.asyncio
async def test_a2a_send_no_subscriber_returns_loud_error(
    registered_tools: _ToolRecorder, fake_listen
):
    """delivered_subscriber_count == 0 → the push woke nobody → loud error,
    not a misleading success body."""
    # Arrange — model a publish reply with zero subscribers.
    fake_listen.send_response = {
        "msg_id": "m1",
        "to_agent": "bob",
        "delivered_subscriber_count": 0,
    }
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert "no live subscriber" in (_err(out) or "")


@pytest.mark.asyncio
async def test_a2a_send_with_subscriber_returns_success(
    registered_tools: _ToolRecorder, fake_listen
):
    """A delivered_subscriber_count >= 1 is a real delivery — success, no
    error (guards against the fail-loud check over-firing)."""
    # Arrange
    fake_listen.send_response = {
        "msg_id": "m1",
        "to_agent": "bob",
        "delivered_subscriber_count": 1,
    }
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert _err(out) is None


@pytest.mark.asyncio
async def test_a2a_send_delivery_error_status_returns_loud_error(
    registered_tools: _ToolRecorder, fake_listen
):
    """A non-2xx from the listen server (e.g. 502 forward failure) is a
    delivery error — surface it loudly, not as a success body."""
    # Arrange
    fake_listen.send_status = 502
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert "HTTP 502" in (_err(out) or "")


@pytest.mark.asyncio
async def test_a2a_send_agent_stopped_connection_refused_returns_loud_error():
    """Target agent down / listen unreachable (refused connection) → loud
    'agent unreachable' error rather than a hang or silent success."""
    # Arrange — register tools pointing at a closed loopback port.
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    refused_port = s.getsockname()[1]
    s.close()
    rec = _ToolRecorder()
    register_tools(
        rec,
        agent_name="alice",
        listen_url=f"http://127.0.0.1:{refused_port}",
        bearer=None,
    )
    # Act
    out = await rec.call_tool_fn("a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert "unreachable" in (_err(out) or "")


@pytest.mark.asyncio
async def test_a2a_send_absent_delivered_count_is_not_treated_as_failure(
    registered_tools: _ToolRecorder, fake_listen
):
    """A 200 reply WITHOUT delivered_subscriber_count (e.g. a cross-host
    forward) must NOT be flagged as a no-subscriber failure — inventing a
    zero would be a false positive."""
    # Arrange — default fake response is ``{"ok": True}`` (no count field).
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert _err(out) is None


@pytest.mark.asyncio
async def test_a2a_reply_no_subscriber_returns_loud_error(
    registered_tools: _ToolRecorder, fake_listen
):
    """The reply path shares the fail-loud send helper."""
    # Arrange — seed a known inbound message to reply to, then zero subs.
    _recent.append({"msg_id": "orig1", "from_agent": "bob", "conversation_id": "c1"})
    fake_listen.send_response = {"delivered_subscriber_count": 0}
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_reply", {"in_reply_to": "orig1", "content": "hey"})
    # Assert
    assert "no live subscriber" in (_err(out) or "")


@pytest.mark.asyncio
async def test_a2a_ack_is_suppressed_before_subscriber_check(
    registered_tools: _ToolRecorder, fake_listen
):
    """Sender-side noise filter runs *before* the HTTP POST, so the
    no-subscriber check downstream never sees the contentless ack — even
    when the listen would have reported zero subscribers, the ack is
    dropped silently with no loud error (it is noise, not a delivery
    failure)."""
    # Arrange — would-be-zero subscriber response: irrelevant, we never POST.
    _recent.append({"msg_id": "orig2", "from_agent": "bob", "conversation_id": "c2"})
    fake_listen.send_response = {"delivered_subscriber_count": 0}
    call_fn = registered_tools.call_tool_fn
    # Act
    out = await call_fn("a2a_ack", {"msg_id": "orig2"})
    body = json.loads(out[0].text)
    # Assert
    assert _err(out) is None and body.get("body", {}).get("suppressed") == "empty_ack"
