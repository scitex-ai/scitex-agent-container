"""The post-ack probe must stop convicting every TUI agent in the fleet.

NO MOCKS. Real files on disk, a real live OS process, a real reaped pid, and the
real probe.

THE BUG (measured on the live fleet, 2026-07-14)
------------------------------------------------
``_probe_post_ack_liveness`` waits for ``<runtime_dir>/apptainer_pid``. Only the
APPTAINER runtime writes that file — a ``tui`` agent launches via tmux and never
writes one, by construction. ``tui`` is this fleet's DEFAULT runtime.

So every TUI agent brokered through ``POST /agents`` waited out the grace window,
failed to find a pidfile it was never going to write, and got stamped
``startup_failed`` / ``post_ack_no_apptainer_pid`` + a 502 — while being
perfectly alive:

  * ``grant``          — carried that exact marker while holding a live tmux
                         session, a fresh heartbeat, and 1 live inbox subscriber
                         (``inbox_reachable: reachable``).
  * ``scitex-writer``  — carried it while ANSWERING a peer's message.

A bogus ``startup_failed`` is read downstream as "this agent is dead", and the
remedy for that is ``--force --fresh``. So a probe looking for the wrong file
talked operators into destroying healthy agents.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from scitex_agent_container._listen._agent_exec_liveness import (
    _probe_post_ack_liveness,
)


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


def _tui(_name: str) -> bool:
    """This agent's runtime does NOT write an apptainer pidfile (it is tui)."""
    return False


def _apptainer(_name: str) -> bool:
    """This agent's runtime DOES write an apptainer pidfile."""
    return True


# --------------------------------------------------------------------------
# The regression: a healthy TUI agent must not be stamped startup_failed.
# --------------------------------------------------------------------------


def test_a_live_tui_agent_is_not_reported_as_a_startup_failure(tmp_path):
    """``grant``'s exact shape: alive, no apptainer_pid file, and it never will be."""
    # Arrange — no apptainer_pid file exists, and never will: this is a TUI agent.
    runtime_dir = tmp_path
    # Act
    failure = _probe_post_ack_liveness(
        runtime_dir,
        name="grant",
        timeout_s=1.0,
        poll_interval_s=0.01,
        writes_pidfile_fn=_tui,
        runtime_is_up_fn=lambda _n: True,
    )
    # Assert
    assert failure is None


def test_a_tui_agent_whose_probe_could_not_run_is_not_a_startup_failure(tmp_path):
    """UNKNOWN authorises NOTHING — least of all a ``startup_failed`` stamp.

    A wedged tmux (or a prober that cannot see the tmux socket) must not be
    rendered as a failed start on an agent that may be perfectly alive.
    """
    # Arrange
    runtime_dir = tmp_path
    # Act
    failure = _probe_post_ack_liveness(
        runtime_dir,
        name="grant",
        timeout_s=0.3,
        poll_interval_s=0.01,
        writes_pidfile_fn=_tui,
        runtime_is_up_fn=lambda _n: None,  # the probe could not run
    )
    # Assert
    assert failure is None


def test_an_unresolvable_spec_is_not_a_startup_failure(tmp_path):
    """We could not tell WHICH runtime this is. That convicts nobody."""
    # Arrange
    runtime_dir = tmp_path
    # Act
    failure = _probe_post_ack_liveness(
        runtime_dir,
        name="mystery",
        timeout_s=0.3,
        poll_interval_s=0.01,
        writes_pidfile_fn=lambda _n: None,  # unresolvable spec
    )
    # Assert
    assert failure is None


def test_a_genuinely_absent_tui_session_IS_reported(tmp_path):
    """The probe must keep its teeth: a probe that RAN and found nothing is a fail."""
    # Arrange
    runtime_dir = tmp_path
    # Act
    failure = _probe_post_ack_liveness(
        runtime_dir,
        name="stillborn",
        timeout_s=0.3,
        poll_interval_s=0.01,
        writes_pidfile_fn=_tui,
        runtime_is_up_fn=lambda _n: False,  # probe SUCCEEDED; nothing is there
    )
    # Assert
    assert failure is not None


def test_a_genuinely_absent_tui_session_is_reported_as_session_absent(tmp_path):
    # Arrange
    runtime_dir = tmp_path
    # Act
    failure = _probe_post_ack_liveness(
        runtime_dir,
        name="stillborn",
        timeout_s=0.3,
        poll_interval_s=0.01,
        writes_pidfile_fn=_tui,
        runtime_is_up_fn=lambda _n: False,
    )
    # Assert
    assert failure[0] == "post_ack_session_absent"


# --------------------------------------------------------------------------
# ...and the apptainer path keeps working exactly as before.
# --------------------------------------------------------------------------


def test_an_apptainer_agent_with_a_live_pidfile_is_healthy(tmp_path, live_pid):
    # Arrange
    (tmp_path / "apptainer_pid").write_text(str(live_pid))
    # Act
    failure = _probe_post_ack_liveness(
        tmp_path,
        name="sdk-agent",
        timeout_s=1.0,
        poll_interval_s=0.01,
        writes_pidfile_fn=_apptainer,
    )
    # Assert
    assert failure is None


def test_an_apptainer_agent_with_no_pidfile_is_still_a_startup_failure(tmp_path):
    """The original Layer-3 fail-loud contract is preserved for the runtime that
    actually writes the file."""
    # Arrange — an apptainer agent that never wrote its pidfile.
    runtime_dir = tmp_path
    # Act
    failure = _probe_post_ack_liveness(
        runtime_dir,
        name="sdk-agent",
        timeout_s=0.3,
        poll_interval_s=0.01,
        writes_pidfile_fn=_apptainer,
    )
    # Assert
    assert failure[0] == "post_ack_no_apptainer_pid"


def test_omitting_the_name_keeps_the_legacy_pidfile_only_behaviour(tmp_path):
    """Back-compat: a caller with no agent name still gets the old probe."""
    # Arrange
    runtime_dir = tmp_path
    # Act
    failure = _probe_post_ack_liveness(runtime_dir, timeout_s=0.3, poll_interval_s=0.01)
    # Assert
    assert failure[0] == "post_ack_no_apptainer_pid"
