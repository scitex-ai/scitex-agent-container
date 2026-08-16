"""Tests for the PR-3 in-SIF auto-fallback on ``sac agents send``.

When ``sac agents send <name> <prompt>`` is invoked inside a SIF
(prompt mode; ``--key`` SIGINT mode is excluded — it needs local
pid access), the CLI auto-proxies to ``POST /agents/<name>/send``
and emits one wire-stable outcome JSON line to stdout, exiting
with the table-mapped code.

PA-306: real CliRunner + in-process HTTP server. PA-307: AAA +
one-assert.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.send_cmds import send


@pytest.fixture
def fake_host_listen(env_save_restore):
    import http.server
    import socketserver
    import threading

    captured: list[dict] = []
    response_queue: list[tuple[int, bytes]] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            captured.append({"method": "POST", "path": self.path, "body": raw})
            status, body = (
                response_queue.pop(0)
                if response_queue
                else (200, b'{"name":"x","sent":true}')
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kw):  # noqa: ARG002
            return

    server = socketserver.TCPServer(("127.0.0.1", 0), _H)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    env_save_restore.set("SAC_LISTEN_BASE_URL", f"http://127.0.0.1:{port}")
    env_save_restore.set("SAC_LISTEN_BEARER", "test-bearer")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")

    class _H_:
        @property
        def captured(self) -> list[dict]:
            return captured

        def enqueue(self, status: int, body: bytes) -> None:
            response_queue.append((status, body))

    try:
        yield _H_()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# In-SIF send — happy path
# ---------------------------------------------------------------------------


def test_in_sif_send_exits_0_on_success(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"name":"alice","sent":true}')
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alice", "hello world"])
    # Assert
    assert result.exit_code == 0


def test_in_sif_send_posts_to_send_endpoint(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"name":"alice","sent":true}')
    runner = CliRunner()
    # Act
    runner.invoke(send, ["alice", "hello world"])
    captured = fake_host_listen.captured
    # Assert
    assert (
        captured[0]["method"] == "POST" and captured[0]["path"] == "/agents/alice/send"
    )


def test_in_sif_send_includes_prompt_in_request_body(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"ok":true}')
    runner = CliRunner()
    # Act
    runner.invoke(send, ["alice", "do the thing"])
    body = json.loads(fake_host_listen.captured[0]["body"])
    # Assert
    assert body["prompt"] == "do the thing"


def test_in_sif_send_outcome_json_carries_ok_true(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"name":"alice","sent":true}')
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["alice", "hi"])
    parsed = json.loads(result.stdout)
    # Assert
    assert parsed["ok"] is True


# ---------------------------------------------------------------------------
# In-SIF send — 403 ACL deny → exit 5
# ---------------------------------------------------------------------------


def test_in_sif_send_exits_5_on_acl_deny(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        403,
        b'{"error":"ACL deny","kind":"acl_deny","reason":"cross-lineage send"}',
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["unrelated-target", "hi"])
    # Assert
    assert result.exit_code == 5


def test_in_sif_send_emits_kind_acl_deny(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        403, b'{"error":"ACL deny","kind":"acl_deny","reason":"x"}'
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["unrelated", "hi"])
    parsed = json.loads(result.stdout)
    # Assert
    assert parsed["kind"] == "acl_deny"


# ---------------------------------------------------------------------------
# In-SIF send — transport error → exit 1
# ---------------------------------------------------------------------------


def test_in_sif_send_exits_1_on_transport_error(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    env_save_restore.set("SAC_LISTEN_BEARER", "x")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(send, ["any-name", "hi"])
    # Assert
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Excluded: --key (SIGINT) path NOT proxied (needs local pid)
# ---------------------------------------------------------------------------


def test_in_sif_send_with_key_does_not_proxy(fake_host_listen):
    # Arrange — --key ESC needs the local pid file to send SIGINT;
    # the in-SIF path explicitly excludes it so the existing
    # ClickException about missing pid is the visible failure.
    runner = CliRunner()
    # Act — alice has no local pid; click should fail with no pid.
    result = runner.invoke(send, ["alice", "--key", "ESC"])
    # Assert — fake server received NO requests.
    assert fake_host_listen.captured == []
