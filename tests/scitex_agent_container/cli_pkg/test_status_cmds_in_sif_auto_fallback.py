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


# ---------------------------------------------------------------------------
# PR#316: graceful degrade when SAC_LISTEN_BASE_URL is missing in-SIF
#
# Lead msg 4cb474fc / clew L3 diag 2026-06-06: sac-from-sac L2 contexts
# (broker-self orchestrator, bare-host with stale SINGULARITY_CONTAINER
# env from a launcher hint) hit is_in_sif() == True but have no
# host-listen to proxy to — SAC_LISTEN_BASE_URL is empty. Pre-PR#316
# this surfaced as a stillborn-read masquerading as a transport error
# (HostListenTransportError "in-SIF host-listen call requires
# SAC_LISTEN_BASE_URL ... Got empty/unset."). Fix: fall through to the
# local Registry read instead — the broker-self happy path has the
# agent's state.db row reachable locally anyway. Tests pin the
# "no-listen-url → local read, not transport error" contract.
# ---------------------------------------------------------------------------


def test_status_in_sif_without_listen_url_does_not_proxy_to_http(
    env_save_restore, fake_host_listen
):
    # Arrange — in-SIF (APPTAINER_CONTAINER set by the fixture) but
    # SAC_LISTEN_BASE_URL is empty (operator-shell case, broker-self).
    # The fake_host_listen fixture installed a URL; explicitly unset
    # to simulate the broken-listen case.
    env_save_restore.set("SAC_LISTEN_BASE_URL", "")
    runner = CliRunner()
    # Act
    runner.invoke(status, ["any-target-name"])
    # Assert — the fake host listen received NO request: the in-SIF
    # path degraded gracefully to the local Registry read.
    assert fake_host_listen.captured == []


def test_status_in_sif_with_unset_listen_url_does_not_proxy_to_http(
    env_save_restore, fake_host_listen
):
    # Arrange — same scenario but env var is fully unset rather than
    # empty-string. Both shapes must degrade identically.
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    runner = CliRunner()
    # Act
    runner.invoke(status, ["any-target-name"])
    # Assert
    assert fake_host_listen.captured == []


def test_status_in_sif_without_listen_url_exit_is_not_transport_error(
    env_save_restore, fake_host_listen
):
    # Arrange — pin that the degraded path does NOT emit the
    # PR-3 outcome JSON with kind=transport (which the pre-PR#316
    # behaviour did). Whatever the local read returns is fine; the
    # contract is "not a stillborn transport error from the
    # host-listen call".
    env_save_restore.set("SAC_LISTEN_BASE_URL", "")
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["any-target-name"])
    # Assert — output (if any) is NOT the in-SIF transport outcome.
    assert '"in-SIF host-listen call requires SAC_LISTEN_BASE_URL"' not in result.output
