"""Tests for the SSE consumer's replay cursor (``Last-Event-ID``).

THE DEFECT THESE GUARD. ``a2a/_inbox_stream.py`` has always supported
replay: it stamps the ``channel_events`` row id on every frame's ``id:``
line and, given a ``Last-Event-ID`` header, replays every row with
``id > cursor``. The consumer never used it — it parsed only ``data:``
lines, discarded ``id:`` entirely, and built its header dict once before
the reconnect loop. So a reconnect resumed at "now" and every event that
arrived while it was re-dialing was never handed to the session.

Measured 2026-08-09 across one ``sac listen`` restart: two agents' inboxes
went 10→2 and 2→0, and we filed it as a DURABILITY defect. It was not —
``channel_events`` held 355 rows, 350 stamped delivered, spanning the
restart in both directions. The storing was fine; the asking was missing.

Same harness as ``test_channel_reconnect.py`` (a real asyncio TCP server
speaking SSE) with one addition: this server RECORDS the request headers
of every connection, which is the only way to assert what the client
actually sent on re-dial.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from scitex_agent_container._mcp.channel import _consume_sse


class _HeaderRecordingServer:
    """SSE server that records each connection's request headers.

    First connection emits ``first_batch`` (each entry an ``(id, event)``
    pair, ``id`` may be ``None`` to omit the ``id:`` line) then EOF, so the
    client is forced to reconnect. Later connections just hold open.
    """

    def __init__(self) -> None:
        self.first_batch: list[tuple[str | None, dict[str, Any]]] = []
        self.headers_seen: list[dict[str, str]] = []
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
        which = len(self.headers_seen) + 1
        try:
            await reader.readline()  # request line
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                raw = line.decode("latin-1").strip()
                if ":" in raw:
                    key, _, value = raw.partition(":")
                    # Header names are case-insensitive on the wire; normalise
                    # so a test asserting "Last-Event-ID" cannot pass or fail
                    # on the client's choice of capitalisation.
                    headers[key.strip().lower()] = value.strip()
            self.headers_seen.append(headers)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
                b": keep-alive\n\n"
            )
            await writer.drain()
            if which == 1:
                for event_id, ev in self.first_batch:
                    frame = ""
                    if event_id is not None:
                        frame += f"id: {event_id}\n"
                    frame += f"data: {json.dumps(ev)}\n\n"
                    writer.write(frame.encode())
                    await writer.drain()
                await asyncio.sleep(0.05)
            else:
                # Hold the second connection open; the test cancels the
                # consumer once it has the header it came for.
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # stx-allow: defensive on writer close
                pass


@pytest_asyncio.fixture
async def header_server():
    server = _HeaderRecordingServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _run_until_reconnect(server: _HeaderRecordingServer) -> None:
    """Drive ``_consume_sse`` until the server has seen two connections."""
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{server.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, None, on_event))
    try:
        for _ in range(200):
            if len(server.headers_seen) >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # stx-allow: teardown
            pass


@pytest.mark.asyncio
async def test_first_connection_sends_no_last_event_id(header_server):
    # Arrange
    header_server.first_batch = [("42", {"msg_id": "m1", "content": "one"})]
    # Act
    await _run_until_reconnect(header_server)
    # Assert
    assert "last-event-id" not in header_server.headers_seen[0]


@pytest.mark.asyncio
async def test_reconnect_sends_last_event_id_from_the_id_line(header_server):
    # Arrange
    header_server.first_batch = [("42", {"msg_id": "m1", "content": "one"})]
    # Act
    await _run_until_reconnect(header_server)
    # Assert
    assert header_server.headers_seen[1].get("last-event-id") == "42"


@pytest.mark.asyncio
async def test_reconnect_cursor_is_the_latest_id_not_the_first(header_server):
    # Arrange
    header_server.first_batch = [
        ("42", {"msg_id": "m1", "content": "one"}),
        ("43", {"msg_id": "m2", "content": "two"}),
    ]
    # Act
    await _run_until_reconnect(header_server)
    # Assert
    assert header_server.headers_seen[1].get("last-event-id") == "43"


@pytest.mark.asyncio
async def test_reconnect_sends_no_cursor_when_server_stamped_no_id(header_server):
    # Arrange — a server that omits ``id:`` gives us nothing to resume from,
    # and inventing a cursor would earn a 400 from the real listen.
    header_server.first_batch = [(None, {"msg_id": "m1", "content": "one"})]
    # Act
    await _run_until_reconnect(header_server)
    # Assert
    assert "last-event-id" not in header_server.headers_seen[1]
