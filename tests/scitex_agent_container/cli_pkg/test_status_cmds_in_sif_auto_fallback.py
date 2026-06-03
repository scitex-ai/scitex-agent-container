"""Tests for the PR-3 in-SIF auto-fallback on ``sac agents status``.

When the per-agent ``status <name>`` verb is invoked inside a SIF,
the CLI auto-proxies to the host's ``GET /agents/<name>/status``
and emits one wire-stable :func:`_in_sif_outcome.outcome_to_stdout_json`
line to stdout, exiting with the table-mapped code.

Real CliRunner + a tiny in-process HTTP server fixture (no mocks;
PA-306). AAA + one assert per test (PA-307).
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.status_cmds import status

# ---------------------------------------------------------------------------
# In-process HTTP server for the host-listen leg
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_host_listen(env_save_restore):
    """Tiny HTTP server fixture (mirror of the DELETE auto-fallback
    fixture). Sets SAC_LISTEN_BASE_URL + bearer + APPTAINER_CONTAINER
    so :func:`is_in_sif` returns True for the CLI under test."""
    import http.server
    import socketserver
    import threading

    captured: list[dict] = []
    response_queue: list[tuple[int, bytes]] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            captured.append({"method": "GET", "path": self.path})
            status, body = (
                response_queue.pop(0)
                if response_queue
                else (200, b'{"name":"x","session_id":null}')
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

    class _ServerHandle:
        @property
        def captured(self) -> list[dict]:
            return captured

        def enqueue(self, status: int, body: bytes) -> None:
            response_queue.append((status, body))

    try:
        yield _ServerHandle()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# In-SIF status — happy path
# ---------------------------------------------------------------------------


def test_in_sif_status_exits_0_on_success(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        200,
        b'{"name":"alice","spec_path":"...","session_id":"sess-1","state_dir":"..."}',
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["alice"])
    # Assert
    assert result.exit_code == 0


def test_in_sif_status_emits_outcome_json(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        200,
        b'{"name":"alice","session_id":"sess-1"}',
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["alice"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["ok"] is True


def test_in_sif_status_proxies_get_to_host_listen(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"name":"alice"}')
    runner = CliRunner()
    # Act
    runner.invoke(status, ["alice"])
    # Assert — captured request must be GET /agents/alice/status.
    captured = fake_host_listen.captured
    assert captured == [{"method": "GET", "path": "/agents/alice/status"}]


def test_in_sif_status_passes_through_host_body_in_details(fake_host_listen):
    # Arrange — PR-1 surface contract: host body lands under
    # outcome.details verbatim.
    body = b'{"name":"alice","session_id":null,"status":"startup_failed","startup_failed":{"kind":"apptainer_mount_failed"}}'
    fake_host_listen.enqueue(200, body)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["alice"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["details"]["status"] == "startup_failed"


# ---------------------------------------------------------------------------
# In-SIF status — 403 ACL deny → exit 5
# ---------------------------------------------------------------------------


def test_in_sif_status_exits_5_on_acl_deny(fake_host_listen):
    # Arrange — host returns the PR-3 acl_deny shape on lineage
    # mismatch.
    fake_host_listen.enqueue(
        403,
        b'{"error":"ACL deny","kind":"acl_deny","reason":"unrelated target"}',
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["unrelated-target"])
    # Assert
    assert result.exit_code == 5


def test_in_sif_status_emits_kind_acl_deny(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        403,
        b'{"error":"ACL deny","kind":"acl_deny","reason":"x"}',
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["unrelated"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["kind"] == "acl_deny"


# ---------------------------------------------------------------------------
# In-SIF status — transport error → exit 1
# ---------------------------------------------------------------------------


def test_in_sif_status_exits_1_on_transport_error(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    env_save_restore.set("SAC_LISTEN_BEARER", "x")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["any-name"])
    # Assert
    assert result.exit_code == 1


def test_in_sif_status_emits_kind_transport(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    env_save_restore.set("SAC_LISTEN_BEARER", "x")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["any-name"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["kind"] == "transport"


# ---------------------------------------------------------------------------
# Non-SIF path: legacy local behaviour preserved
# ---------------------------------------------------------------------------


def test_non_sif_path_does_not_proxy(env_save_restore, tmp_path):
    # Arrange — explicitly NOT in a SIF.
    env_save_restore.delete("APPTAINER_CONTAINER")
    env_save_restore.delete("SINGULARITY_CONTAINER")
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "rt"))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path / "agents"))
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["nonexistent"])
    # Assert — non-SIF path raises an error via agent_status; the
    # output is the legacy error text (not an outcome JSON).
    assert '"ok": true' not in result.output


# ---------------------------------------------------------------------------
# Fleet view (no name) is NOT auto-proxied (out of scope)
# ---------------------------------------------------------------------------


def test_fleet_view_does_not_proxy(fake_host_listen):
    # Arrange — even with SIF detection enabled, ``sac agents status``
    # without a name (fleet view) keeps the local-Registry path. The
    # in-SIF auto-fallback covers the per-agent verb only — fleet view
    # is a follow-up.
    runner = CliRunner()
    # Act — fleet view; capture either succeeds locally (empty
    # registry) or errors but should NOT proxy to the HTTP fixture.
    runner.invoke(status, [])
    # Assert — the fake server saw NO requests.
    assert fake_host_listen.captured == []
