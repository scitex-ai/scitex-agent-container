"""Dispatch-ledger integration for the outbound peer client.

``post_turn_to_url`` mints a dispatch_id, records a ``sent`` row before
the POST, stamps the id into the request body, and transitions the row
to ``delivered`` / ``timeout`` / ``failed`` once the round-trip resolves.

No mocks: a real ``http.server`` on loopback mimics ``/v1/turn`` and the
real ``state.db`` lives under ``tmp_path`` (isolated via env). The stub
server captures the request body so we can assert the dispatch_id was
actually put on the wire.

Conventions: AAA markers, one assertion per test (STX-TQ).
"""

from __future__ import annotations

import http.server
import importlib
import json
import os
import socket
import threading
from pathlib import Path

import pytest

from scitex_agent_container._network.peer import PeerError, post_turn_to_url


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db, exported via env (explicit save/restore, no mock)."""
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(handler_cls):
    """Spin a daemon http.server on a free loopback port; return (server, thread, port)."""
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _echo_handler(captured: list[dict]):
    """A /v1/turn handler that records the request body and echoes text."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            captured.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"text": f"echo:{body.get('text', '')}"}).encode("utf-8")
            )

        def log_message(self, *a, **kw):
            pass

    return _Handler


def _error_handler(status: int):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"boom")

        def log_message(self, *a, **kw):
            pass

    return _Handler


# ---------------------------------------------------------------------------
# Clean round-trip — ledger row created and moved to delivered.
# ---------------------------------------------------------------------------


def test_post_turn_records_ledger_row_to_agent(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    server, thread, port = _start_server(_echo_handler([]))
    try:
        # Act
        post_turn_to_url(
            f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0, to_agent="bob"
        )
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert rows[0]["to_agent"] == "bob"


def test_post_turn_marks_clean_roundtrip_delivered(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    server, thread, port = _start_server(_echo_handler([]))
    try:
        # Act
        post_turn_to_url(f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0)
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert rows[0]["status"] == "delivered"


def test_post_turn_stamps_dispatch_id_into_request_body(db_path: Path):
    # Arrange — the stub captures the wire body so we can assert the id
    # was actually sent (receiver-side correlation).
    captured: list[dict] = []
    server, thread, port = _start_server(_echo_handler(captured))
    try:
        # Act
        post_turn_to_url(f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert len(captured[0]["dispatch_id"]) == 32


def test_post_turn_wire_dispatch_id_matches_ledger_row(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    captured: list[dict] = []
    server, thread, port = _start_server(_echo_handler(captured))
    try:
        # Act
        post_turn_to_url(f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0)
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert — the id on the wire is the id in the ledger.
    assert captured[0]["dispatch_id"] == rows[0]["dispatch_id"]


# ---------------------------------------------------------------------------
# Failure paths — HTTP error and unreachable host mark the row failed.
# ---------------------------------------------------------------------------


def test_post_turn_marks_http_error_failed(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    server, thread, port = _start_server(_error_handler(500))
    raised: list[BaseException] = []
    try:
        # Act
        try:
            post_turn_to_url(f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0)
        except PeerError as exc:
            raised.append(exc)
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert rows[0]["status"] == "failed"


def test_post_turn_marks_unreachable_failed(db_path: Path):
    # Arrange — port 1 on loopback is unrouteable → URLError → failed.
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    raised: list[BaseException] = []
    # Act
    try:
        post_turn_to_url("http://127.0.0.1:1/v1/turn", "x", timeout_s=1.0)
    except PeerError as exc:
        raised.append(exc)
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == "failed"


def test_post_turn_still_returns_reply_when_ledger_records(db_path: Path):
    # Arrange — the dispatch itself must succeed regardless of ledger.
    server, thread, port = _start_server(_echo_handler([]))
    try:
        # Act
        reply = post_turn_to_url(
            f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert reply == "echo:hi"
