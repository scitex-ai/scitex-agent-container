"""Tests for the PR-3 in-SIF auto-fallback on ``sac agents delete``.

When the CLI is invoked inside an apptainer SIF, the local
filesystem doesn't carry the host registry and there's no useful
pid file to SIGTERM. The in-SIF path auto-proxies each DELETE to
the host's ``sac listen`` and emits one wire-stable JSON line per
name to stdout, exiting with the highest exit code seen across
the batch.

This file exercises the splice point at the top of the ``delete``
CLI command:

  * SIF detected → :func:`_delete_via_host_listen` invoked, the
    host-listen HTTP path runs, the local rmtree / registry path
    is NOT touched;
  * non-SIF → existing local behaviour preserved (no regression).

Real Click ``CliRunner`` (PA-306: no mocks; the SIF detection is
toggled by setting/unsetting the env var the production code reads).
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._delete import delete

# ---------------------------------------------------------------------------
# Test-double opener used to redirect the in-SIF HTTP path
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _patch_urlopen_via_module(monkeypatch_alt, fake):
    """Swap the urllib opener used inside the in-SIF HTTP client.

    Real wiring: the CLI verb calls
    :func:`_in_sif_http_client.host_listen_call(...)` which goes
    through ``urllib.request.urlopen``. We swap that one symbol
    via a thin module-level binding so PA-306 (no monkeypatch)
    stays clean — the test redirects the production seam (urllib)
    by writing a value into a module attribute the production
    code already exposes through the ``opener=`` kwarg.

    Concretely: we cannot pass ``opener=`` from the CLI surface,
    so we patch ``urllib.request.urlopen`` via the env-set
    SAC_LISTEN_BASE_URL pointing at a tiny in-process HTTP server
    started by the test. That keeps PA-306 clean: no
    ``unittest.mock``, only real-IO test doubles.
    """
    # Not used in this file — kept here as a placeholder so the
    # comment block above is anchored. Real wiring below uses a
    # tiny in-process HTTP server (the canonical PA-306 idiom).


# ---------------------------------------------------------------------------
# In-process HTTP server stub for the host-listen leg
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_host_listen(env_save_restore):
    """Run a tiny HTTP server in a thread that captures + responds
    to the in-SIF CLI's DELETE requests. The fixture sets
    SAC_LISTEN_BASE_URL to the server URL and ALSO sets APPTAINER_CONTAINER
    so :func:`is_in_sif` returns True for the CLI under test.

    Yields the server object so tests can configure its next
    response and inspect the captured request list.
    """
    import http.server
    import socketserver
    import threading

    captured: list[dict] = []
    response_queue: list[tuple[int, bytes]] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_DELETE(self):  # noqa: N802 — std lib name
            captured.append({"method": "DELETE", "path": self.path})
            status, body = (
                response_queue.pop(0)
                if response_queue
                else (200, b'{"name":"x","stopped":true}')
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kw):  # noqa: ARG002 — quiet stdlib spam
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
# In-SIF DELETE — happy path → exit 0 + JSON
# ---------------------------------------------------------------------------


def test_in_sif_delete_exits_0_on_success(fake_host_listen):
    # Arrange — host responds with 200 OK.
    fake_host_listen.enqueue(200, b'{"name":"alice","stopped":true,"pid":1234}')
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["alice"])
    # Assert
    assert result.exit_code == 0


def test_in_sif_delete_emits_outcome_json_to_stdout(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"name":"alice","stopped":true,"pid":1234}')
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["alice"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["ok"] is True


def test_in_sif_delete_emits_http_status_in_outcome(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"name":"alice"}')
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["alice"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["http_status"] == 200


# ---------------------------------------------------------------------------
# In-SIF DELETE — 403 lineage ACL deny → exit 5
# ---------------------------------------------------------------------------


def test_in_sif_delete_exits_5_on_acl_deny(fake_host_listen):
    # Arrange — host returns the PR-3 5th-kind ACL deny shape.
    fake_host_listen.enqueue(
        403, b'{"error":"ACL deny","kind":"acl_deny","reason":"unrelated target"}'
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["unrelated-target"])
    # Assert — exit_code 5 per the PR-3 Checkpoint 2 table.
    assert result.exit_code == 5


def test_in_sif_delete_emits_kind_acl_deny_in_outcome(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        403, b'{"error":"ACL deny","kind":"acl_deny","reason":"x"}'
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["unrelated-target"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["kind"] == "acl_deny"


# ---------------------------------------------------------------------------
# In-SIF DELETE — 410 stillborn (PR-1) → exit 6
# ---------------------------------------------------------------------------


def test_in_sif_delete_exits_6_on_startup_failed(fake_host_listen):
    # Arrange — host returns the PR-1 410 flat-summary STARTUP_FAILED.
    body = (
        b'{"name":"zombie","status":"startup_failed",'
        b'"kind":"startup_failed","phase":"container_creation",'
        b'"runtime_dir":"/home/u/.scitex/...","details":{}}'
    )
    fake_host_listen.enqueue(410, body)
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["zombie"])
    # Assert
    assert result.exit_code == 6


# ---------------------------------------------------------------------------
# In-SIF DELETE — batch (multi-name) emits one JSON line per name
# ---------------------------------------------------------------------------


def test_in_sif_delete_batch_emits_one_json_line_per_name(fake_host_listen):
    # Arrange — 3 names, all 200 OK.
    fake_host_listen.enqueue(200, b'{"name":"a"}')
    fake_host_listen.enqueue(200, b'{"name":"b"}')
    fake_host_listen.enqueue(200, b'{"name":"c"}')
    runner = CliRunner()
    # Act — batch mode does NOT require --yes/-y in the in-SIF path
    # because the host gate is the ultimate authority.
    result = runner.invoke(delete, ["a", "b", "c"])
    lines = [line for line in result.output.strip().split("\n") if line]
    # Assert
    assert len(lines) == 3


def test_in_sif_delete_batch_exit_is_max_of_per_name_codes(fake_host_listen):
    # Arrange — first DELETE succeeds (0), second hits ACL deny (5).
    fake_host_listen.enqueue(200, b'{"name":"a"}')
    fake_host_listen.enqueue(
        403, b'{"error":"ACL deny","kind":"acl_deny","reason":"x"}'
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["a", "b"])
    # Assert — exit code should be the worst (= 5, ACL deny).
    assert result.exit_code == 5


# ---------------------------------------------------------------------------
# In-SIF DELETE — transport error → exit 1
# ---------------------------------------------------------------------------


def test_in_sif_delete_exits_1_on_transport_error(env_save_restore):
    # Arrange — point at a definitely-unused port so the connection
    # is refused immediately.
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    env_save_restore.set("SAC_LISTEN_BEARER", "x")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["any-name"])
    # Assert
    assert result.exit_code == 1


def test_in_sif_delete_emits_kind_transport_on_transport_error(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    env_save_restore.set("SAC_LISTEN_BEARER", "x")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    runner = CliRunner()
    # Act
    result = runner.invoke(delete, ["any-name"])
    parsed = json.loads(result.output.strip())
    # Assert
    assert parsed["kind"] == "transport"


# ---------------------------------------------------------------------------
# Non-SIF path: existing local behaviour preserved
# ---------------------------------------------------------------------------


def test_non_sif_path_does_not_proxy(env_save_restore, tmp_path):
    # Arrange — explicitly unset SIF detection envs + ensure
    # SCITEX_AGENT_CONTAINER_RUNTIME_DIR points at a clean tmp so
    # the existing local logic sees zero matching dirs / registry.
    env_save_restore.delete("APPTAINER_CONTAINER")
    env_save_restore.delete("SINGULARITY_CONTAINER")
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "rt"))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path / "agents"))
    runner = CliRunner()
    # Act — agent doesn't exist locally; existing path emits
    # "[skip] 'x': not found" and exits 1.
    result = runner.invoke(delete, ["nonexistent"])
    # Assert — output is the legacy text, not an outcome JSON.
    assert "not found" in result.output
