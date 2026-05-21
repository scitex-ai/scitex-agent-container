"""``post_turn_to_url`` stamps the requester ``from_agent`` into the body.

NO MOCKS. A real ``http.server`` on 127.0.0.1 captures the POSTed
``/v1/turn`` body; the test asserts the explicit ``from_agent`` rode
into it so the receiving runner's Stop hook can push a completion report
back to the requester. Mirrors the real-server pattern already used in
``test_peer.py``.

TQ: AAA markers, ≥3-word names, one assertion each.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from typing import Any

from scitex_agent_container._network.peer import post_turn_to_url


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _capture_post_body(*, from_agent: str | None) -> dict[str, Any]:
    """Run ``post_turn_to_url`` against a real receiver; return the captured body."""
    port = _free_port()
    captured: list[dict[str, Any]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            captured.append(json.loads(self.rfile.read(length).decode("utf-8")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"text": "ok", "exit_after": False}).encode("utf-8")
            )

        def log_message(self, *a, **kw):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        post_turn_to_url(
            f"http://127.0.0.1:{port}/v1/turn",
            "hello",
            timeout_s=5.0,
            from_agent=from_agent,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    return captured[0]


class TestPeerRequesterBody:
    def test_explicit_from_agent_rides_into_post_body(self) -> None:
        # Arrange
        # Act
        body = _capture_post_body(from_agent="lead")
        # Assert
        assert body["from_agent"] == "lead"

    def test_post_body_always_carries_a_dispatch_id(self) -> None:
        # Arrange
        # Act
        body = _capture_post_body(from_agent="lead")
        # Assert
        assert isinstance(body.get("dispatch_id"), str)

    def test_post_body_carries_the_turn_text(self) -> None:
        # Arrange
        # Act
        body = _capture_post_body(from_agent="lead")
        # Assert
        assert body["text"] == "hello"
