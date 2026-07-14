"""Tests for ``_listen/_holder_health.py`` — the three-state health verdict.

Two states could not express "I asked and got nothing", so the standby
loop collapsed that into the same ``False`` as "it said no" and a later
lucky reply erased it. These pin the three-state contract, including the
load-bearing asymmetry: a 401 (auth-gated but ALIVE) must never be read
as dead, because destroying a healthy control plane on a wrong verdict is
worse than failing to act on a right one.

No-mocks (PA-306 / STX-NM001-003): the probe runs against a REAL loopback
``http.server`` on an ephemeral port and a REAL closed port.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import http.server
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

from scitex_agent_container._listen._holder_health import (
    HolderHealth,
    classify_status,
    probe_holder_health,
)


@contextmanager
def _server(status: int) -> Iterator[int]:
    """A REAL HTTP server answering every GET with ``status``."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib handler contract)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_a) -> None:
            return

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# classify_status — the three states
# ---------------------------------------------------------------------------


def test_transport_failure_is_unreachable() -> None:
    # Arrange — ``-1`` is _default_http_get's transport-failure sentinel:
    # we asked and got NOTHING. That is its own state, not a "no".
    # Act
    verdict = classify_status(-1)
    # Assert
    assert verdict is HolderHealth.UNREACHABLE


def test_server_error_is_not_serving() -> None:
    # Arrange — a 503 IS an answer, but it is not health. The old probe
    # (``status > 0``) called it a healthy holder.
    # Act
    verdict = classify_status(503)
    # Assert
    assert verdict is HolderHealth.NOT_SERVING


def test_ok_status_is_serving() -> None:
    # Arrange — the plain healthy case.
    # Act
    verdict = classify_status(200)
    # Assert
    assert verdict is HolderHealth.SERVING


@pytest.mark.parametrize("status", [401, 403])
def test_auth_gated_status_is_serving(status: int) -> None:
    # Arrange — load-bearing (card sac-listen-restart-healthcheck-bearer,
    # PR #463): a 401/403 PROVES the daemon is bound and auth-gating.
    # Reading it as dead once SIGKILLed a healthy daemon.
    # Act
    verdict = classify_status(status)
    # Assert
    assert verdict is HolderHealth.SERVING


def test_not_found_status_is_serving() -> None:
    # Arrange — a 404 still proves the process is bound and speaking HTTP.
    # We refuse to destroy on a route-shape difference.
    # Act
    verdict = classify_status(404)
    # Assert
    assert verdict is HolderHealth.SERVING


# ---------------------------------------------------------------------------
# probe_holder_health — REAL sockets
# ---------------------------------------------------------------------------


def test_live_server_probes_as_serving() -> None:
    # Arrange
    with _server(200) as port:
        # Act
        probe = probe_holder_health("127.0.0.1", port, timeout=2.0)
    # Assert
    assert probe.health is HolderHealth.SERVING


def test_erroring_server_probes_as_not_serving() -> None:
    # Arrange — bound, speaking HTTP, but its health route is broken.
    with _server(503) as port:
        # Act
        probe = probe_holder_health("127.0.0.1", port, timeout=2.0)
    # Assert
    assert probe.health is HolderHealth.NOT_SERVING


def test_closed_port_probes_as_unreachable() -> None:
    # Arrange — nothing is listening on port 1.
    # Act
    probe = probe_holder_health("127.0.0.1", 1, timeout=0.5)
    # Assert
    assert probe.health is HolderHealth.UNREACHABLE


def test_unreachable_probe_is_not_serving() -> None:
    # Arrange — the predicate the standby loop branches on. Absence of
    # evidence must never read as evidence of health.
    # Act
    probe = probe_holder_health("127.0.0.1", 1, timeout=0.5)
    # Assert
    assert probe.serving is False


# ---------------------------------------------------------------------------
# describe() — the log line must state the EVIDENCE, not a conclusion
# ---------------------------------------------------------------------------


def test_unreachable_describe_says_no_answer() -> None:
    # Arrange — the operator must be able to read the log and see that
    # nothing answered, rather than be told a verdict.
    probe = probe_holder_health("127.0.0.1", 1, timeout=0.5)
    # Act
    text = probe.describe()
    # Assert
    assert "did not answer" in text


def test_serving_describe_names_the_status() -> None:
    # Arrange
    with _server(200) as port:
        probe = probe_holder_health("127.0.0.1", port, timeout=2.0)
    # Act
    text = probe.describe()
    # Assert
    assert "HTTP 200" in text
