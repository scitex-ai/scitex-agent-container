"""Tests for ``_django/_remote`` (the authenticated SAC control-plane client).

Exercises the real client against a REAL loopback HTTP listener serving the
contract (the ``loopback`` fixture) — no mocks, no monkeypatch. Each test has
AAA markers and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scitex_agent_container._django._remote import (
    FleetUnavailableError,
    RemoteFleet,
    RemoteOperationError,
    resolve_token,
)
from scitex_agent_container._django.views import _fleet_rows

from .conftest import TOKEN


def test_resolve_token_prefers_env(env_save_restore):
    # Arrange
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN", "env-token")
    # Act
    token = resolve_token("http://127.0.0.1:1")
    # Assert
    assert token == "env-token"


def test_resolve_token_falls_back_to_token_file(env_save_restore, tmp_path):
    # Arrange
    path = tmp_path / "tok"
    path.write_text("file-token\n", encoding="utf-8")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN_FILE", str(path))
    # Act
    token = resolve_token("http://127.0.0.1:1")
    # Assert
    assert token == "file-token"


def test_list_all_returns_scoped_rows(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    rows = fleet.list_all()
    # Assert
    assert {r["name"] for r in rows} == {"alpha", "beta", "gamma", "delta"}


def test_read_status_returns_body(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    status = fleet.read_status("alpha")
    # Assert
    assert status["liveness"]["verdict"] == "ALIVE"


def test_read_status_typed_error_carries_kind(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    try:
        fleet.read_status("delta")
        raised = False
    except RemoteOperationError as exc:
        raised = exc.kind == "spec_resolution_failed" and exc.status_code == 400
    # Assert
    assert raised


def test_read_statuses_collects_concurrently(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    results = fleet.read_statuses(["alpha", "delta"])
    # Assert
    assert isinstance(results["alpha"], dict) and isinstance(results["delta"], RemoteOperationError)


def test_read_tail_returns_sse_body(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    tail = fleet.read_tail("alpha")
    # Assert
    assert "start the job" in tail and "sk-abc123DEF456GHI789jkl012" in tail


def test_read_tail_404_is_empty(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    tail = fleet.read_tail("beta")
    # Assert
    assert tail == ""


def test_unreachable_listener_raises_fleet_unavailable():
    # Arrange
    fleet = RemoteFleet("http://127.0.0.1:1", TOKEN)
    # Act
    try:
        fleet.list_all()
        raised = False
    except FleetUnavailableError:
        raised = True
    # Assert
    assert raised


def test_wrong_token_is_rejected(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", "not-the-token")
    # Act
    try:
        fleet.list_all()
        raised = False
    except RemoteOperationError as exc:
        raised = exc.status_code == 401
    # Assert
    assert raised

# ── the bounded fleet read (timeout budget; baseline card) ─────────────────
# A fleet read that cannot finish inside the client timeout used to render the
# page's "listener unreachable" banner against a HEALTHY listener: the real
# control plane measured 5.1-6.9s warm / 19.0s cold against an 8.0s ceiling, so
# the whole fleet table disappeared. These live here because _remote.py is what
# they exercise (PS-204: a test file must mirror a src module).


class _SlowListener(BaseHTTPRequestHandler):
    """A control plane where EVERY status read is slow (the 2026-09-17 shape)."""

    delay = 0.0
    agents: list[str] = []

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/agents":
            body = json.dumps({"agents": [self._row(n) for n in self.agents]}).encode()
            self._send(200, body)
            return
        if path.endswith("/status"):
            time.sleep(self.delay)
            name = path.rsplit("/status", 1)[0].rsplit("/", 1)[-1]
            self._send(200, json.dumps({"name": name, "liveness": {"verdict": "ALIVE"}}).encode())
            return
        self._send(404, b'{"error": "not found"}')

    @staticmethod
    def _row(name: str) -> dict:
        import socket

        return {
            "name": name,
            "role": "worker",
            "project": "proj",
            "pid": 1,
            "a2a_port": 19000,
            "turn_url": f"http://{socket.gethostname()}:19000/v1/turn",
            "started_at": "2026-09-01T00:00:00Z",
        }

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BlackHole(BaseHTTPRequestHandler):
    """A listener that accepts the connection and never answers."""

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802 (http.server API)
        time.sleep(30)


@pytest.fixture
def slow_listener():
    """A real loopback listener whose /status reads sleep.

    Yields (not returns) because it owns a socket — a resource-acquiring
    fixture must let pytest tear it down (STX-TQ005).
    """
    _SlowListener.delay = 0.0
    _SlowListener.agents = [f"agent-{i}" for i in range(12)]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowListener)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Control:
        def __init__(self, port: int) -> None:
            self.port = port

        @property
        def fleet(self) -> RemoteFleet:
            return RemoteFleet(f"http://127.0.0.1:{self.port}", "tok")

    try:
        yield Control(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _SlowListener.delay = 0.0


@pytest.fixture
def black_hole():
    """A listener that accepts the TCP connection then never responds."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlackHole)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_slow_status_reads_still_return_every_row(slow_listener):
    # Arrange: every agent's status read is slower than one per-agent budget.
    _SlowListener.delay = 1.2
    _SlowListener.agents = [f"agent-{i}" for i in range(40)]
    # Act
    agents, comm_error = _fleet_rows(slow_listener.fleet, "operator")
    # Assert: every row survives the slow read.
    assert len(agents) == 40 and comm_error == ""


def test_slow_fleet_read_stays_within_the_ceiling(slow_listener):
    # Arrange: 40 agents x 1.2s would be 48s if the fan-out were serial.
    _SlowListener.delay = 1.2
    _SlowListener.agents = [f"agent-{i}" for i in range(40)]
    # Act
    started = time.monotonic()
    _fleet_rows(slow_listener.fleet, "operator")
    elapsed = time.monotonic() - started
    # Assert: bounded by the client ceiling, not by the agent count.
    assert elapsed < 30.0


def test_silent_listener_resolves_to_unavailable(black_hole):
    # Arrange: TCP connect succeeds, no response ever arrives.
    fleet = RemoteFleet(black_hole, "tok", timeout=2.0)
    # Act
    try:
        fleet.list_all()
        outcome = "ok"
    except FleetUnavailableError:
        outcome = "unavailable"
    # Assert: reported as unavailable rather than hanging.
    assert outcome == "unavailable"


def test_client_timeout_outlasts_the_measured_control_plane_latency():
    # Arrange: measured warm /agents latency on scitex-compute-03 was 5.1-6.9s.
    fleet = RemoteFleet("http://127.0.0.1:1", "tok")
    # Act
    timeout = fleet.timeout
    # Assert: the client must not sit on that boundary.
    assert timeout >= 30.0
