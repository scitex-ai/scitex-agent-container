"""End-to-end: ``sac listen`` graceful shutdown does not hang on an
in-flight SSE inbox stream (card ``sac-listen-sigterm-sse-shutdown-hang``).

Lives under ``tests/integration/`` (real uvicorn server on loopback +
real ``httpx`` SSE client, no subprocess) rather than the PS-204 mirror
tree — it exercises the *interaction* of ``_listen/_node_channel`` (the
SSE handler), ``a2a/_inbox_bus`` (the broker close path), and
``_lifecycle/_listen_lifespan`` (the shutdown bridge), not one src module.

Root cause it guards against: the SSE handler used to park on
``await queue.get()`` with no shutdown signal, so uvicorn's graceful
shutdown (which waits for in-flight requests) hung until
``sac listen restart --force`` escalated to SIGKILL after 10 s. The fix
races ``queue.get()`` against a broker shutdown Event and closes the
broker the instant uvicorn's ``should_exit`` flips.

All real primitives — no mocks (PA-306).
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

TOKEN = "test-token-shutdown-sse"


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
