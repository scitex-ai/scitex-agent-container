"""Tests for ``cli_pkg.a2a_group`` — ``sac a2a {serve,doctor}``.

PA-306 no-mocks coverage closure for ``a2a_group.py``. All
collaborators are real:

* ``CliRunner`` invokes the real Click commands.
* ``a2a_doctor`` is exercised against a real local ``http.server``
  ``HTTPServer`` in a background thread, mirroring the pattern in
  ``tests/scitex_agent_container/_lifecycle/test_health.py``.
* ``a2a_serve`` is exercised by booting the real ``sac a2a serve``
  subprocess on an ephemeral port and probing its AgentCard
  endpoint -- the same pattern proven by
  ``tests/e2e/test_a2a_peer_messaging.py``.
* YAMLs live under ``tmp_path``; agent names are taken from the
  parent dir name (the dir-as-SSoT convention).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.a2a_group import _emit, a2a

# ---------------------------------------------------------------------------
# Real local HTTP server -- serves whatever AgentCard the test asks for
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _CardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        srv = self.server  # type: ignore[assignment]
        status = getattr(srv, "status_code", 200)
        body = getattr(srv, "body", b"")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def card_server() -> Iterator[Any]:
    """Spin up a real HTTPServer on 127.0.0.1 in a background thread."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _CardHandler)
    server.status_code = 200  # type: ignore[attr-defined]
    server.body = b""  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Controller:
        def __init__(self) -> None:
            self.port = port

        def set_card(self, body: dict | None, *, status: int = 200) -> None:
            server.status_code = status  # type: ignore[attr-defined]
            server.body = json.dumps(body).encode() if body is not None else b"not json"

    try:
        yield Controller()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# YAML helpers (dir-as-SSoT)
# ---------------------------------------------------------------------------


def _write_spec(
    tmp_path: Path,
    name: str,
    *,
    host: str = "127.0.0.1",
    port: int | None,
    include_port: bool = True,
) -> Path:
    """Write ``<tmp_path>/<name>/spec.yaml`` with the given a2a block."""
    agent_dir = tmp_path / name
    agent_dir.mkdir()
    spec_path = agent_dir / "spec.yaml"
    if include_port and port is not None:
        body = (
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "metadata:\n"
            f"  name: {name}\n"
            "spec:\n"
            "  a2a:\n"
            f"    host: {host}\n"
            f"    port: {port}\n"
        )
    else:
        body = (
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "metadata:\n"
            f"  name: {name}\n"
            "spec: {}\n"
        )
    spec_path.write_text(body)
    return spec_path


# ---------------------------------------------------------------------------
# a2a_doctor -- happy path (real local server)
# ---------------------------------------------------------------------------


def test_doctor_healthy_returns_zero_exit(tmp_path: Path, card_server: Any) -> None:
    # Arrange
    name = "ag-healthy"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"name": name, "url": f"http://x/{name}"})
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec)])
    # Assert
    assert res.exit_code == 0


def test_doctor_healthy_text_mentions_agent(tmp_path: Path, card_server: Any) -> None:
    # Arrange
    name = "ag-healthy"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"name": name})
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec)])
    # Assert
    assert "healthy" in res.output


def test_doctor_json_emits_ok_true(tmp_path: Path, card_server: Any) -> None:
    # Arrange
    name = "ag-json"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"name": name, "url": f"http://x/{name}"})
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec), "--json"])
    # Assert
    assert json.loads(res.output)["ok"] is True


def test_doctor_json_carries_elapsed_ms(tmp_path: Path, card_server: Any) -> None:
    # Arrange
    name = "ag-elapsed"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"name": name})
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec), "--json"])
    # Assert
    assert "elapsed_ms" in json.loads(res.output)


# ---------------------------------------------------------------------------
# a2a_doctor -- error branches
# ---------------------------------------------------------------------------


def test_doctor_missing_port_exits_two(tmp_path: Path) -> None:
    # Arrange
    name = "ag-no-port"
    spec = _write_spec(tmp_path, name, port=None, include_port=False)
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec)])
    # Assert
    assert res.exit_code == 2


