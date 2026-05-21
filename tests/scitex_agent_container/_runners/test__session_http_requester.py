"""``POST /v1/turn`` threads requester identity onto the TurnEnvelope.

NO MOCKS. A real ``serve_inbound`` HTTP server on a real socket, fed by a
real asyncio consumer that captures the dequeued envelope so the test can
assert the requester fields (``from_agent`` / ``dispatch_id``) landed on
it. Mirrors the no-mock pattern in ``test__session_http.py``.

TQ: AAA markers, ≥3-word names, one assertion each.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.request
from typing import Any

import pytest

from scitex_agent_container._runners._session_http import serve_inbound
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_bound(port: int) -> None:
    """Poll until the TCP port accepts connections."""
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    pytest.fail(f"server never bound on port {port}")


async def _capturing_consumer(inbox: "asyncio.Queue", *, captured: list) -> None:
    """Real consumer: record each turn envelope, then resolve its future."""
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope) and not env.response.done():
            captured.append(env)
            env.response.set_result("ok")


def _post(url: str, body: dict) -> None:
    """POST a JSON body to ``url`` (fire-and-forget; success is implicit)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        resp.read()


async def _run_and_capture(body: dict) -> TurnEnvelope:
    """Spin the real sidecar, POST ``body`` to /v1/turn, return the envelope."""
    port = _free_port()
    inbox = make_inbox()
    stop = asyncio.Event()
    captured: list[Any] = []
    consumer = asyncio.create_task(_capturing_consumer(inbox, captured=captured))
    server = asyncio.create_task(
        serve_inbound(inbox, host="127.0.0.1", port=port, stop=stop)
    )
    try:
        await _wait_bound(port)
        await asyncio.to_thread(_post, f"http://127.0.0.1:{port}/v1/turn", body)
    finally:
        stop.set()
        await inbox.put(ShutdownEnvelope())
        await asyncio.wait_for(consumer, timeout=5.0)
        await asyncio.wait_for(server, timeout=5.0)
    return captured[0]


class TestRequesterThreading:
    def test_v1_turn_threads_from_agent_onto_envelope(self) -> None:
        # Arrange
        body = {"text": "hi", "from_agent": "lead", "dispatch_id": "d-1"}
        # Act
        env = asyncio.run(_run_and_capture(body))
        # Assert
        assert env.from_agent == "lead"

    def test_v1_turn_threads_dispatch_id_onto_envelope(self) -> None:
        # Arrange
        body = {"text": "hi", "from_agent": "lead", "dispatch_id": "d-1"}
        # Act
        env = asyncio.run(_run_and_capture(body))
        # Assert
        assert env.dispatch_id == "d-1"

    def test_v1_turn_without_from_agent_leaves_envelope_requester_none(self) -> None:
        # Arrange
        body = {"text": "boot"}  # mission/boot turn — no requester declared
        # Act
        env = asyncio.run(_run_and_capture(body))
        # Assert
        assert env.from_agent is None

    def test_v1_turn_blank_from_agent_is_normalised_to_none(self) -> None:
        # Arrange
        body = {"text": "hi", "from_agent": ""}
        # Act
        env = asyncio.run(_run_and_capture(body))
        # Assert
        assert env.from_agent is None
