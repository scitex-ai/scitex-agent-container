"""Real-binary tests for the tmux pane probes ``TmuxManager.pane_pid`` /
``.pane_dead`` (``_runners/_tmux/_tmux_probe.py``).

These back ``TuiSessionRuntime.is_running``'s IDENTITY-based liveness
(card ``sac-fix-live-agents-read-stopped``): the pane's process pid is a
namespace-robust liveness signal where the old ``session_activity``
freshness gate falsely read a live-but-idle agent as "stopped".

Fast hermetic branch (absent session → ``None``) always runs; the live
success path spins a real ``tmux`` session and is skipped when tmux is
not on PATH. No mocks — real ``subprocess.run`` against ``tmux`` itself.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from scitex_agent_container._runners._tmux.tmux import TmuxManager


def test_pane_pid_returns_none_for_absent_session() -> None:
    # Arrange — a session name guaranteed not to exist (uuid in name).
    bogus = f"tui-test-absent-{uuid.uuid4().hex[:8]}"
    # Act
    result = TmuxManager.pane_pid(bogus)
    # Assert
    assert result is None


def test_pane_dead_returns_none_for_absent_session() -> None:
    # Arrange
    bogus = f"tui-test-absent-{uuid.uuid4().hex[:8]}"
    # Act
    result = TmuxManager.pane_dead(bogus)
    # Assert
    assert result is None


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux binary not on PATH")
def test_pane_pid_returns_live_pid_for_running_session() -> None:
    # Arrange — spin a real detached session running a long sleep.
    name = f"tui-test-probe-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "sleep", "600"],
        check=True,
        capture_output=True,
    )
    try:
        # Act
        pid = TmuxManager.pane_pid(name)
        # Assert — a concrete, positive pid for the live pane process.
        assert isinstance(pid, int) and pid > 0
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", name], check=False, capture_output=True
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux binary not on PATH")
def test_pane_dead_false_for_running_session() -> None:
    # Arrange
    name = f"tui-test-probe-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "sleep", "600"],
        check=True,
        capture_output=True,
    )
    try:
        # Act
        dead = TmuxManager.pane_dead(name)
        # Assert — the pane's process is running, so not dead.
        assert dead is False
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", name], check=False, capture_output=True
        )
