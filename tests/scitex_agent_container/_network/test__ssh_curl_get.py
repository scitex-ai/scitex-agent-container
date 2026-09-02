"""Unit + end-to-end coverage for ``_network/_ssh_curl._get_via_ssh_curl``.

The GET sibling of ``_post_via_ssh_curl`` exists so the reachability probe
can exercise the cross-host forwarder's TRANSPORT rather than a look-alike
of it. Two properties matter and both are pinned here:

* the ssh argv is the POST's argv (host, BatchMode, ControlMaster options),
  and the bearer never reaches any argv element — it rides stdin as a curl
  config, the house shape;
* the remote snippet really works: the end-to-end tests install an ``ssh``
  that executes the remote command LOCALLY (no mock; a real ``/bin/sh``
  runs the real ``curl`` against a real ``http.server``), and assert on the
  status line + body the production parser reads back.

No ``unittest.mock``; ``subprocess_shim`` / ``ssh_exec_shim`` install fake
binaries on PATH and production code calls the real ``subprocess.run``.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator

import pytest

from scitex_agent_container._network._ssh_curl import (
    STATUS_MARKER,
    _get_via_ssh_curl,
    split_status_line,
)

#: A FAKE, token-shaped value. Never a real credential — asserted ABSENT.
_FAKE_BEARER = "FAKE-BEARER-VALUE-4f2a9c1e-NOT-A-REAL-TOKEN"

#: An ``ssh`` that runs the LAST argv element (the remote command) locally,
#: with stdin passed through — what sshd does with the post-host argv. The
#: shared ``ssh_exec_shim`` script insists on a ``--`` separator, which the
#: forwarder's argv (and therefore this GET's) deliberately does not carry.
_LOCAL_EXEC_SSH = """#!/bin/sh
for __a in "$@"; do __cmd="$__a"; done
exec sh -c "$__cmd"
"""


def _pin_ssh_env(tmp_path, env_save_restore) -> None:
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")


# ---------------------------------------------------------------------------
# argv shape — the POST's argv, with the bearer kept out of it
# ---------------------------------------------------------------------------


def test_get_via_ssh_curl_includes_target_host_in_argv(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    # Act
    _get_via_ssh_curl(host="example.invalid", port=7878, path="/v1/health")
    argv = subprocess_shim.argv_for("ssh")
    # Assert
    assert "example.invalid" in argv


def test_get_via_ssh_curl_keeps_bearer_out_of_every_argv_element(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange — a token in the argv is world-readable in /proc on BOTH hosts.
    _pin_ssh_env(tmp_path, env_save_restore)
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    # Act
    _get_via_ssh_curl(
        host="example.invalid", port=7878, path="/v1/health", bearer=_FAKE_BEARER
    )
    leaked = [part for part in subprocess_shim.argv_for("ssh") if _FAKE_BEARER in part]
    # Assert — count, never the matching text.
    assert len(leaked) == 0


def test_get_via_ssh_curl_takes_the_bearer_from_a_curl_config_on_stdin(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    # Act
    _get_via_ssh_curl(
        host="example.invalid", port=7878, path="/v1/health", bearer=_FAKE_BEARER
    )
    remote = subprocess_shim.argv_for("ssh")[-1]
    # Assert
    assert "--config -" in remote


def test_get_via_ssh_curl_omits_the_config_read_when_bearer_is_none(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    # Act
    _get_via_ssh_curl(host="example.invalid", port=7878, path="/v1/health")
    remote = subprocess_shim.argv_for("ssh")[-1]
    # Assert
    assert "--config" not in remote


def test_get_via_ssh_curl_uses_batch_mode_like_the_forwarder(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange — the whole point of the sibling: the SAME ssh options.
    _pin_ssh_env(tmp_path, env_save_restore)
    subprocess_shim.install("ssh", exit=0, stdout="{}")
    # Act
    _get_via_ssh_curl(host="example.invalid", port=7878, path="/v1/health")
    argv = subprocess_shim.argv_for("ssh")
    # Assert
    assert "BatchMode=yes" in argv


def test_get_via_ssh_curl_refuses_a_bearer_that_would_break_the_header():
    # Arrange — a quote would corrupt the curl config line, not authenticate.
    bad = 'abc"def'

    # Act
    def _call():
        return _get_via_ssh_curl(
            host="example.invalid", port=7878, path="/v1/health", bearer=bad
        )

    # Assert
    with pytest.raises(ValueError):
        _call()


def test_get_via_ssh_curl_reports_a_non_zero_ssh_exit(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused")
    # Act
    rc, _out, _err = _get_via_ssh_curl(
        host="example.invalid", port=7878, path="/v1/health"
    )
    # Assert
    assert rc == 255


# ---------------------------------------------------------------------------
# split_status_line — the parser the probe reads the answer through
# ---------------------------------------------------------------------------


def test_split_status_line_reads_the_status_after_the_body():
    # Arrange
    stdout = f'{{"ok": true}}\n{STATUS_MARKER}200\n'.encode()
    # Act
    status, _body = split_status_line(stdout)
    # Assert
    assert status == 200


def test_split_status_line_keeps_the_body_without_the_marker():
    # Arrange
    stdout = f'{{"ok": true}}\n{STATUS_MARKER}200\n'.encode()
    # Act
    _status, body = split_status_line(stdout)
    # Assert
    assert body == '{"ok": true}'


def test_split_status_line_is_none_when_no_marker_was_printed():
    # Arrange — a remote shell that printed a banner and never ran curl.
    stdout = b"Welcome to somewhere\n"
    # Act
    status, _body = split_status_line(stdout)
    # Assert — UNKNOWN, never a fabricated status.
    assert status is None


# ---------------------------------------------------------------------------
# End to end: real sh, real curl, real HTTP server — the ssh leg stands in
# ---------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        srv = self.server
        srv.seen_auth.append(self.headers.get("Authorization"))  # type: ignore[attr-defined]
        if self.path == "/v1/health":
            body = json.dumps({"ok": True, "service": "sac-listen", "v": 1}).encode()
            self.send_response(200)
        else:
            body = b'{"error": "not found"}'
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def health_server() -> Iterator[Any]:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _HealthHandler)
    server.seen_auth = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.port = port  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


_needs_curl = pytest.mark.skipif(
    shutil.which("curl") is None, reason="curl is not installed on this host"
)


@_needs_curl
def test_get_via_ssh_curl_end_to_end_reads_a_200_from_a_real_listen_shaped_server(
    tmp_path, env_save_restore, ssh_exec_shim, health_server
):
    # Arrange — the shim executes the real remote snippet with real curl.
    _pin_ssh_env(tmp_path, env_save_restore)
    ssh_exec_shim.install_binary("ssh", _LOCAL_EXEC_SSH)
    # Act
    _rc, stdout, _stderr = _get_via_ssh_curl(
        host="peer.invalid",
        port=health_server.port,
        path="/v1/health",
        bearer=_FAKE_BEARER,
        timeout_s=5,
    )
    status, _body = split_status_line(stdout)
    # Assert
    assert status == 200


@_needs_curl
def test_get_via_ssh_curl_end_to_end_delivers_the_body_the_listen_sent(
    tmp_path, env_save_restore, ssh_exec_shim, health_server
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    ssh_exec_shim.install_binary("ssh", _LOCAL_EXEC_SSH)
    # Act
    _rc, stdout, _stderr = _get_via_ssh_curl(
        host="peer.invalid", port=health_server.port, path="/v1/health", timeout_s=5
    )
    _status, body = split_status_line(stdout)
    # Assert
    assert json.loads(body)["service"] == "sac-listen"


@_needs_curl
def test_get_via_ssh_curl_end_to_end_bearer_arrives_as_the_authorization_header(
    tmp_path, env_save_restore, ssh_exec_shim, health_server
):
    # Arrange — the value is in no argv, yet the server must still receive it.
    _pin_ssh_env(tmp_path, env_save_restore)
    ssh_exec_shim.install_binary("ssh", _LOCAL_EXEC_SSH)
    # Act
    _get_via_ssh_curl(
        host="peer.invalid",
        port=health_server.port,
        path="/v1/health",
        bearer=_FAKE_BEARER,
        timeout_s=5,
    )
    # Assert
    assert health_server.seen_auth == [f"Bearer {_FAKE_BEARER}"]


@_needs_curl
def test_get_via_ssh_curl_end_to_end_reports_a_404_as_its_status(
    tmp_path, env_save_restore, ssh_exec_shim, health_server
):
    # Arrange
    _pin_ssh_env(tmp_path, env_save_restore)
    ssh_exec_shim.install_binary("ssh", _LOCAL_EXEC_SSH)
    # Act
    _rc, stdout, _stderr = _get_via_ssh_curl(
        host="peer.invalid", port=health_server.port, path="/nowhere", timeout_s=5
    )
    status, _body = split_status_line(stdout)
    # Assert
    assert status == 404