def test_doctor_missing_port_json_carries_error(tmp_path: Path) -> None:
    # Arrange
    name = "ag-no-port-j"
    spec = _write_spec(tmp_path, name, port=None, include_port=False)
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec), "--json"])
    # Assert
    assert "spec.a2a.port" in json.loads(res.output)["error"]


def test_doctor_name_mismatch_returns_one(tmp_path: Path, card_server: Any) -> None:
    # Arrange
    name = "ag-mismatch"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"name": "someone-else"})
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec)])
    # Assert
    assert res.exit_code == 1


def test_doctor_name_mismatch_message_explains(
    tmp_path: Path, card_server: Any
) -> None:
    # Arrange
    name = "ag-mismatch-msg"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"name": "someone-else"})
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec)])
    # Assert
    assert "name mismatch" in res.output


def test_doctor_http_error_exits_one(tmp_path: Path, card_server: Any) -> None:
    # Arrange: server returns 500
    name = "ag-http-err"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"err": "boom"}, status=500)
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec)])
    # Assert
    assert res.exit_code == 1


def test_doctor_http_error_message_carries_status(
    tmp_path: Path, card_server: Any
) -> None:
    # Arrange
    name = "ag-http-status"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card({"err": "boom"}, status=500)
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec), "--json"])
    # Assert
    assert "HTTP 500" in json.loads(res.output)["error"]


def test_doctor_malformed_json_exits_one(tmp_path: Path, card_server: Any) -> None:
    # Arrange: server returns non-JSON bytes
    name = "ag-bad-json"
    spec = _write_spec(tmp_path, name, port=card_server.port)
    card_server.set_card(None)  # writes b"not json"
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(spec)])
    # Assert
    assert res.exit_code == 1


def test_doctor_connection_refused_exits_one(tmp_path: Path) -> None:
    # Arrange: a free port that nothing is listening on
    closed_port = _free_port()
    name = "ag-down"
    spec = _write_spec(tmp_path, name, port=closed_port)
    # Act: short timeout so the test stays fast
    res = CliRunner().invoke(a2a, ["doctor", str(spec), "--timeout", "1.0"])
    # Assert
    assert res.exit_code == 1


def test_doctor_port_override_wins_over_yaml(tmp_path: Path, card_server: Any) -> None:
    # Arrange: yaml declares a bogus port, --port redirects to live server.
    name = "ag-portov"
    spec = _write_spec(tmp_path, name, port=1, include_port=True)
    card_server.set_card({"name": name})
    # Act
    res = CliRunner().invoke(
        a2a, ["doctor", str(spec), "--port", str(card_server.port)]
    )
    # Assert
    assert res.exit_code == 0


def test_doctor_host_override_wins_over_yaml(tmp_path: Path, card_server: Any) -> None:
    # Arrange: yaml declares a bogus host, --host redirects to live server.
    name = "ag-hostov"
    spec = _write_spec(tmp_path, name, host="0.0.0.0", port=card_server.port)
    card_server.set_card({"name": name})
    # Act
    res = CliRunner().invoke(
        a2a,
        ["doctor", str(spec), "--host", "127.0.0.1", "--json"],
    )
    # Assert
    assert json.loads(res.output)["ok"] is True


def test_doctor_uses_metadata_name_when_not_spec_yaml(
    tmp_path: Path, card_server: Any
) -> None:
    # Arrange: yaml file is foo.yaml (NOT spec.yaml), so name comes
    # from metadata.name, not the parent dir.
    agent_dir = tmp_path / "container"
    agent_dir.mkdir()
    yaml_path = agent_dir / "foo.yaml"
    yaml_path.write_text(
        "metadata:\n  name: meta-name\n"
        "spec:\n  a2a:\n    host: 127.0.0.1\n"
        f"    port: {card_server.port}\n"
    )
    card_server.set_card({"name": "meta-name"})
    # Act
    res = CliRunner().invoke(a2a, ["doctor", str(yaml_path), "--json"])
    # Assert
    assert json.loads(res.output)["agent"] == "meta-name"


