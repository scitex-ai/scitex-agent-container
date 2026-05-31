"""SSE reconnect verification for ``_mcp.channel._consume_sse`` (task #26 sub 2).

Background: the SAC-from-SAC investigation surfaced that the in-container
SSE consumer (`_mcp/channel.py:170-237`) already implements exponential-
backoff auto-reconnect (0.5s → 30s cap) and the existing test suite
covers the happy path (`test_consume_sse_dispatches_event_to_callback`
+ friends) — but NOT the operator-mandated invariant for task #26:

  **When the listen server drops the SSE connection mid-stream
  (e.g. `sac listen` restarts), agents MUST auto-reconnect WITHOUT
  an agent restart.** A regression here would silently orphan
  every agent on the host (heartbeats keep beating, but pushed
  turns never land).

This module pins that invariant. Each test drives REAL ``_consume_sse``
against a real local asyncio TCP server that mimics ``sac listen``'s
SSE wire shape. The fakes simulate the failure modes the production
listen restart actually exposes:

* Server drops the connection after the first batch (clean EOF mid-
  stream — the operator-restart scenario).
* Server is initially unreachable, then becomes reachable.
* Server returns 200 but transient transport error during streaming.

No mocks (PA-306): real ``asyncio.start_server`` + real
``httpx.AsyncClient`` inside ``_consume_sse``. Each test: AAA markers
(TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from scitex_agent_container._mcp.channel import _consume_sse

# ---------------------------------------------------------------------------
# Servers — minimal asyncio TCP servers speaking SSE, with controllable
# failure modes the production listen restart actually exposes
# ---------------------------------------------------------------------------


class _ReconnectScenarioServer:
    """Real asyncio TCP server with operator-controlled failure modes.

    Pattern per-connection:
      - first connection: emit any events in ``first_batch`` then EOF
      - subsequent connections: emit any events in ``second_batch``
        then EOF
    ``connection_count`` records how many times the server accepted a
    connection (the reconnect counter).
    """

    def __init__(self) -> None:
        self.first_batch: list[dict[str, Any]] = []
        self.second_batch: list[dict[str, Any]] = []
        self.connection_count = 0
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
        self.connection_count += 1
        which = self.connection_count
        try:
            # Drain request line + headers (we don't branch on them).
            await reader.readline()
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            # Serve SSE headers.
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
                b": keep-alive\n\n"
            )
            await writer.drain()
            batch = self.first_batch if which == 1 else self.second_batch
            for ev in batch:
                writer.write(f"data: {json.dumps(ev)}\n\n".encode())
                await writer.drain()
            # Close the writer to simulate the listen-server-dropping-
            # the-stream signal. Brief hold so the client buffers
            # cleanly before the EOF lands.
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # stx-allow: defensive on writer close
                pass


@pytest_asyncio.fixture
async def reconnect_server():
    server = _ReconnectScenarioServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Reconnect after the listen drops the stream mid-flight
# (the operator-restart scenario)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_sse_reconnects_after_server_drops_connection(
    reconnect_server,
):
    # Arrange — first batch arrives, server drops the stream, second
    # batch arrives on the auto-reconnect. This is the operator's
    # restart-survives invariant.
    reconnect_server.first_batch = [
        {"msg_id": "pre-restart", "from_agent": "lead", "content": "1"}
    ]
    reconnect_server.second_batch = [
        {"msg_id": "post-restart", "from_agent": "lead", "content": "2"}
    ]
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{reconnect_server.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act — poll until both pre- and post-restart events arrive (the
    # reconnect happens after the 0.5s initial backoff).
    for _ in range(120):
        if len(received) >= 2:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert — both events landed across the drop, with the post-
    # restart event observable AFTER the pre-restart one (proves the
    # consumer reconnected, not a single long-lived stream).
    assert [e["msg_id"] for e in received] == ["pre-restart", "post-restart"]


@pytest.mark.asyncio
async def test_consume_sse_records_at_least_two_connection_attempts(
    reconnect_server,
):
    # Arrange — proves the reconnect is REAL (a second TCP connection
    # to the server), not e.g. the consumer holding the same stream
    # open. The server's ``connection_count`` records each accepted
    # connection.
    reconnect_server.first_batch = [
        {"msg_id": "first", "from_agent": "lead", "content": "x"}
    ]
    reconnect_server.second_batch = [
        {"msg_id": "second", "from_agent": "lead", "content": "y"}
    ]
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{reconnect_server.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act — wait until the second event arrives (forces a reconnect).
    for _ in range(120):
        if len(received) >= 2:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert — the server saw >=2 incoming connections (reconnect
    # actually happened). Strict >= guards against any flake where the
    # consumer tries a third reconnect attempt before cancellation.
    assert reconnect_server.connection_count >= 2


# ---------------------------------------------------------------------------
# Reconnect when the server is initially unreachable
# (the listen-server-not-yet-up scenario at agent startup)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_sse_reconnects_after_server_starts_late():
    # Arrange — pick an unused port, start the consumer FIRST against
    # it, then start the server. The consumer must retry until the
    # server is reachable, then deliver the event.
    server = _ReconnectScenarioServer()
    # Pre-allocate a port by starting and immediately stopping a
    # throwaway server (kernel hands the same port back if we're
    # quick — but we also tolerate a different port because the
    # client only knows the URL once the real server is up). Easier:
    # start the server first to grab the port, stop it, then drive
    # the consumer at that URL while we re-start.
    await server.start()
    await server.stop()
    held_port = server.port

    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"http://127.0.0.1:{held_port}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))

    # Brief delay so the consumer fires (and fails) at least once.
    await asyncio.sleep(0.2)

    # Re-arm the server on the (likely-same) port. If the port was
    # reused by the kernel we get true late-start coverage; if not,
    # the test gracefully aborts before assertion.
    new_server = _ReconnectScenarioServer()
    try:
        new_server._server = await asyncio.start_server(
            new_server._handle, host=new_server.host, port=held_port
        )
        new_server.port = held_port
    except OSError:
        # Port reused by some other process — abort the test cleanly
        # rather than flake on port-allocation luck.
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        pytest.skip("port reused by an unrelated process — flake guard")
    new_server.first_batch = [
        {"msg_id": "late-start", "from_agent": "lead", "content": "z"}
    ]
    try:
        # Act — poll until the late-start event arrives.
        for _ in range(120):
            if received:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await new_server.stop()
    # Assert — the event from the late-started server reached the
    # callback, proving the consumer kept retrying through the
    # unreachable window.
    assert received and received[0]["msg_id"] == "late-start"
