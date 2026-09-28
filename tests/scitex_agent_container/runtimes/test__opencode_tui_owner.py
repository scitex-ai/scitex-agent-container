"""Tests for ``runtimes/_opencode_tui_owner.py``.

A fake ``opencode`` binary (stub serve + stub attach, on PATH) keeps
the owner hermetic: no real TUI, no real model, no network beyond
loopback. Signal handling runs in the pytest main thread, like
production.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes import _opencode_tui_owner as owner
from scitex_agent_container.runtimes._gateway_opencode import SERVE_FILE

_FAKE_OPENCODE = """#!/usr/bin/env python3
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def _serve(port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload):
            import json as _json

            body = _json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/global/health":
                self._send(200, {"healthy": True})
            elif self.path.startswith("/session/"):
                self._send(200, {"id": self.path.rsplit("/", 1)[-1]})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/session":
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self._send(200, {"id": "ses_stub1"})
            else:
                self.send_response(404)
                self.end_headers()

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def _attach():
    import json as _json

    state_dir = os.environ["OC_TEST_STATE"]
    serve_path = os.path.join(state_dir, "opencode-serve.json")
    with open(serve_path, encoding="utf-8") as handle:
        url = _json.load(handle)["url"]
    with open(os.path.join(state_dir, "attach-marker"), "w", encoding="utf-8") as out:
        out.write(url)
    marker = os.path.join(state_dir, "attach-argv.json")
    with open(marker, "w", encoding="utf-8") as out:
        _json.dump(sys.argv[1:], out)
    raise SystemExit(int(os.environ.get("OC_TEST_ATTACH_RC", "0")))


if sys.argv[1] == "serve":
    _serve(int(sys.argv[sys.argv.index("--port") + 1]))
elif sys.argv[1] == "attach":
    _attach()
else:
    raise SystemExit(2)
"""


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def _fake_opencode(env_save_restore, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(_FAKE_OPENCODE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    env_save_restore.set("OC_TEST_STATE", str(tmp_path))
    return tmp_path


def _run_owner(state_dir, port):
    url = f"http://127.0.0.1:{port}"
    return owner.main(
        [
            "--state-dir",
            str(state_dir),
            "--agent-name",
            "worker",
            "--port",
            str(port),
            "--",
            "opencode",
            "attach",
            url,
        ]
    )


def test_owner_publishes_the_serve_url_for_the_tui(_fake_opencode):
    # Arrange
    state_dir = _fake_opencode
    port = _free_port()
    # Act
    _run_owner(state_dir, port)
    # Assert
    assert (state_dir / "attach-marker").read_text() == f"http://127.0.0.1:{port}"


def test_owner_removes_serve_state_on_exit(_fake_opencode):
    # Arrange
    state_dir = _fake_opencode
    port = _free_port()
    # Act
    _run_owner(state_dir, port)
    # Assert
    assert not (state_dir / SERVE_FILE).exists()


def test_owner_returns_the_tui_exit_code(_fake_opencode, env_save_restore):
    # Arrange
    state_dir = _fake_opencode
    env_save_restore.set("OC_TEST_ATTACH_RC", "3")
    port = _free_port()
    # Act
    rc = _run_owner(state_dir, port)
    # Assert
    assert rc == 3


def test_owner_pins_the_stable_session_on_attach(_fake_opencode):
    # Arrange
    state_dir = _fake_opencode
    port = _free_port()
    # Act
    _run_owner(state_dir, port)
    # Assert
    assert json.loads((state_dir / "attach-argv.json").read_text()) == [
        "attach",
        f"http://127.0.0.1:{port}",
        "--session",
        "ses_stub1",
    ]


def test_owner_persists_the_session_map_for_the_driver(_fake_opencode):
    # Arrange
    state_dir = _fake_opencode
    port = _free_port()
    # Act
    _run_owner(state_dir, port)
    # Assert
    assert json.loads((state_dir / "opencode-sessions.json").read_text()) == {
        "sac:worker": "ses_stub1"
    }


def test_owner_reuses_the_mapped_session_when_the_server_holds_it(_fake_opencode):
    # Arrange
    state_dir = _fake_opencode
    (state_dir / "opencode-sessions.json").write_text(
        json.dumps({"sac:worker": "ses_kept"})
    )
    port = _free_port()
    # Act
    _run_owner(state_dir, port)
    # Assert
    assert json.loads((state_dir / "attach-argv.json").read_text())[-1] == "ses_kept"


def test_pin_respects_a_caller_supplied_session():
    # Arrange
    command = ["opencode", "attach", "http://x", "--session", "ses_mine"]
    # Act
    pinned = owner._pin_attach_session(command, "ses_other")
    # Assert
    assert pinned == command


def test_owner_refuses_a_missing_command(tmp_path):
    # Arrange
    argv = ["--state-dir", str(tmp_path), "--port", "4199"]

    def action():
        return owner.main(argv)

    # Act
    run = action
    # Assert
    with pytest.raises(SystemExit):
        run()


def test_owner_refuses_an_invalid_port(tmp_path):
    # Arrange
    argv = ["--state-dir", str(tmp_path), "--port", "0", "--", "true"]

    def action():
        return owner.main(argv)

    # Act
    run = action
    # Assert
    with pytest.raises(SystemExit):
        run()


def test_health_wait_times_out_when_nothing_listens():
    # Arrange
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    process = SimpleNamespace(poll=lambda: None)
    # Act
    def action():
        return owner._wait_for_health(url, process, 0.3)

    # Assert
    with pytest.raises(RuntimeError, match="did not become ready"):
        action()


def test_atomic_json_is_owner_only(tmp_path):
    # Arrange
    path = tmp_path / SERVE_FILE
    # Act
    owner._atomic_json(path, {"url": "http://127.0.0.1:1"})
    # Assert
    assert path.stat().st_mode & 0o777 == 0o600
