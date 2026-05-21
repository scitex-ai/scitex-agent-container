"""Sender-side interpretation of the peer's honest 504 timeout body.

Tests:
  1. ``interpret_timeout_body`` builds a neutral PeerTimeoutPending from
     the honest shape (status / heartbeat / possibilities present).
  2. ``interpret_timeout_body`` degrades to a generic message when the
     body is empty / missing the honest shape (older peer).
  3. ``interpret_timeout_body`` does not crash when heartbeat is None.
  4. ``post_turn_to_url`` raises PeerTimeoutPending (NOT plain PeerError)
     against a REAL local server returning 504 + honest body.
  5. ``post_turn_to_url`` raises PeerTimeoutPending against a REAL local
     504 WITHOUT the honest body (generic interpretation).
  6. A genuine non-504 transport failure still raises plain PeerError
     (the in-progress path is 504-only).
  7. PeerTimeoutPending IS a PeerError subclass (back-compat catch).
  8. ``sac peer post-turn`` exits 0 and prints the interpretation when
     the peer returns a 504 honest body (in-progress, not failure).

HARD RULE — NO MOCKS: every transport test runs against a real
``http.server`` on 127.0.0.1; the connection-failure test points at an
unrouteable loopback port. STX-TQ: AAA markers, descriptive names, one
assertion per test.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

from scitex_agent_container._network._peer_timeout import (
    PeerTimeoutPending,
    interpret_timeout_body,
)
from scitex_agent_container._network.peer import PeerError, post_turn_to_url

_HONEST_BODY = {
    "status": "timeout_wait_elapsed",
    "detail": "The bounded HTTP wait of 120s elapsed; the turn may still be running.",
    "possibilities": ["turn still draining", "agent in a long tool call"],
    "timeout_s": 120.0,
    "session_id": "sid-abc",
    "heartbeat": {"state": "working", "ts": 1700000000.0, "elapsed_s": 215.0},
    "error": "turn exceeded 120s timeout",
}


def _start_504_server(body: bytes):
    """Spin up a daemon http.server on 127.0.0.1 that returns 504 + ``body``.

    Returns ``(server, thread, port)``. Caller is responsible for
    ``server.shutdown()`` + ``thread.join()``.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **kw):  # silence test output
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


# ---------------------------------------------------------------------------
# 1-3 — interpret_timeout_body unit behaviour (no transport)
# ---------------------------------------------------------------------------


class TestInterpretTimeoutBody:
    def test_honest_body_interpretation_states_not_a_failure(self) -> None:
        # Arrange
        body = dict(_HONEST_BODY)
        # Act
        exc = interpret_timeout_body(body, fallback_label="http://x/v1/turn")
        # Assert
        assert "NOT necessarily" in exc.interpretation

    def test_honest_body_preserves_session_id_field(self) -> None:
        # Arrange
        body = dict(_HONEST_BODY)
        # Act
        exc = interpret_timeout_body(body, fallback_label="http://x/v1/turn")
        # Assert
        assert exc.session_id == "sid-abc"

    def test_honest_body_renders_heartbeat_phase(self) -> None:
        # Arrange
        body = dict(_HONEST_BODY)
        # Act
        exc = interpret_timeout_body(body, fallback_label="http://x/v1/turn")
        # Assert
        assert "phase 'working'" in exc.interpretation

    def test_honest_body_joins_possibilities_list(self) -> None:
        # Arrange
        body = dict(_HONEST_BODY)
        # Act
        exc = interpret_timeout_body(body, fallback_label="http://x/v1/turn")
        # Assert
        assert "agent in a long tool call" in exc.interpretation

    def test_empty_body_produces_generic_may_still_be_running_message(self) -> None:
        # Arrange — older peer: no structured body at all.
        body = None
        # Act
        exc = interpret_timeout_body(body, fallback_label="mba:18888")
        # Assert
        assert "may still be running on mba:18888" in exc.interpretation

    def test_empty_body_status_field_is_none(self) -> None:
        # Arrange
        body = None
        # Act
        exc = interpret_timeout_body(body, fallback_label="mba:18888")
        # Assert
        assert exc.status is None

    def test_heartbeat_none_renders_unavailable_without_crashing(self) -> None:
        # Arrange — honest shape but the peer could not read live state.
        body = {
            "status": "timeout_wait_elapsed",
            "timeout_s": 60.0,
            "heartbeat": None,
            "possibilities": [],
        }
        # Act
        exc = interpret_timeout_body(body, fallback_label="p")
        # Assert
        assert "Peer heartbeat: unavailable." in exc.interpretation

    def test_pending_is_peer_error_subclass_for_backcompat_catch(self) -> None:
        # Arrange
        body = dict(_HONEST_BODY)
        # Act
        exc = interpret_timeout_body(body, fallback_label="x")
        # Assert
        assert isinstance(exc, PeerError)


# ---------------------------------------------------------------------------
# 4-6 — post_turn_to_url against a REAL local server (no mocks)
# ---------------------------------------------------------------------------


class TestPostTurnTimeoutOverHttp:
    def test_504_honest_body_raises_peer_timeout_pending(self) -> None:
        # Arrange
        server, thread, port = _start_504_server(
            json.dumps(_HONEST_BODY).encode("utf-8")
        )
        raised: list[BaseException] = []
        try:
            # Act
            try:
                post_turn_to_url(
                    f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
                )
            except PeerTimeoutPending as exc:
                raised.append(exc)
            # Assert
            assert raised and isinstance(raised[0], PeerTimeoutPending)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_504_honest_body_interpretation_not_raw_json(self) -> None:
        # Arrange
        server, thread, port = _start_504_server(
            json.dumps(_HONEST_BODY).encode("utf-8")
        )
        raised: list[PeerTimeoutPending] = []
        try:
            # Act
            try:
                post_turn_to_url(
                    f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
                )
            except PeerTimeoutPending as exc:
                raised.append(exc)
            # Assert — friendly text, not the raw JSON envelope.
            assert raised and "NOT necessarily" in raised[0].interpretation
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_504_honest_body_carries_structured_timeout_s(self) -> None:
        # Arrange
        server, thread, port = _start_504_server(
            json.dumps(_HONEST_BODY).encode("utf-8")
        )
        raised: list[PeerTimeoutPending] = []
        try:
            # Act
            try:
                post_turn_to_url(
                    f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
                )
            except PeerTimeoutPending as exc:
                raised.append(exc)
            # Assert
            assert raised and raised[0].timeout_s == 120.0
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_504_without_honest_body_raises_generic_pending(self) -> None:
        # Arrange — older peer: 504 with a non-JSON / non-honest body.
        server, thread, port = _start_504_server(b"Gateway Timeout")
        raised: list[BaseException] = []
        try:
            # Act
            try:
                post_turn_to_url(
                    f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
                )
            except PeerTimeoutPending as exc:
                raised.append(exc)
            # Assert
            assert raised and "may still be running" in raised[0].interpretation
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_connection_failure_raises_plain_peer_error_not_pending(self) -> None:
        # Arrange — port 1 on loopback is unrouteable → URLError.
        raised: list[BaseException] = []
        # Act
        try:
            post_turn_to_url("http://127.0.0.1:1/v1/turn", "x", timeout_s=1.0)
        except PeerError as exc:
            raised.append(exc)
        # Assert — a genuine transport failure is NOT in-progress.
        assert raised and not isinstance(raised[0], PeerTimeoutPending)