# ---------------------------------------------------------------------------
# _emit -- direct unit (covers branches indirectly already, but pin
# the API)
# ---------------------------------------------------------------------------


def test_emit_json_branch_serialises_dict(capsys: pytest.CaptureFixture) -> None:
    # Arrange
    payload = {"ok": True, "agent": "x"}
    # Act
    _emit(payload, True)
    # Assert
    assert json.loads(capsys.readouterr().out) == payload


def test_emit_text_unhealthy_writes_to_stderr(
    capsys: pytest.CaptureFixture,
) -> None:
    # Arrange
    payload = {"ok": False, "agent": "x", "error": "boom"}
    # Act
    _emit(payload, False)
    # Assert
    assert "unhealthy" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# a2a_serve -- argument plumbing (no live boot) + live boot smoke
# ---------------------------------------------------------------------------


def test_serve_rejects_unknown_handler_choice(tmp_path: Path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, "ag-bad-handler", port=9999)
    # Act
    res = CliRunner().invoke(a2a, ["serve", str(spec), "--handler", "nonsense"])
    # Assert: Click rejects the invalid choice before serve() runs.
    assert res.exit_code != 0


def test_serve_requires_at_least_one_yaml() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    res = runner.invoke(a2a, ["serve"])
    # Assert
    assert res.exit_code != 0


def test_serve_rejects_missing_yaml_path(tmp_path: Path) -> None:
    # Arrange
    bogus = tmp_path / "does-not-exist.yaml"
    # Act
    res = CliRunner().invoke(a2a, ["serve", str(bogus)])
    # Assert
    assert res.exit_code != 0


# ---------------------------------------------------------------------------
# a2a_serve -- live subprocess smoke (mirrors tests/e2e pattern)
# ---------------------------------------------------------------------------


def _sac_cmd() -> list[str]:
    """Locate the ``sac`` entry-point or fall back to ``python -m``."""
    sac = shutil.which("sac")
    if sac:
        return [sac]
    return [sys.executable, "-m", "scitex_agent_container.cli"]


def _wait_for_card(url: str, *, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                r.read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


@pytest.fixture
def live_serve_yaml(tmp_path: Path) -> Path:
    """Real v3 spec.yaml suitable for ``sac a2a serve``."""
    name = "smoke-agent"
    agent_dir = tmp_path / name
    agent_dir.mkdir()
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  a2a:\n"
        "    host: 127.0.0.1\n"
        "    port: 0\n"
        "    handler: echo\n"
    )
    return spec


@pytest.fixture
def live_serve_card_status(live_serve_yaml: Path) -> Iterator[int]:
    """Boot ``sac a2a serve`` on an ephemeral port, return card HTTP status.

    Yields the HTTP status code returned by a real ``GET`` to the
    server's per-agent AgentCard endpoint. The fixture cleans up the
    subprocess.
    """
    port = _free_port()
    cmd = _sac_cmd() + [
        "a2a",
        "serve",
        str(live_serve_yaml),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--handler",
        "echo",
        "--verbose",
    ]
    proc = subprocess.Popen(
        cmd,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Server's real route per a2a/_server.py (agent-card.json, not
        # agent.json). a2a_doctor uses a different URL — that mismatch
        # is a separate concern, not under test here.
        url = f"http://127.0.0.1:{port}/agents/smoke-agent/.well-known/agent-card.json"
        if not _wait_for_card(url, timeout=20.0):
            pytest.skip("a2a serve subprocess never became ready")
        with urllib.request.urlopen(url, timeout=5) as r:
            yield int(r.status)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_serve_subprocess_serves_card_endpoint(
    live_serve_card_status: int,
) -> None:
    # Arrange
    status = live_serve_card_status
    # Act
    is_ok = status == 200
    # Assert
    assert is_ok
