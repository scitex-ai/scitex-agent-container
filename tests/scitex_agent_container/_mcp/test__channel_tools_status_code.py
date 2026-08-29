"""``status_code`` (ADR-0007) on the ``a2a_send`` MCP tool's success path.

Companion to ``test__channel_send_errors.py`` (the FAILURE side —
``not_running_error`` / ``unknown_target_error`` already carry
``status_code`` via ``_error_payload``, tested there) and
``test_channel_tools_dispatch_ledger.py`` (whose ``_ToolRecorder`` /
``http.server`` stub this file's harness mirrors, so it needs no
PostgreSQL: these tests never touch the dispatch ledger's content, only
the tool's own JSON reply).

No mocks: a real ``http.server`` loopback stub stands in for ``sac
listen`` and returns a REAL ``delivered_subscriber_count`` in its body,
which is exactly what makes this a genuine, non-fabricated measurement
(unlike the ``agent_send`` dispatch path fixed in
``cli_pkg._send_dispatch_nonblocking`` — see
``test__send_dispatch_nonblocking.py`` for that half of the incident).

AAA markers, one assertion per test (STX-TQ002 / STX-TQ007).
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading


class _ToolRecorder:
    """Hand-rolled stand-in for an mcp lowlevel Server (real object, not a Mock)."""

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


def _start_listen_stub(delivered_subscriber_count: int):
    """A loopback http.server returning a REAL subscriber count."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {
                "msg_id": "m1",
                "to_agent": "bob",
                "delivered_subscriber_count": delivered_subscriber_count,
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, *a, **kw):
            pass

    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _invoke_a2a_send(listen_url: str, target: str, content: str) -> dict:
    """Register the tools against a recorder, run a2a_send once, return the body."""
    from mcp.types import CallToolResult

    from scitex_agent_container._mcp._channel_tools import register_tools

    rec = _ToolRecorder()
    register_tools(rec, agent_name="alice", listen_url=listen_url, bearer=None)
    out = asyncio.run(
        rec.call_tool_fn("a2a_send", {"target": target, "content": content})
    )
    blocks = out.content if isinstance(out, CallToolResult) else out
    return json.loads(blocks[0].text)


# ---------------------------------------------------------------------------
# Success — a real, measured delivered_subscriber_count carries a real
# StatusCode (http 202, final=False).
# ---------------------------------------------------------------------------


def test_a2a_send_success_body_carries_status_code():
    # Arrange
    server, thread, port = _start_listen_stub(1)
    try:
        # Act
        body = _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert "status_code" in body


def test_a2a_send_success_status_code_is_http_202():
    # Arrange
    server, thread, port = _start_listen_stub(1)
    try:
        # Act
        body = _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert (body["status_code"]["kind"], body["status_code"]["code"]) == ("http", 202)


def test_a2a_send_success_status_code_is_not_final():
    # Arrange — persisted + enqueued is not the same as READ (card
    # sac-a2a-send-may-report-dispatch-as-arrival-20260821's own finding).
    server, thread, port = _start_listen_stub(1)
    try:
        # Act
        body = _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    from scitex_dev.status import StatusCode

    sc = StatusCode.from_dict(body["status_code"])
    assert sc.final is False


def test_a2a_send_success_status_code_message_carries_the_real_count():
    # Arrange — the count in the message is the REAL, measured fan-out,
    # never a fabricated constant.
    server, thread, port = _start_listen_stub(3)
    try:
        # Act
        body = _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert "3 live inbox subscriber(s)" in body["status_code"]["message"]


def test_a2a_send_success_names_a_probe_in_the_message():
    # Arrange — M2: a non-final http 202 MUST name a runnable probe.
    server, thread, port = _start_listen_stub(1)
    try:
        # Act
        body = _invoke_a2a_send(f"http://127.0.0.1:{port}", "bob", "hi")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
    # Assert
    assert "`a2a_inbox`" in body["status_code"]["message"]


# EOF
