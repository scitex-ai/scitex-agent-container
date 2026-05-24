"""``_wake_turn`` threads requester identity into the woken ``/v1/turn`` body.

NO MOCKS. ``_wake_turn`` POSTs to a REAL in-process HTTP/1.1 receiver
(``asyncio.start_server``) that captures the JSON body. The test then
asserts the requester fields (``from_agent`` / ``dispatch_id``) rode
into the body so the woken turn's envelope carries them to the Stop
hook. The ``"unknown"`` sentinel that ``mint_event`` stamps for a
missing sender must NOT be threaded as a requester.

TQ: AAA markers, ≥3-word names, one assertion each.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

from scitex_agent_container._mcp._channel_wake import _wake_turn


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CapturingHTTP:
    """Minimal real HTTP/1.1 server that records one POST body and 200s."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.port = _free_port()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        data = await reader.read(65_536)
        head, _, body = data.partition(b"\r\n\r\n")
        # Read any remaining body bytes up to Content-Length.
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        while len(body) < length:
            more = await reader.read(length - len(body))
            if not more:
                break
            body += more
        try:
            self.bodies.append(json.loads(body.decode() or "{}"))
        except json.JSONDecodeError:
            self.bodies.append({})
        resp_body = b'{"text": "ok", "session_id": null, "exit_after": false}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(resp_body)).encode() + b"\r\n\r\n" + resp_body
        )
        await writer.drain()
        writer.close()


async def _wake_and_capture(event: dict[str, Any]) -> dict[str, Any]:
    """Start the real receiver, run ``_wake_turn``, return the captured body."""
    srv = _CapturingHTTP()
    await srv.start()
    try:
        await _wake_turn(
            event, turn_url=f"http://127.0.0.1:{srv.port}/v1/turn", bearer=None
        )
    finally:
        await srv.stop()
    return srv.bodies[0]


class TestWakeThreadsRequester:
    def test_wake_body_carries_from_agent_of_event_sender(self) -> None:
        # Arrange
        event = {"from_agent": "lead", "content": "do it", "msg_id": "m1"}
        # Act
        body = asyncio.run(_wake_and_capture(event))
        # Assert
        assert body["from_agent"] == "lead"

    def test_wake_body_carries_dispatch_id_when_event_has_one(self) -> None:
        # Arrange
        event = {
            "from_agent": "lead",
            "content": "do it",
            "msg_id": "m1",
            "dispatch_id": "d-42",
        }
        # Act
        body = asyncio.run(_wake_and_capture(event))
        # Assert
        assert body["dispatch_id"] == "d-42"

    def test_wake_body_omits_unknown_sentinel_as_requester(self) -> None:
        # Arrange
        # mint_event defaults a missing sender to the literal "unknown".
        event = {"from_agent": "unknown", "content": "do it", "msg_id": "m1"}
        # Act
        body = asyncio.run(_wake_and_capture(event))
        # Assert
        assert "from_agent" not in body

    def test_wake_body_omits_dispatch_id_when_event_lacks_one(self) -> None:
        # Arrange
        event = {"from_agent": "lead", "content": "do it", "msg_id": "m1"}
        # Act
        body = asyncio.run(_wake_and_capture(event))
        # Assert
        assert "dispatch_id" not in body

    def test_wake_body_always_carries_the_turn_text(self) -> None:
        # Arrange
        event = {"from_agent": "lead", "content": "do it", "msg_id": "m1"}
        # Act
        body = asyncio.run(_wake_and_capture(event))
        # Assert
        assert "do it" in body["text"]
