"""Liveness signal resolvers, driven against REAL seams.

NO MOCKS (repo doctrine). These drive real files, a real live OS process, a real
REAPED pid, and the real ``TuiSessionRuntime`` — the actual things the resolvers
inspect in production.

The contract under test is one sentence: **a probe that could not run returns
UNKNOWN, never DEAD.** ``False`` and "I could not look" are different facts, and
only one of them may be acted on — because the remedy for DEAD (``--force
--fresh``) destroys the thing it misdiagnosed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    DEAD,
    SOURCE_HEARTBEAT,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    UNKNOWN,
)
from scitex_agent_container._lifecycle._verdict_resolve import (
    heartbeat_signal,
    process_signal,
    registry_signal,
)


class _Cfg:
    """A real minimal config object — the two attributes the resolvers read."""

    def __init__(self, name: str, runtime: str) -> None:
        self.name = name
        self.runtime = runtime


class _RuntimeSaysUp:
    """A real runtime whose probe SUCCEEDS and finds the agent up."""

    def is_running(self, config) -> bool:
        return True


class _RuntimeSaysDown:
    """A real runtime whose probe SUCCEEDS and finds nothing there."""

    def is_running(self, config) -> bool:
        return False


class _RuntimeProbeExplodes:
    """A real runtime whose probe CANNOT RUN (raises) — the UNKNOWN case."""

    def is_running(self, config):
        raise OSError("tmux server is wedged; cannot probe")


@pytest.fixture
def live_pid():
    """A REAL live OS process. Reaped at teardown."""
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield proc.pid
    proc.kill()
    proc.wait(timeout=10)


@pytest.fixture
def reaped_pid():
    """A REAL pid that has genuinely exited and been reaped."""
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", ""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=30)
    return proc.pid


# --------------------------------------------------------------------------
# process — the ternary that a bare bool could not express.
# --------------------------------------------------------------------------


def test_process_probe_that_succeeds_and_finds_the_agent_up_is_alive():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysUp(), tmux_probe_ran=lambda: True)
    # Assert
    assert signal.verdict == ALIVE


def test_process_probe_that_succeeds_and_finds_nothing_is_dead():
    """Positive evidence of absence — the tmux probe RAN and there is no session."""
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: True)
    # Assert
    assert signal.verdict == DEAD


def test_a_wedged_tmux_makes_a_missing_session_unknown_not_dead():
    """THE false-RED regression.

    ``TmuxManager.exists`` returns ``False`` both for "no such session" and for
    "I cannot talk to tmux at all". TUI is this fleet's DEFAULT runtime, so
    collapsing those two marks EVERY agent dead the moment tmux hiccups — or the
    moment the prober sits in a mount namespace that cannot see the tmux socket,
    which is exactly what happens inside a container.
    """
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: False)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_wedged_tmux_says_so_in_the_evidence():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: False)
    # Assert
    assert "could not look" in signal.detail


def test_a_probe_that_raises_is_unknown_not_dead():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeProbeExplodes())
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_non_tui_runtime_that_reports_down_is_dead():
    """The apptainer pidfile read is local and reliable — its False IS a probe."""
    # Arrange
    config = _Cfg("agent-a", "apptainer")
    # Act
    signal = process_signal(config, _RuntimeSaysDown())
    # Assert
    assert signal.verdict == DEAD


def test_process_signal_is_sourced_as_process():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysUp(), tmux_probe_ran=lambda: True)
    # Assert
    assert signal.source == SOURCE_PROCESS


# --------------------------------------------------------------------------
# heartbeat — a real file with a real mtime.
# --------------------------------------------------------------------------


def test_a_fresh_heartbeat_is_alive(tmp_path):
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0, "state": "running"}')
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert signal.verdict == ALIVE


def test_a_stale_heartbeat_is_unknown_never_dead(tmp_path):
    """The shared writer lives in ``sac listen``, not in the agent.

    When it stops, EVERY agent's beat freezes at once — a fact about the writer,
    not about any agent. Convicting on it would swap one fleet-wide false-death
    flood for another.
    """
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0, "state": "running"}')
    old = time.time() - 5086  # grant's measured staleness, 2026-07-14
    os.utime(hb, (old, old))
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_missing_heartbeat_is_unknown_never_dead(tmp_path):
    # Arrange
    hb = tmp_path / "does-not-exist.json"
    # Act
    signal = heartbeat_signal("ghost", path=hb)
    # Assert
    assert signal.verdict == UNKNOWN


def test_pid_zero_in_the_heartbeat_is_reported_as_deciding_nothing(tmp_path):
    """``pid: 0`` is a HARDCODED literal from the central listen-side writer.

    ``_tui_heartbeat_loop._beat_one`` calls ``write_fn(state_dir, pid=0, ...)``.
    It was never a fact about the agent, and the evidence line must say so — the
    whole "unfalsifiable row" panic was built on reading it as one.
    """
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0, "state": "running"}')
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert "decides nothing" in signal.detail


def test_heartbeat_signal_is_sourced_as_heartbeat(tmp_path):
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0}')
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert signal.source == SOURCE_HEARTBEAT


# --------------------------------------------------------------------------
# registry — a DECLARATION, graded asymmetrically against a REAL pid.
# --------------------------------------------------------------------------


def test_a_reaped_recorded_pid_is_positive_evidence_of_death(reaped_pid):
    """``os.kill(pid, 0)`` raising ESRCH means THAT process does not exist."""
    # Arrange
    rows = [{"name": "scitex-dev", "pid": reaped_pid}]
    # Act
    signal = registry_signal("scitex-dev", rows=rows)
    # Assert
    assert signal.verdict == DEAD


def test_a_live_recorded_pid_is_only_unknown_because_pids_are_recycled(live_pid):
    """Asymmetric on purpose: a reaped pid is proof, a live one may be a stranger."""
    # Arrange
    rows = [{"name": "grant", "pid": live_pid}]
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.verdict == UNKNOWN


def test_no_active_row_is_unknown_never_dead():
    """Absence of a declaration is not evidence of death.

    Reading it as one alarmed ~100 false criticals per sweep against agents that
    were serving HTTP in the same log.
    """
    # Arrange
    rows: list[dict] = []
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_row_recording_pid_zero_is_unknown_never_dead():
    """``grant``'s exact shape: a row that declares 'running' and records pid 0."""
    # Arrange
    rows = [{"name": "grant", "pid": 0}]
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_row_recording_a_null_pid_is_unknown_never_dead():
    """On this fleet, active rows routinely carry ``pid = NULL`` while healthy."""
    # Arrange
    rows = [{"name": "grant", "pid": None}]
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.verdict == UNKNOWN


def test_registry_signal_is_sourced_as_registry():
    # Arrange
    rows: list[dict] = []
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.source == SOURCE_REGISTRY
