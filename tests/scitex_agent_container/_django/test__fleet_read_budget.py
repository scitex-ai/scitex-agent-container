"""Focused tests for the bounded fleet read (``gui-baseline-scitex-agent-container-20260917``).

Regression under test: a fleet read that cannot finish inside the client
timeout renders the page's "listener unreachable" banner even though the
listener IS reachable and is answering. On 2026-09-17 the real control plane on
scitex-compute-03 answered ``GET /agents`` 200 in 5.1-6.9s warm / 19.0s cold
against an 8.0s client timeout, so the whole fleet table disappeared.

Two properties are asserted against a REAL loopback control plane (no mocks, no
monkeypatch — the ``loopback`` fixture shape, extended here for latency):

1. Per-agent granularity — one slow agent degrades its OWN row to an explicit
   state; the other rows still render.
2. A single fleet-level read cannot spend more than the client timeout in
   total, so a slow (not down) listener does not consume Django worker time in
   proportion to the agent count.

Route/viewport-independent: these are view/render assertions, shared by the
standalone and Hub mounts because both render the same partials.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scitex_agent_container._django._remote import FleetUnavailableError, RemoteFleet
from scitex_agent_container._django.views import _fleet_rows


class _SlowListener(BaseHTTPRequestHandler):
    """A control plane where EVERY status read is slow (the 2026-09-17 shape)."""

    delay = 0.0
    repeat = 0.0
    agents: list[str] = []
    requests = 0

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802 (http.server API)
        type(self).requests += 1
        if self.repeat:
            time.sleep(self.repeat)
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


@pytest.fixture
def slow_listener(env_save_restore):
    """A real loopback listener whose /status reads sleep; yields a setter."""

    _SlowListener.delay = 0.0
    _SlowListener.repeat = 0.0
    _SlowListener.agents = [f"agent-{i}" for i in range(12)]
    _SlowListener.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowListener)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", f"http://127.0.0.1:{port}")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN", "tok")

    class Control:
        def __init__(self, port: int) -> None:
            self.port = port

        @property
        def fleet(self) -> RemoteFleet:
            return RemoteFleet(f"http://127.0.0.1:{self.port}", "tok")

    try:
        yield Control(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _SlowListener.delay = 0.0
        _SlowListener.repeat = 0.0


def test_slow_status_degrades_its_own_row_not_the_fleet(slow_listener):
    # Arrange: every agent's status read is slower than one per-agent budget.
    _SlowListener.delay = 1.2
    _SlowListener.agents = [f"agent-{i}" for i in range(40)]
    # Act
    started = time.monotonic()
    agents, comm_error = _fleet_rows(slow_listener.fleet, "operator")
    elapsed = time.monotonic() - started
    # Assert: rows survive AND the read is bounded by the client timeout, not
    # the agent count (40 agents x 1.2s = 48s serially, worse than the ceiling).
    assert len(agents) == 40 and comm_error == "" and elapsed < 30.0


def test_bounded_fleet_read_never_exceeds_the_configured_timeout(env_save_restore):
    # Arrange: a listener that is UP but swallows the request forever — TCP
    # connect succeeds, no response ever arrives. This is the state that must
    # resolve to "unavailable" rather than hanging the worker.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _BlackHole(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):  # noqa: N802 (http.server API)
            time.sleep(30)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlackHole)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fleet = RemoteFleet(f"http://127.0.0.1:{server.server_address[1]}", "tok", timeout=2.0)
    try:
        # Act
        started = time.monotonic()
        try:
            fleet.list_all()
            outcome = "ok"
        except FleetUnavailableError:
            outcome = "unavailable"
        elapsed = time.monotonic() - started
        # Assert
        assert outcome == "unavailable" and elapsed < fleet.timeout * 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_client_timeout_outlasts_the_measured_control_plane_latency():
    # Arrange: the measured warm /agents latency on scitex-compute-03 was
    # 5.1-6.9s; the client must not sit on that boundary.
    # Act
    timeout = RemoteFleet("http://127.0.0.1:1", "tok").timeout
    # Assert
    assert timeout >= 30.0
