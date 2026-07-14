"""The listen inbox stream BEATS when idle.

This is the stream every sac agent subscribes to (``sac mcp channel`` →
``GET /agents/<name>/inbox/stream`` on the host listen). It used to write one
frame on connect and then go silent until an event arrived — and a stream that
never writes cannot be told apart from one that has DIED.

That is the client's problem, and it is the one that matters: with no bytes ever
arriving, a connection that died silently (no FIN, no RST — a hard host death, a
wedged uvicorn, an idle NAT/firewall flow drop) parks the consumer on an
unbounded read forever. It believes it is subscribed; the broker holds no
subscriber for it; every message aimed at that agent lands on an empty bus.
Deafness, with nothing logged anywhere, curable only by restarting the agent.
The beat gives the client bytes to read, so a bounded read deadline can fire and
it can re-dial. See ``_mcp/channel.py::_consume_sse``.

Scope note, so nobody inherits a claim this file does not prove: uvicorn ALREADY
reaps a subscriber whose client closes cleanly or resets the connection (it sees
``connection_lost`` and cancels the response, which runs the stream's
``finally``). The beat adds nothing there. What it adds server-side is narrower:
on an idle stream the beat is the ONLY write, so a peer that vanished with no TCP
signal at all will eventually surface an error on write (TCP retransmit timeout)
instead of holding its subscriber slot indefinitely. That path takes minutes and
is not exercised here — it is a consequence, not a claim under test.

Runs a REAL uvicorn over a real loopback socket against the real ``create_app``
— the only seam is the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket

import httpx
import pytest
import uvicorn

from scitex_agent_container._listen.server import create_app
from tests.scitex_agent_container._helpers.loopback_server import (
    await_until_serving,
    serve_in_thread,
)

TOKEN = "test-token-keepalive"
AGENT = "keepalive-bob"

# Beat fast so the tests take milliseconds, not a minute.
BEAT_S = "0.2"


@pytest.fixture
def fast_beat_env():
    """Set the REAL env var the server reads for its beat cadence, then restore.

    The interval is resolved at CALL time precisely so a deployment (or a test)
    can steer it — this exercises that real path rather than rewriting an
    internal.
    """
    key = "SAC_INBOX_KEEPALIVE_S"
    previous = os.environ.get(key)
    os.environ[key] = BEAT_S
    try:
        yield float(BEAT_S)
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _free_port() -> int:
    """Pick a free loopback port.

    A plain helper, deliberately NOT a fixture: STX-TQ005 forbids a fixture from
    acquiring an external resource, and the same convention is already used by
    the sibling suites (see ``test__nodes.py::_sse_roundtrip``).
    """
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(port: int):
    app = create_app(token=TOKEN)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)
    thread, crash = serve_in_thread(server, port)
    await await_until_serving(server, thread, port=port, crash=crash)
    return app, server, thread


@pytest.mark.asyncio
async def test_idle_inbox_stream_emits_keepalive_frames(fast_beat_env):
    # Arrange — subscribe and then publish NOTHING. Under the old handler this
    # connection would receive exactly one frame ever and then fall silent.
    port = _free_port()
    app, server, thread = await _serve(port)
    headers = {"authorization": f"Bearer {TOKEN}"}
    beats: list[str] = []

    async def subscribe() -> None:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=10.0
        ) as ac:
            async with ac.stream(
                "GET", f"/agents/{AGENT}/inbox/stream", headers=headers
            ) as sse:
                async for line in sse.aiter_lines():
                    if line.startswith(":") and "keepalive" in line:
                        beats.append(line)
                        if len(beats) >= 2:
                            return

    # Act — wait for two beats on a stream with zero traffic.
    task = asyncio.create_task(subscribe())
    try:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10.0)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        server.should_exit = True
        thread.join(timeout=5.0)
    # Assert — an idle stream keeps speaking, so a client can tell it is alive.
    assert len(beats) >= 2
