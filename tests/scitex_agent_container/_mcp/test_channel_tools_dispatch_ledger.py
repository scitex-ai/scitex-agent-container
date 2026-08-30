"""Dispatch-ledger integration for the a2a_send MCP tool.

``register_tools`` wires the send-side ``a2a_*`` tools. ``a2a_send``
mints a dispatch_id, records a ``sent`` ledger row (from_agent=this
agent, to_agent=target, conversation_id), stamps the id into the
A2A ``params.metadata``, then transitions the row to ``delivered`` /
``failed`` on the send outcome.

``a2a_send`` also stamps ``agent="alice"`` — the OWNING agent — since the
ledger moved to the fleet-wide PostgreSQL store on 2026-08-28. It happens to
equal ``from_agent`` here, which is why one test below asserts the two
SEPARATELY rather than treating either as evidence of the other.

No mocks: the ``a2a_send`` tool POSTs to ``{listen_url}/agents/<target>
/message:send`` over real httpx; a real loopback ``http.server`` stands
in for ``sac listen`` and captures the wire body. The ledger lives in a real,
throwaway PostgreSQL schema (the ``pg_schema`` fixture) — ``db_path`` is gone,
because it named a file and there is no file. The mcp ``server`` is
a tiny hand-rolled recorder exposing only the two decorator methods
``register_tools`` uses (``list_tools`` / ``call_tool``) — a real
collaborator object, not a Mock.

Conventions: AAA markers, one assertion per test (STX-TQ).
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading


class _ToolRecorder:
    """Hand-rolled stand-in for an mcp lowlevel Server.

    Exposes only the two decorator methods ``register_tools`` calls.
    Captures the registered ``call_tool`` coroutine so the test can
    invoke it directly. Real object, not a Mock.
    """

    def __init__(self) -> None:
        self.call_tool_fn = None
        self.list_tools_fn = None

    def list_tools(self):
        def deco(fn):
            self.list_tools_fn = fn
            return fn

        return deco

    def call_tool(self):
        def deco(fn):
            self.call_tool_fn = fn
            return fn

        return deco


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_listen_stub(status: int, captured: list[dict]):
    """A loopback http.server mimicking sac listen's message:send route."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            captured.append(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {"delivered_subscriber_count": 1} if status < 400 else {}
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, *a, **kw):
            pass

    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _invoke_a2a_send(listen_url: str, target: str, content: str):
    """Register the tools against a recorder and run a2a_send once."""
    from scitex_agent_container._mcp._channel_tools import register_tools

    rec = _ToolRecorder()
    register_tools(rec, agent_name="alice", listen_url=listen_url, bearer=None)
    return asyncio.run(
        rec.call_tool_fn("a2a_send", {"target": target, "content": content})
    )


# ---------------------------------------------------------------------------
# Clean send — ledger row created (from alice, to target) and delivered.
# ---------------------------------------------------------------------------


def test_a2a_send_records_ledger_row_from_this_agent(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    server, thread, port = _start_listen_stub(200, [])
    try:
        # Act
        _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert rows[0]["from_agent"] == "alice"


def test_a2a_send_scopes_the_row_to_this_agent(pg_schema: str):
    # Arrange — a FOREIGN row is planted first, and it is what makes this test
    # able to fail: with only alice's own row present, an unscoped read and a
    # scoped one return the same single row and the assertion proves nothing.
    # Measured — without the foreign row this test passes even with the
    # owning-agent filter deleted from list_dispatches.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(agent="carol", from_agent="carol", to_agent="dave", text="theirs")
    server, thread, port = _start_listen_stub(200, [])
    try:
        # Act
        _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
        rows = list_dispatches(agent="alice")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert [r["to_agent"] for r in rows] == ["bob"]


def test_a2a_send_records_target_as_to_agent(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    server, thread, port = _start_listen_stub(200, [])
    try:
        # Act
        _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert rows[0]["to_agent"] == "bob"


def test_a2a_send_marks_clean_send_delivered(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    server, thread, port = _start_listen_stub(200, [])
    try:
        # Act
        _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert rows[0]["status"] == "delivered"


def test_a2a_send_stamps_dispatch_id_into_metadata(pg_schema: str):
    # Arrange — capture the A2A envelope so we can read params.metadata.
    captured: list[dict] = []
    server, thread, port = _start_listen_stub(200, captured)
    try:
        # Act
        _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert len(captured[0]["params"]["metadata"]["dispatch_id"]) == 32


def test_a2a_send_wire_dispatch_id_matches_ledger_row(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    captured: list[dict] = []
    server, thread, port = _start_listen_stub(200, captured)
    try:
        # Act
        _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert captured[0]["params"]["metadata"]["dispatch_id"] == rows[0]["dispatch_id"]


# ---------------------------------------------------------------------------
# Failure path — a 5xx from listen marks the row failed (SendError).
# ---------------------------------------------------------------------------


def test_a2a_send_marks_server_error_failed(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import list_dispatches

    server, thread, port = _start_listen_stub(500, [])
    try:
        # Act
        _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
        rows = list_dispatches()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert rows[0]["status"] == "failed"
