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
import urllib.error
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


def _post(url: str, body: dict) -> int:
    """POST a JSON body to ``url`` and return its actual HTTP status."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def _run_request(body: dict) -> tuple[int, list[TurnEnvelope]]:
    """Return the native receiver status and every envelope it enqueued."""
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
        status = await asyncio.to_thread(
            _post, f"http://127.0.0.1:{port}/v1/turn", body
        )
    finally:
        stop.set()
        await inbox.put(ShutdownEnvelope())
        await asyncio.wait_for(consumer, timeout=5.0)
        await asyncio.wait_for(server, timeout=5.0)
    return status, captured


async def _run_and_capture(body: dict) -> TurnEnvelope:
    """Spin the real sidecar, POST ``body`` to /v1/turn, return the envelope."""
    _, captured = await _run_request(body)
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

    def test_marker_bound_send_preserves_text_and_requester(self) -> None:
        # Arrange
        body = {
            "text": "hi\n<!-- delivery:d-1 -->",
            "visible_delivery_id": "d-1",
            "dispatch_id": "d-1",
            "from_agent": "lead",
        }
        # Act
        status, captured = asyncio.run(_run_request(body))
        # Assert
        assert (
            status,
            [(env.text, env.dispatch_id, env.from_agent) for env in captured],
        ) == (200, [(body["text"], "d-1", "lead")])

    @pytest.mark.parametrize(
        "extra",
        [
            {"visible_delivery_id": "missing"},
            {"visible_delivery_id": "d-10"},
            {"visible_delivery_id": 1},
            {"visible_delivery_id": "d-1", "untrusted": "must not pass"},
            {"visible_delivery_id": "d-1", "dispatch_id": 1},
            {"visible_delivery_id": "d-1", "from_agent": 1},
            {"visible_delivery_id": "d-1", "exit_after": "false"},
        ],
        ids=[
            "missing-marker",
            "prefix-marker",
            "invalid-id",
            "unknown-field",
            "invalid-dispatch",
            "invalid-requester",
            "invalid-exit",
        ],
    )
    def test_invalid_visible_send_is_rejected_before_enqueue(self, extra) -> None:
        # Arrange
        body = {"text": "hi\n<!-- delivery:d-1 -->", **extra}
        # Act
        result = asyncio.run(_run_request(body))
        # Assert
        assert result == (400, [])
