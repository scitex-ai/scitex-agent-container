"""Declared SDK wire types and real delivery/receipt failure boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from pydantic import TypeAdapter

from scitex_agent_container._mcp.channel import _push_channel_event
from scitex_agent_container.runtimes._channel_inbox_dispatcher import _NotificationSink


class _WireSession:
    """Serialize actual SDK envelopes onto a real memory transport."""

    def __init__(self) -> None:
        self.send, self.receive = anyio.create_memory_object_stream[bytes](1)

    async def send_message(self, message: SessionMessage) -> None:
        wire = TypeAdapter(JSONRPCMessage).dump_json(message.message)
        await self.send.send(wire)

    async def decoded(self) -> dict:
        wire = await self.receive.receive()
        validated = TypeAdapter(JSONRPCMessage).validate_json(wire)
        return json.loads(TypeAdapter(JSONRPCMessage).dump_json(validated))


@contextlib.asynccontextmanager
async def _receipt_endpoint(status: int) -> AsyncIterator[tuple[str, list[dict]]]:
    """Own a bounded real loopback endpoint; never contact a fleet server."""
    requests: list[dict] = []
    tasks: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await reader.readline()
            size = 0
            while (line := await reader.readline()) != b"\r\n":
                if not line:
                    raise EOFError("incomplete synthetic request")
                if line.lower().startswith(b"content-length:"):
                    size = int(line.split(b":", 1)[1])
            requests.append(json.loads(await reader.readexactly(size)))
            body = b'{"accepted":true}'
            writer.write(
                f"HTTP/1.1 {status} Synthetic\r\nContent-Length: {len(body)}\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield url, requests
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


def _event() -> dict:
    return {
        "from_agent": "sdk-contract-peer",
        "content": "synthetic notification",
        "msg_id": "sdk-contract-event",
        "ts": 0,
        "requires_reply": True,
    }


def test_notification_roundtrips_method_through_declared_sdk():
    # Arrange
    async def run():
        session = _WireSession()
        # Act
        await _push_channel_event(session, _event())
        return await session.decoded()

    decoded = asyncio.run(run())
    # Assert
    assert decoded["method"] == "notifications/claude/channel"


def test_notification_roundtrips_jsonrpc_version():
    # Arrange
    async def run():
        session = _WireSession()
        # Act
        await _push_channel_event(session, _event())
        return await session.decoded()

    decoded = asyncio.run(run())
    # Assert
    assert decoded["jsonrpc"] == "2.0"


def test_notification_preserves_content_on_wire():
    # Arrange
    async def run():
        session = _WireSession()
        # Act
        await _push_channel_event(session, _event())
        return await session.decoded()

    decoded = asyncio.run(run())
    # Assert
    assert decoded["params"]["content"] == "synthetic notification"


def test_notification_metadata_keeps_client_string_contract():
    # Arrange
    async def run():
        session = _WireSession()
        # Act
        await _push_channel_event(session, _event())
        return await session.decoded()

    decoded = asyncio.run(run())
    # Assert
    assert decoded["params"]["meta"]["requires_reply"] == "true"


def test_missing_receipt_configuration_still_delivers():
    # Arrange
    async def run():
        session = _WireSession()
        async with _receipt_endpoint(202) as (_, requests):
            # Act
            await _push_channel_event(session, _event())
            await session.decoded()
            return requests

    requests = asyncio.run(run())
    # Assert
    assert requests == []


def test_successful_delivery_sends_structural_receipt():
    # Arrange
    async def run():
        session = _WireSession()
        async with _receipt_endpoint(202) as (url, requests):
            # Act
            await _push_channel_event(
                session, _event(), agent_name="sdk-receiver", listen_url=url
            )
            return requests

    requests = asyncio.run(run())
    # Assert
    assert requests[0]["params"]["metadata"]["kind"] == "reaction"


def test_receipt_http_error_does_not_lose_delivered_notification():
    # Arrange
    async def run():
        session = _WireSession()
        async with _receipt_endpoint(503) as (url, _):
            # Act
            await _push_channel_event(
                session, _event(), agent_name="sdk-error-receiver", listen_url=url
            )
            return await session.decoded()

    decoded = asyncio.run(run())
    # Assert
    assert decoded["params"]["content"] == "synthetic notification"


def test_closed_delivery_transport_refuses():
    # Arrange
    async def run():
        session = _WireSession()
        await session.receive.aclose()
        try:
            await _push_channel_event(session, _event())
        except anyio.BrokenResourceError as exc:
            return type(exc)
        return None

    # Act
    error_type = asyncio.run(run())
    # Assert
    assert error_type is anyio.BrokenResourceError


def test_failed_delivery_never_sends_receipt():
    # Arrange
    async def run():
        session = _WireSession()
        await session.receive.aclose()
        async with _receipt_endpoint(202) as (url, requests):
            # Act
            try:
                await _push_channel_event(
                    session, _event(), agent_name="sdk-refused", listen_url=url
                )
            except anyio.BrokenResourceError:
                pass
            return requests

    requests = asyncio.run(run())
    # Assert
    assert requests == []


def test_native_notification_sink_handles_nonwake_receipt():
    # Arrange
    event = {**_event(), "ack": True}
    # Act
    delivered = asyncio.run(_push_channel_event(_NotificationSink(), event))
    # Assert
    assert delivered is None
