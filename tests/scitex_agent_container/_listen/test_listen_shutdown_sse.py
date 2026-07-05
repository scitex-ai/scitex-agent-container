"""Graceful-shutdown coverage for the ``sac listen`` SSE inbox stream.

Card ``sac-listen-sigterm-sse-shutdown-hang``: the per-agent SSE
inbox-stream handler parks on ``asyncio.Queue.get()`` with no shutdown
signal, so a SIGTERM graceful shutdown of ``sac listen`` waits on the
in-flight stream until ``sac listen restart --force`` escalates to
SIGKILL after 10 s. The fix races ``queue.get()`` against a broker-level
shutdown Event (:meth:`Broker.get_or_close` / :meth:`Broker.close`) and
wires a lifespan shutdown-bridge that closes the broker the instant
uvicorn's ``should_exit`` flips, so in-flight streams cancel promptly.

All real primitives — a real :class:`Broker`, a real ``asyncio.Queue``,
and (for the end-to-end case) a real uvicorn server driven over loopback
TCP with a real ``httpx`` SSE client. No mocks (PA-306).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container.a2a._inbox_bus import Broker

TOKEN = "test-token-shutdown-sse"


# --- Broker-level unit tests (pure asyncio, no server) ----------------------


async def _park_then_probe_done() -> bool:
    """Park a ``get_or_close`` on an idle+open broker; report whether it
    completed (it must not)."""
    broker = Broker()
    q = await broker.subscribe("alice")
    waiter = asyncio.ensure_future(broker.get_or_close(q))
    await asyncio.sleep(0.15)
    done = waiter.done()
    waiter.cancel()
    with contextlib.suppress(BaseException):
        await waiter
    return done


async def _close_then_collect_result() -> object:
    """Park a ``get_or_close``, then ``close()`` the broker and return
    whatever the parked call resolves to."""
    broker = Broker()
    q = await broker.subscribe("alice")
    waiter = asyncio.ensure_future(broker.get_or_close(q))
    await asyncio.sleep(0.1)
    broker.close()
    return await asyncio.wait_for(waiter, timeout=1.0)


async def _close_then_measure_unblock_seconds() -> float:
    """Park a ``get_or_close``, ``close()``, and return the seconds it
    took to unblock."""
    broker = Broker()
    q = await broker.subscribe("alice")
    waiter = asyncio.ensure_future(broker.get_or_close(q))
    await asyncio.sleep(0.1)
    t0 = time.monotonic()
    broker.close()
    await asyncio.wait_for(waiter, timeout=1.0)
    return time.monotonic() - t0


async def _deliver_pending_event() -> object:
    """A queued event must be returned by ``get_or_close`` (not dropped)."""
    broker = Broker()
    q = await broker.subscribe("alice")
    await q.put({"msg_id": "m1", "content": "hi"})
    return await asyncio.wait_for(broker.get_or_close(q), timeout=1.0)


async def _result_when_already_closed() -> object:
    """``get_or_close`` on an already-closed broker resolves at once."""
    broker = Broker()
    q = await broker.subscribe("alice")
    broker.close()
    return await asyncio.wait_for(broker.get_or_close(q), timeout=1.0)


def test_get_or_close_blocks_while_idle_and_open() -> None:
    # Arrange
    scenario = _park_then_probe_done()
    # Act
    completed = asyncio.run(scenario)
    # Assert
    assert completed is False


def test_get_or_close_returns_none_when_broker_closes() -> None:
    # Arrange
    scenario = _close_then_collect_result()
    # Act
    result = asyncio.run(scenario)
    # Assert
    assert result is None


def test_get_or_close_unblocks_within_bounded_time_on_close() -> None:
    # Arrange
    scenario = _close_then_measure_unblock_seconds()
    # Act
    elapsed = asyncio.run(scenario)
    # Assert
    assert elapsed < 0.5


def test_get_or_close_delivers_a_pending_event() -> None:
    # Arrange
    scenario = _deliver_pending_event()
    # Act
    event = asyncio.run(scenario)
    # Assert
    assert event == {"msg_id": "m1", "content": "hi"}


def test_get_or_close_returns_none_immediately_if_already_closed() -> None:
    # Arrange
    scenario = _result_when_already_closed()
    # Act
    result = asyncio.run(scenario)
    # Assert
    assert result is None


def test_broker_is_not_closing_before_close() -> None:
    # Arrange
    broker = Broker()
    # Act
    closing = broker.is_closing()
    # Assert
    assert closing is False


def test_close_sets_is_closing_true() -> None:
    # Arrange
    broker = Broker()
    # Act
    broker.close()
    # Assert
    assert broker.is_closing() is True


def test_close_is_idempotent() -> None:
    # Arrange
    broker = Broker()
    # Act
    broker.close()
    broker.close()
    # Assert
    assert broker.is_closing() is True


# --- End-to-end: real uvicorn daemon shutdown does not hang on SSE ----------


@pytest.fixture
def shutdown_env(tmp_path: Path):
    """Isolate on-disk roots to ``tmp_path`` and disable the noisy
    background loops so the lifespan launches essentially just the
    shutdown bridge — keeping the shutdown-timing assertion deterministic.
    """
    saved: dict[str, str | None] = {}
    env = {
        "HOME": str(tmp_path),
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR": str(tmp_path / "registry"),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SCITEX_AGENT_CONTAINER_STATE_DB": str(tmp_path / "state.db"),
        # Quiet the lifespan's optional background loops for a clean,
        # fast shutdown-timing measurement (the SSE stream + bridge are
        # what this test exercises).
        "SAC_PERIODIC_DRIVE_DISABLED": "1",
        "SAC_GITHUB_CI_POLLER_DISABLED": "1",
        "SAC_TUI_HEARTBEAT_DISABLED": "1",
        "SAC_SDK_HEARTBEAT_DISABLED": "1",
        "SAC_LIVENESS_TICK_DISABLED": "1",
        "SAC_LISTEN_STARTUP_SYNC_DISABLED": "1",
        "SAC_LISTEN_BIND_WATCHDOG_DISABLED": "1",
    }
    for key, val in env.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = val
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _measure_inflight_sse_shutdown_seconds(
    *, server: uvicorn.Server, thread: threading.Thread, port: int
) -> float:
    """Open a real SSE inbox stream, park it, flip ``should_exit`` (what
    uvicorn's SIGTERM handler does), and return how long the daemon
    thread took to exit.

    Raises if the stream never opened or if the thread was still alive
    after an 8 s join (the hang the fix must prevent) — so the caller's
    single assertion only has to police promptness.
    """
    ready = asyncio.Event()
    stream_ended = asyncio.Event()

    async def consume() -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as ac:
                async with ac.stream(
                    "GET",
                    f"http://127.0.0.1:{port}/agents/alice/inbox/stream",
                    headers={"authorization": f"Bearer {TOKEN}"},
                ) as sse:
                    async for line in sse.aiter_lines():
                        # The `: sac-channel ready` comment frame confirms
                        # the subscription is live and the loop is now
                        # parked on the next event.
                        if line.startswith(":"):
                            ready.set()
                    # aiter_lines ends when the server closes the
                    # connection on graceful shutdown.
        finally:
            stream_ended.set()

    sub = asyncio.ensure_future(consume())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
        # Confirm it is genuinely parked — no event will ever arrive.
        await asyncio.sleep(0.25)
        if sub.done():
            raise RuntimeError("SSE stream closed before shutdown was triggered")

        # Trigger the graceful shutdown exactly as SIGTERM does.
        t0 = time.monotonic()
        server.should_exit = True
        await asyncio.to_thread(thread.join, 8.0)
        elapsed = time.monotonic() - t0
        if thread.is_alive():
            raise RuntimeError(
                f"sac listen hung on SIGTERM with an in-flight SSE stream "
                f"({elapsed:.1f}s, thread still alive) — graceful shutdown "
                "did not cancel it, would escalate to SIGKILL"
            )
        await asyncio.wait_for(stream_ended.wait(), timeout=3.0)
        return elapsed
    finally:
        if not sub.done():
            sub.cancel()
            with contextlib.suppress(BaseException):
                await sub


def test_sac_listen_graceful_shutdown_cancels_inflight_sse(shutdown_env) -> None:
    """The regression guard: with a client parked on the SSE inbox
    stream, flipping uvicorn's ``should_exit`` must let the daemon exit
    PROMPTLY — the in-flight stream must not block graceful shutdown.

    ``timeout_graceful_shutdown`` is deliberately LARGE (30 s) so the
    only thing that can make shutdown prompt is the lifespan shutdown
    bridge cancelling the SSE stream; a regression that reintroduces the
    bare ``queue.get()`` (or drops the bridge) leaves the thread alive
    past the 8 s join, which ``_measure_...`` raises on.
    """
    # Arrange
    port = _free_port()
    app = create_app(token=TOKEN)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws="none",
        # Large on purpose — isolate the bridge as the thing under test.
        timeout_graceful_shutdown=30.0,
    )
    server = uvicorn.Server(config)
    # Mirror the boot path (listen_cmds._do_start_listen) so the lifespan
    # shutdown bridge is wired to this server's ``should_exit``.
    app.state.uvicorn_server = server
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn loopback did not start in 5s")
        time.sleep(0.02)

    # Act
    try:
        elapsed = asyncio.run(
            _measure_inflight_sse_shutdown_seconds(
                server=server, thread=thread, port=port
            )
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)

    # Assert
    assert elapsed < 5.0
