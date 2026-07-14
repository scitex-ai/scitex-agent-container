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
import os
from typing import Any

import pytest
import pytest_asyncio

from scitex_agent_container._mcp.channel import _consume_sse, _jittered_backoff

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
    # Arrange — reserve a port via a plain socket bind (so we own it
    # for the consumer's first attempts), then release it and re-bind
    # via the real SSE server. This is the operator's "listen-server-
    # not-yet-up at agent startup" scenario. Using a held socket
    # avoids the port-already-reused flake the naive "start+stop+
    # restart" approach exhibited.
    import socket as _socket

    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    held_port = holder.getsockname()[1]

    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"http://127.0.0.1:{held_port}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))

    # Brief delay so the consumer fires (and is refused, since the
    # holder socket is bound but not accepting) at least once.
    await asyncio.sleep(0.2)

    # Release the port and re-bind via the real SSE server. SO_REUSEADDR
    # on the new server's listen socket avoids the TIME_WAIT collision
    # that would otherwise make the immediate re-bind unreliable.
    holder.close()
    new_server = _ReconnectScenarioServer()
    new_server._server = await asyncio.start_server(
        new_server._handle,
        host=new_server.host,
        port=held_port,
        reuse_address=True,
    )
    new_server.port = held_port
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


# ---------------------------------------------------------------------------
# Reconnect when the stream dies SILENTLY — no FIN, no RST, just a socket
# nobody will ever speak on again.
#
# This is the failure mode the two tests above CANNOT see: they close the
# connection, so the client gets an EOF and obviously loops. A real listen can
# vanish without closing (hard host death, wedged uvicorn, an idle NAT/firewall
# flow drop). With an unbounded read the consumer then parks inside
# ``aiter_lines()`` forever, still believing it is subscribed while the broker
# holds no subscriber for it — deafness with no error raised anywhere, curable
# only by restarting the agent. That is the same shape as the bug #591 fixed in
# the CONNECT path, surviving in the READ path.
# ---------------------------------------------------------------------------


class _SilentStallServer:
    """Accepts, sends SSE headers, then goes SILENT — never writes, never closes.

    Faithful to a peer that died without closing its socket: from the client's
    side the connection is still ESTABLISHED and simply has nothing to say.
    """

    def __init__(self) -> None:
        self.connection_count = 0
        self._server: asyncio.base_events.Server | None = None
        self._held: list[asyncio.StreamWriter] = []
        self.host = "127.0.0.1"
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host=self.host, port=0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for w in self._held:
            try:
                w.close()
            except Exception:  # stx-allow: defensive on writer close
                pass
        self._held.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connection_count += 1
        await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
            b": sac-channel ready\n\n"
        )
        await writer.drain()
        # ...and now say nothing, forever. Hold the writer open so the socket
        # is never closed — the client must time out the READ to escape.
        self._held.append(writer)


@pytest_asyncio.fixture
async def silent_stall_server():
    server = _SilentStallServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def fast_read_deadline():
    """Squeeze the SSE read deadline to 1s so this test runs in seconds.

    Sets the REAL env var the production code reads (and restores it), rather
    than rewriting an internal — the read deadline is resolved from the
    environment at CALL time precisely so a deployment (or a test) can steer it.
    """
    key = "SAC_MCP_SSE_READ_TIMEOUT_S"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    try:
        yield 1.0
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@pytest.mark.asyncio
async def test_consume_sse_reconnects_when_stream_goes_silent(
    silent_stall_server, fast_read_deadline
):
    # Arrange — a server that opens the stream and then never speaks again and
    # never hangs up.
    received: list[dict[str, Any]] = []

    async def on_event(ev: dict[str, Any]) -> None:
        received.append(ev)

    url = f"{silent_stall_server.base_url}/agents/alice/inbox/stream"
    task = asyncio.create_task(_consume_sse(url, bearer=None, on_event=on_event))
    # Act — wait long enough for the read deadline to fire and the backoff to
    # bring the consumer back around for a second connect.
    for _ in range(120):
        if silent_stall_server.connection_count >= 2:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Assert — it re-dialled. With an unbounded read this stays at 1 forever:
    # the consumer is wedged, the agent is deaf, and nothing anywhere errors.
    assert silent_stall_server.connection_count >= 2


# ---------------------------------------------------------------------------
# Jittered backoff — the thundering herd.
#
# Every agent on a host subscribes to the SAME listen, so when it goes away they
# all lose the stream in the same instant and climb an IDENTICAL ladder (0.5s,
# 1s, 2s, 4s …), re-dialling in lockstep at a process that is by definition
# mid-restart. ~14 adapters landing together on every rung is a good way to
# knock over the thing they are all waiting for.
# ---------------------------------------------------------------------------


def test_jittered_backoff_stays_within_its_window():
    # Arrange — jitter must not extend the ladder: a retry still lands inside
    # its own backoff window, so recovery latency is unchanged.
    window = 8.0
    # Act
    samples = [_jittered_backoff(window) for _ in range(200)]
    # Assert — equal jitter: never below half the window, never above it.
    assert all(window / 2 <= s <= window for s in samples)


def test_jittered_backoff_decorrelates_retries():
    # Arrange — the whole point: two adapters that lost the stream in the same
    # instant must NOT re-dial at the same moment.
    window = 8.0
    # Act
    samples = [_jittered_backoff(window) for _ in range(200)]
    # Assert — a fixed backoff would collapse to one value; jitter spreads them.
    assert len(set(samples)) > 100
