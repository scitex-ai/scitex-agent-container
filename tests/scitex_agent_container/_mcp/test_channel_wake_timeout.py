"""``_wake_turn`` BOUNDS its client-side wait so a WEDGED ``/v1/turn`` cannot
block the SSE consumer forever (load-resilience incident 2026-07-09).

NO MOCKS (STX-NM002): the timeout is exercised against a REAL in-process HTTP/1.1
receiver (``asyncio.start_server``) that ACCEPTS the connection but never sends a
response. The production transport (a real ``httpx.AsyncClient``) must then raise
a ``httpx.TimeoutException`` within the bound instead of hanging indefinitely.
Env behaviour is exercised by setting the REAL ``SAC_MCP_WAKE_TIMEOUT_S`` var (a
``yield``-based env helper, not ``monkeypatch``).

TQ: AAA markers, >=3-word names, one assertion each.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from contextlib import contextmanager

import httpx
import pytest

from scitex_agent_container._mcp._channel_wake import (
    _WAKE_TIMEOUT_DEFAULT_S,
    _resolve_wake_timeout,
    _wake_turn,
)


@contextmanager
def _env(name: str, value: str):
    """Set a REAL env var for the block, restoring the prior value on exit."""
    prev = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _HangingHTTP:
    """Real HTTP/1.1 server that accepts + reads a request but NEVER replies."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.port = _free_port()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            # close() only: do NOT wait_closed(). The stalled handler holds its
            # connection open for the full sleep, and (Python 3.12+) wait_closed()
            # blocks until active connections finish — which would make TEARDOWN,
            # not the client bound, dominate the elapsed time. The orphaned handler
            # is cancelled when asyncio.run() tears the loop down.
            self._server.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.read(1024)  # consume request head, then stall
            await asyncio.sleep(30)  # never respond within the client bound
        except asyncio.CancelledError:  # test teardown cancels the stalled handler
            pass
        finally:
            writer.close()


async def _wake_against_hung_server(timeout: float) -> None:
    """Run ``_wake_turn`` against a server that never responds."""
    srv = _HangingHTTP()
    await srv.start()
    try:
        await _wake_turn(
            {"from_agent": "lead", "content": "do it", "msg_id": "m1"},
            turn_url=f"http://127.0.0.1:{srv.port}/v1/turn",
            bearer=None,
            timeout=timeout,
        )
    finally:
        await srv.stop()


# -- env resolution: the bound is always finite + positive ----------------


class TestResolveWakeTimeout:
    def test_default_is_the_finite_module_bound(self) -> None:
        # Arrange
        expected = _WAKE_TIMEOUT_DEFAULT_S
        # Act
        resolved = _resolve_wake_timeout()
        # Assert — never None/infinite; a wedged runner is always recoverable.
        assert resolved == expected

    def test_honours_env_override_seconds(self) -> None:
        # Arrange
        var = "SAC_MCP_WAKE_TIMEOUT_S"
        # Act
        with _env(var, "45"):
            resolved = _resolve_wake_timeout()
        # Assert
        assert resolved == 45.0

    def test_ignores_unparseable_env_value(self) -> None:
        # Arrange
        var = "SAC_MCP_WAKE_TIMEOUT_S"
        # Act
        with _env(var, "nonsense"):
            resolved = _resolve_wake_timeout()
        # Assert
        assert resolved == _WAKE_TIMEOUT_DEFAULT_S

    def test_ignores_nonpositive_env_value(self) -> None:
        # Arrange — 0 / negative would be "no bound"; refuse it.
        var = "SAC_MCP_WAKE_TIMEOUT_S"
        # Act
        with _env(var, "0"):
            resolved = _resolve_wake_timeout()
        # Assert
        assert resolved == _WAKE_TIMEOUT_DEFAULT_S


# -- behaviour: a hung /v1/turn raises within the bound, never hangs -------


class TestWakeTimesOutOnHungTurnUrl:
    def test_raises_httpx_timeout_when_turn_url_hangs(self) -> None:
        # Arrange — real server accepts but never responds; tiny client bound.
        coro = _wake_against_hung_server(1.0)
        # Act
        run = pytest.raises(httpx.TimeoutException)
        # Assert — a real TimeoutException, NOT an infinite block.
        with run:
            asyncio.run(coro)

    def test_returns_within_bound_not_after_server_sleep(self) -> None:
        # Arrange
        start = time.monotonic()
        # Act — the server would sleep 30 s; the 1 s client bound must win.
        try:
            asyncio.run(_wake_against_hung_server(1.0))
        except httpx.TimeoutException:
            pass
        # Assert — returned promptly (well under the server's 30 s stall).
        assert time.monotonic() - start < 10.0
