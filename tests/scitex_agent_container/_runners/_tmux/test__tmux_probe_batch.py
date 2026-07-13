"""Tests for the BATCHED tmux fleet probe (``list_sessions_activity``).

The per-agent probe pair cost THREE ``tmux`` subprocess spawns per agent
(``exists`` spawns one; ``session_activity`` goes through ``_display_field``,
which re-probes ``exists`` and then spawns ``tmux display``), so a heartbeat
tick was O(N) subprocesses — ~30s at fleet scale on a loaded host, which blew
the tick's budget and got it ABANDONED. This probe answers the same question
for the WHOLE fleet in ONE spawn, mirroring the one-query
``port_allocator.list_claims`` pattern.

RETURN CONTRACT under test (load-bearing — "unknown" ≠ "empty"):
  * ``dict`` — probe succeeded; a session absent from it is CONFIRMED absent.
  * ``{}``   — probe succeeded, fleet genuinely empty (no tmux server).
  * ``None`` — probe FAILED; liveness UNKNOWN. Callers must NOT read this as
    "every agent is dead" — that inference is the bug that broke fleet comms.

NO MOCKS: drives a REAL tmux server on a private socket with REAL sessions.

STX-TQ002 AAA-markers each on its own line + STX-TQ007 one-assert.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from scitex_agent_container._runners._tmux._tmux_probe import list_sessions_activity

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed on this host"
)

# Private socket so the tests can never touch the operator's real tmux
# server (which carries the live fleet's `tui-<agent>` sessions).
SOCKET = "sac-probe-tests"


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", SOCKET, *args], capture_output=True, text=True, timeout=10
    )


@pytest.fixture()
def tmux_server():
    """A real, isolated tmux server; killed on teardown."""
    _tmux("kill-server")
    yield _tmux
    _tmux("kill-server")


def _probe_on_socket(*, timeout_s: float = 5.0):
    """Drive the REAL production probe against the isolated test socket.

    Deliberately NOT a re-implementation: a hand-copied parse in the test
    drifts from the code it claims to cover (this test originally asserted
    tmux's no-server message was "no server running", when for an absent
    socket it is actually "error connecting to ... (No such file or
    directory)" — a copy hid that, the real function exposes it).
    """
    return list_sessions_activity(timeout_s=timeout_s, socket_name=SOCKET)


def test_no_tmux_server_is_a_confirmed_empty_fleet_not_unknown(tmux_server):
    # Arrange — no server running at all.
    tmux_server("kill-server")
    # Act
    snapshot = _probe_on_socket()
    # Assert — {} (confirmed empty), NOT None (unknown).
    assert snapshot == {}


def test_one_probe_returns_every_live_session(tmux_server):
    # Arrange — 30 real sessions. Serially this would be 90 tmux spawns.
    for idx in range(30):
        tmux_server("new-session", "-d", "-s", f"tui-agent-{idx}", "sleep", "60")
    # Act — ONE spawn.
    snapshot = _probe_on_socket()
    # Assert
    assert len(snapshot) == 30


def test_probe_carries_each_sessions_activity_epoch(tmux_server):
    # Arrange — a real session has a real pane-activity epoch.
    tmux_server("new-session", "-d", "-s", "tui-scitex-hpc", "sleep", "60")
    # Act
    snapshot = _probe_on_socket()
    # Assert — a plausible unix epoch, not a placeholder.
    assert snapshot["tui-scitex-hpc"] > 1_600_000_000


def test_absent_session_is_confirmed_absent_on_a_successful_probe(tmux_server):
    # Arrange — a live server with one session; another agent is NOT running.
    tmux_server("new-session", "-d", "-s", "tui-alive", "sleep", "60")
    # Act
    snapshot = _probe_on_socket()
    # Assert — the probe SUCCEEDED, so absence is real (a beat is correctly
    # withheld for this agent — but note the loop still writes no "dead").
    assert "tui-not-running" not in snapshot


def test_wedged_probe_is_unknown_not_an_empty_fleet(tmux_server):
    # Arrange — a live server with live sessions, but the probe cannot
    # complete in time. This is the loaded-host case: reading it as "no
    # sessions" would mark every live agent dead.
    tmux_server("new-session", "-d", "-s", "tui-alive", "sleep", "60")
    # Act — an unsatisfiable timeout forces a REAL subprocess timeout.
    snapshot = _probe_on_socket(timeout_s=0.000_001)
    # Assert — None (UNKNOWN), never {} (which would read as "all dead").
    assert snapshot is None


def test_production_probe_never_raises_on_the_default_socket():
    # Arrange — the real entry point the heartbeat loop calls. Whatever the
    # host's tmux state, it must degrade to dict/{}/None and never raise.
    # Act
    snapshot = list_sessions_activity()
    # Assert
    assert snapshot is None or isinstance(snapshot, dict)
