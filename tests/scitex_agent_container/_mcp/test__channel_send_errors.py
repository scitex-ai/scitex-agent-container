"""The FAIL-LOUD contract: a send that reached nobody must FAIL the call.

Why this module exists as its own file
--------------------------------------
``test__channel_tools.py`` already asserted that a 0-subscriber send returns
a body containing an ``error`` key. Those tests passed — and agents silently
swallowed each other's messages anyway.

The gap: a caller does not decide "did my call succeed?" by reading the body.
It reads the MCP protocol's ``isError`` flag. And the low-level server stamps
``isError=False`` on ANY plain ``list[TextContent]`` a handler returns — so
"reached no live subscriber" was arriving inside a result the protocol
classified as SUCCESSFUL. The old tests could not have caught that, because
they invoked the handler directly and never went through the component that
produces the flag.

So every test here drives a REAL ``mcp.server.lowlevel.Server`` against a
REAL loopback HTTP listen. No mocks. The assertions are about what the caller
actually sees.

The three states (and why the middle one is the only failure)
-------------------------------------------------------------
* ``delivered_subscriber_count >= 1`` → delivered. Success.
* ``delivered_subscriber_count == 0``  → DEFINITIVELY not delivered. The bus
  fanned out to nobody. This is evidence, so it fails loudly.
* field ABSENT (e.g. a cross-host forward) → could not determine. Inventing a
  zero would be a false accusation of non-delivery, so it does NOT fail.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("mcp.types")  # gates the module on `mcp`

from scitex_agent_container._mcp._channel_send_errors import (  # noqa: E402
    ERR_NO_SUBSCRIBER,
    ERR_UNREACHABLE,
)
from scitex_agent_container._mcp._channel_tools import register_tools  # noqa: E402


# ---------------------------------------------------------------------------
# A real loopback listen. Speaks just enough HTTP/1.1 to answer message:send
# with a configurable publish reply — the same shape sac listen returns from
# ``node_message_send``.
# ---------------------------------------------------------------------------


class _FakeListen:
    """Real asyncio TCP server answering ``POST /agents/<n>/message:send``."""

    def __init__(self) -> None:
        self.send_response: dict[str, Any] = {"ok": True}
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if not await reader.readline():
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
            body = json.dumps(self.send_response).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
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
async def listen():
    server = _FakeListen()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _call(listen_url: str, tool: str, args: dict[str, Any]):
    """Invoke ``tool`` through a REAL MCP Server; return its CallToolResult.

    This is the caller's-eye view. The low-level server is what turns a
    handler's return value into the ``CallToolResult`` (and its ``isError``
    flag) that the calling model receives — so it is the only place the
    swallowed-message bug was ever observable.
    """
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server = Server(name="sac-channel-test")
    register_tools(server, agent_name="alice", listen_url=listen_url, bearer=None)
    handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=tool, arguments=args),
    )
    return (await handler(request)).root


def _body(result) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def _refused_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


# ---------------------------------------------------------------------------
# THE BUG: 0 subscribers came back as a SUCCESSFUL tool call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_subscriber_send_is_an_mcp_error(listen):
    """delivered_subscriber_count == 0 → the caller must see a FAILED call,
    not a success whose body merely mentions an error."""
    # Arrange — the listen publishes to nobody.
    listen.send_response = {"msg_id": "m1", "delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert result.isError is True


@pytest.mark.asyncio
async def test_delivered_send_is_not_an_mcp_error(listen):
    """Guard against the fail-loud check over-firing: >= 1 subscriber is a
    real delivery and must stay a plain success."""
    # Arrange
    listen.send_response = {"msg_id": "m1", "delivered_subscriber_count": 1}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert result.isError is False


@pytest.mark.asyncio
async def test_absent_subscriber_count_is_not_an_mcp_error(listen):
    """Three states, not two. An ABSENT count (a cross-host forward that does
    not report one) is "could not determine" — it must NOT be inferred as zero
    and failed. Absence of evidence is not evidence of non-delivery."""
    # Arrange — a 200 carrying no delivered_subscriber_count at all.
    listen.send_response = {"ok": True}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert result.isError is False


@pytest.mark.asyncio
async def test_unreachable_listen_is_an_mcp_error():
    """Connection refused is a demonstrable non-delivery — also a caller-
    visible failure, not a quietly-swallowed one."""
    # Arrange — nothing is listening on this port.
    url = f"http://127.0.0.1:{_refused_port()}"
    # Act
    result = await _call(url, "a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert result.isError is True


@pytest.mark.asyncio
async def test_reply_to_unknown_msg_id_is_an_mcp_error(listen):
    """A reply that resolved no recipient delivered nothing either."""
    # Arrange — nothing in the inbox ring, so this msg_id resolves to no sender.
    unknown_msg_id = "ghost"
    # Act
    result = await _call(
        listen.base_url, "a2a_reply", {"in_reply_to": unknown_msg_id, "content": "x"}
    )
    # Assert
    assert result.isError is True


# ---------------------------------------------------------------------------
# The failure must stay ACTIONABLE — loud is not enough if it strands the
# caller. The detail (who, how many, what now) survives the fail-loud change.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_subscriber_error_names_the_target(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["target"] == "bob"


@pytest.mark.asyncio
async def test_no_subscriber_error_carries_machine_readable_code(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["code"] == ERR_NO_SUBSCRIBER


@pytest.mark.asyncio
async def test_no_subscriber_error_reports_the_subscriber_count(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["delivered_subscriber_count"] == 0


@pytest.mark.asyncio
async def test_no_subscriber_error_states_delivered_false(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["delivered"] is False


@pytest.mark.asyncio
async def test_no_subscriber_error_reports_the_message_as_durably_queued(listen):
    """sac listen persists to ``channel_events`` BEFORE it publishes, and
    replays undelivered rows on the target's next connect. "Not delivered"
    therefore does not mean "lost" — and telling the caller to re-send would
    double-deliver once the adapter reconnects."""
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["durably_queued"] is True


@pytest.mark.asyncio
async def test_no_subscriber_error_does_not_prescribe_a_restart(listen):
    """0 subscribers means a DETACHED INBOX ADAPTER, not a dead agent. The
    remedy must never be one that destroys a healthy session."""
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    advice = " ".join(_body(result)["what_to_do"]).lower()
    # Assert
    assert "do not force-restart" in advice


@pytest.mark.asyncio
async def test_unreachable_error_carries_machine_readable_code():
    # Arrange
    url = f"http://127.0.0.1:{_refused_port()}"
    # Act
    result = await _call(url, "a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert _body(result)["code"] == ERR_UNREACHABLE
