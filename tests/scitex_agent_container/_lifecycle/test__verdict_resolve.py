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
    _tmux_probe_ran,
    heartbeat_signal,
    process_signal,
    registry_signal,
    remote_process_signal,
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


def _on_the_host() -> bool:
    """We are NOT in a container, so a pid check is a real sensor.

    Pinned explicitly wherever a resolver bottoms out in ``os.kill(pid, 0)``. A
    pid only means anything in the namespace that minted it, so these tests would
    otherwise return DEAD on a CI runner and UNKNOWN inside a container —
    the same code, two answers, depending on where pytest happened to run. The
    instrument-independence suite covers the in-container half explicitly.
    """
    return False


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
    """The apptainer pidfile read is a real probe — ON THE HOST.

    ``in_sif_fn`` is pinned rather than left to the ambient environment, and that
    is not a formality: ``ApptainerRuntime.is_running`` is ``os.kill(pid, 0)``,
    which is only a sensor in the pid namespace that MINTED the pid. Run from
    inside a container it reads "reaped" for every healthy agent on the host. So
    "is this a probe at all" depends on where the test runs — and a test whose
    verdict flips between CI and a container is testing the environment, not the
    code.
    """
    # Arrange
    config = _Cfg("agent-a", "apptainer")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), in_sif_fn=_on_the_host)
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
# The false-DEAD this module produced on itself, caught in development.
# --------------------------------------------------------------------------


def test_an_empty_tmux_snapshot_from_inside_a_container_is_not_an_observation():
    """MEASURED 2026-07-14 — and it convicted a live agent.

    From inside a SIF, ``tmux ls`` prints "no server running on
    /tmp/tmux-1000/default": TRUE of the CONTAINER's own /tmp, and one of
    ``_tmux_probe``'s "no server ⇒ confirmed-empty" markers. So
    ``list_sessions_activity()`` does not FAIL — it SUCCEEDS and returns ``{}``,
    i.e. "the fleet is genuinely empty". The host's tmux is merely in another
    mount namespace.

    Run from in there, that made ``process_signal`` return DEAD for ``grant`` —
    an agent holding a live tmux session, a fresh heartbeat and a live inbox
    subscriber on the host. A confident, well-evidenced, entirely false death
    verdict. Only the corroboration gate stopped it authorising anything.
    """
    # Arrange: the real "empty snapshot" + the real "we are in a container".
    empty_snapshot = lambda **_kw: {}  # noqa: E731  — what tmux really returns
    in_a_container = lambda: True  # noqa: E731
    # Act
    ran = _tmux_probe_ran(snapshot_fn=empty_snapshot, in_sif_fn=in_a_container)
    # Assert — a non-observation must not be read as an observation.
    assert ran is None


def test_an_empty_tmux_snapshot_on_the_bare_host_IS_an_observation():
    """The probe must keep its teeth where it CAN see: on the host, empty is empty."""
    # Arrange
    empty_snapshot = lambda **_kw: {}  # noqa: E731
    on_the_host = lambda: False  # noqa: E731
    # Act
    ran = _tmux_probe_ran(snapshot_fn=empty_snapshot, in_sif_fn=on_the_host)
    # Assert
    assert ran is True


def test_a_failed_tmux_probe_is_never_an_observation():
    # Arrange — list_sessions_activity's own contract: None = the probe FAILED.
    failed_probe = lambda **_kw: None  # noqa: E731
    on_the_host = lambda: False  # noqa: E731
    # Act
    ran = _tmux_probe_ran(snapshot_fn=failed_probe, in_sif_fn=on_the_host)
    # Assert
    assert ran is None


def test_a_tui_agent_is_unknown_not_dead_when_the_probe_cannot_see_the_fleet():
    """The fix, at the signal level: cannot see ⇒ UNKNOWN, never DEAD."""
    # Arrange
    config = _Cfg("grant", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: None)
    # Assert
    assert signal.verdict == UNKNOWN


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
    """``os.kill(pid, 0)`` raising ESRCH means THAT process does not exist.

    True only in the namespace that minted the pid — hence the pinned
    ``in_sif_fn``; see :func:`_on_the_host`.
    """
    # Arrange
    rows = [{"name": "scitex-dev", "pid": reaped_pid}]
    # Act
    signal = registry_signal("scitex-dev", rows=rows, in_sif_fn=_on_the_host)
    # Assert
    assert signal.verdict == DEAD


def test_a_live_recorded_pid_is_only_unknown_because_pids_are_recycled(live_pid):
    """Asymmetric on purpose: a reaped pid is proof, a live one may be a stranger."""
    # Arrange
    rows = [{"name": "grant", "pid": live_pid}]
    # Act
    signal = registry_signal("grant", rows=rows, in_sif_fn=_on_the_host)
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


# --------------------------------------------------------------------------
# remote_process_signal — control-plane cross-host liveness. ssh is INJECTED
# (a real callable returning rc). Same doctrine: a probe that could not run
# is UNKNOWN, never DEAD — so a wedged ssh cannot slander a live remote agent.
# --------------------------------------------------------------------------


def test_remote_process_signal_rc0_session_present_is_alive():
    # Arrange
    cfg = _Cfg("spartan-dev", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=lambda _argv: 0)
    # Assert
    assert signal.verdict == ALIVE


def test_remote_process_signal_rc1_no_remote_session_is_dead():
    # Arrange
    cfg = _Cfg("spartan-dev", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=lambda _argv: 1)
    # Assert
    assert signal.verdict == DEAD


def test_remote_process_signal_ssh_connect_failure_is_unknown_never_dead():
    # Arrange — rc 255 is ssh's own connection-failed code.
    cfg = _Cfg("spartan-dev", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=lambda _argv: 255)
    # Assert
    assert signal.verdict == UNKNOWN


def test_remote_process_signal_run_ssh_raising_is_unknown_never_dead():
    # Arrange
    cfg = _Cfg("spartan-dev", "tui")

    def _boom(_argv):
        raise OSError("ssh shell-out exploded")

    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=_boom)
    # Assert
    assert signal.verdict == UNKNOWN
