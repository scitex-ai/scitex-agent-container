"""Tests for ``sac agents spawn-from-here``.

PR-3 Checkpoint 3 — pins the wire-stable outcome JSON + exit code
the SAC-from-SAC consumer (clew launcher, parent agent scripts)
gets from a POST /agents through this dedicated verb.

PA-306: real CliRunner + in-process HTTP server (no mocks). PA-307:
AAA + one-assert per test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._spawn_from_here import spawn_from_here


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
                else (200, b'{"name":"x","returncode":0}')
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
# Happy path — POST + outcome
# ---------------------------------------------------------------------------


def test_spawn_from_here_exits_0_on_success(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"name":"child","returncode":0}')
    runner = CliRunner()
    # Act
    result = runner.invoke(spawn_from_here, ["child"])
    # Assert
    assert result.exit_code == 0


def test_spawn_from_here_posts_to_agents_endpoint(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"ok":true}')
    runner = CliRunner()
    # Act
    runner.invoke(spawn_from_here, ["child"])
    captured = fake_host_listen.captured
    # Assert
    assert captured[0]["method"] == "POST" and captured[0]["path"] == "/agents"


def test_spawn_from_here_includes_child_name_in_body(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, b'{"ok":true}')
    runner = CliRunner()
    # Act
    runner.invoke(spawn_from_here, ["my-child"])
    body = json.loads(fake_host_listen.captured[0]["body"])
    # Assert
    assert body["name"] == "my-child"


def test_spawn_from_here_resolves_caller_from_sac_name_env(
    fake_host_listen, env_save_restore
):
    # Arrange — SAC_NAME env populates the caller field automatically.
    env_save_restore.set("SAC_NAME", "parent-launcher")
    fake_host_listen.enqueue(200, b'{"ok":true}')
    runner = CliRunner()
    # Act
    runner.invoke(spawn_from_here, ["child"])
    body = json.loads(fake_host_listen.captured[0]["body"])
    # Assert
    assert body["caller"] == "parent-launcher"


def test_spawn_from_here_explicit_caller_overrides_env(
    fake_host_listen, env_save_restore
):
    # Arrange
    env_save_restore.set("SAC_NAME", "from-env")
    fake_host_listen.enqueue(200, b'{"ok":true}')
    runner = CliRunner()
    # Act
    runner.invoke(spawn_from_here, ["child", "--caller", "explicit"])
    body = json.loads(fake_host_listen.captured[0]["body"])
    # Assert
    assert body["caller"] == "explicit"


# ---------------------------------------------------------------------------
# Spec file inline POST
# ---------------------------------------------------------------------------


def test_spawn_from_here_inlines_spec_from_file(fake_host_listen, tmp_path):
    # Arrange — write a real spec.yaml to disk, then verify the
    # parsed body lands inside the POST body.
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"workdir": "/tmp"},
    }
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec))
    fake_host_listen.enqueue(200, b'{"ok":true}')
    runner = CliRunner()
    # Act
    runner.invoke(spawn_from_here, ["child", "--spec-file", str(path)])
    body = json.loads(fake_host_listen.captured[0]["body"])
    # Assert
    assert body["spec"] == spec


def test_spawn_from_here_overwrite_flag_threads_through(fake_host_listen, tmp_path):
    # Arrange
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"workdir": "/tmp"},
    }
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec))
    fake_host_listen.enqueue(200, b'{"ok":true}')
    runner = CliRunner()
    # Act
    runner.invoke(
        spawn_from_here,
        ["child", "--spec-file", str(path), "--overwrite"],
    )
    body = json.loads(fake_host_listen.captured[0]["body"])
    # Assert
    assert body["overwrite"] is True


# ---------------------------------------------------------------------------
# Server-side error mapping
# ---------------------------------------------------------------------------


def test_spawn_from_here_exits_2_on_bind_unresolvable(fake_host_listen):
    # Arrange — PR-1 preflight body shape.
    fake_host_listen.enqueue(
        400,
        b'{"error":"...","kind":"bind_unresolvable","details":{"binds":[]}}',
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(spawn_from_here, ["child"])
    # Assert
    assert result.exit_code == 2


def test_spawn_from_here_exits_4_on_already_exists(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(409, b'{"error":"...","kind":"already_exists"}')
    runner = CliRunner()
    # Act
    result = runner.invoke(spawn_from_here, ["existing"])
    # Assert
    assert result.exit_code == 4


def test_spawn_from_here_exits_5_on_acl_deny(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        403, b'{"error":"ACL deny","kind":"acl_deny","reason":"x"}'
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(spawn_from_here, ["child"])
    # Assert
    assert result.exit_code == 5


def test_spawn_from_here_exits_1_on_transport_error(env_save_restore):
    # Arrange — point at a definitely-unused port.
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    env_save_restore.set("SAC_LISTEN_BEARER", "x")
    runner = CliRunner()
    # Act
    result = runner.invoke(spawn_from_here, ["child"])
    # Assert
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI-side spec file error → spec_invalid outcome
# ---------------------------------------------------------------------------


def test_spawn_from_here_unreadable_spec_file_exits_3(fake_host_listen, tmp_path):
    # Arrange — file exists but is not valid YAML.
    path = tmp_path / "bad.yaml"
    path.write_text(":not valid yaml{")
    runner = CliRunner()
    # Act
    result = runner.invoke(spawn_from_here, ["child", "--spec-file", str(path)])
    # Assert
    assert result.exit_code == 3
