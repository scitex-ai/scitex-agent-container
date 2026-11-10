"""Relocation challenge sends use the installed CLI and preserve its refusal."""

import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from scitex_agent_container._lifecycle._relocate_shell import Shell, marked
from scitex_agent_container._lifecycle._relocate_target_ssh import (
    MARK_BOOT,
    deliver_prompt,
)


@contextmanager
def _owned_delivery(tmp_path, status):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            payload = json.dumps(
                {"reply": "owned reply", "error": "owned refusal"}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    agent = "owned-handshake-" + uuid.uuid4().hex
    payload_path = Path("/tmp") / f"sac-relocate-brief-{agent}.txt"
    payload_path.touch(exist_ok=False)
    payload_path.chmod(0o600)
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "HOME": str(tmp_path / "home"),
        "SCITEX_DIR": str(tmp_path / "state"),
        "SAC_AGENT_SCOPE": "user",
        "SAC_LISTEN_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "SAC_LISTEN_BEARER": "owned-token",
        "PYTHONNOUSERSITE": "1",
    }
    for key in ("COVERAGE_PROCESS_START", "COVERAGE_FILE"):
        if key in os.environ:
            env[key] = os.environ[key]

    def execute(argv, *, timeout_s):
        result = subprocess.run(
            argv, env=env, text=True, capture_output=True, timeout=min(timeout_s, 10)
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    try:
        yield agent, execute, calls
    finally:
        payload_path.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(("http_status", "exit_code"), [(200, 0), (403, 1)])
def test_challenge_send_reaches_cli_and_preserves_its_exit(
    tmp_path, http_status, exit_code
):
    # Arrange
    with _owned_delivery(tmp_path, http_status) as (agent, execute, calls):
        shell = Shell("owned-local", is_local=True)

        # Act
        result = deliver_prompt(shell, agent, "bounded challenge", exec_fn=execute)

    # Assert
    assert (calls, result.exit_code, marked(result, MARK_BOOT)) == (
        [(f"/agents/{agent}/send", {"prompt": "bounded challenge"})],
        exit_code,
        [str(exit_code)],
    )


def test_challenge_text_remains_literal_across_the_shell(tmp_path):
    # Arrange
    marker = tmp_path / "must-not-exist"
    prompt = f"line one\n'quoted' $(touch {marker}) `touch {marker}`"
    with _owned_delivery(tmp_path, 200) as (agent, execute, calls):
        shell = Shell("owned-local", is_local=True)

        # Act
        result = deliver_prompt(shell, agent, prompt, exec_fn=execute)

    # Assert
    assert (result.exit_code, calls, marker.exists()) == (
        0,
        [(f"/agents/{agent}/send", {"prompt": prompt})],
        False,
    )
