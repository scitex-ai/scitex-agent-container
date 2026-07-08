"""Unit tests for the pure TUI liveness / responsiveness decisions
(``runtimes/_tui_liveness.py``).

Root cause these guard (card ``sac-fix-live-agents-read-stopped``): the
old ``TuiSessionRuntime.is_running`` gated liveness on
``session_activity`` freshness, so a live-but-idle agent read "stopped".
``pane_process_alive`` is the identity-based replacement (session exists
AND pane process alive); ``is_responsive_from_activity`` preserves the
old activity-freshness rule as a separate hang-detection signal.

STX-TQ002 AAA markers + one-assert. No mocks — collaborators are injected
as plain callables (real Protocol impls), and ``pid_alive`` hits the real
``os.kill`` syscall against the test process's own pid.
"""

from __future__ import annotations

import os

from scitex_agent_container.runtimes._tui_liveness import (
    is_responsive_from_activity,
    pane_process_alive,
    pid_alive,
)

# A pid the OS will never have assigned to a live process during the test.
_DEAD_PID = 2_147_483_646


# ---------------------------------------------------------------------------
# pid_alive
# ---------------------------------------------------------------------------


def test_pid_alive_true_for_own_process() -> None:
    # Arrange
    own_pid = os.getpid()
    # Act
    result = pid_alive(own_pid)
    # Assert
    assert result is True


def test_pid_alive_false_for_dead_pid() -> None:
    # Arrange
    pid = _DEAD_PID
    # Act
    result = pid_alive(pid)
    # Assert
    assert result is False


def test_pid_alive_false_for_zero() -> None:
    # Arrange — pid 0 must NOT reach os.kill(0, 0) (that signals the
    # caller's whole process group); it is not a real pane process.
    pid = 0
    # Act
    result = pid_alive(pid)
    # Assert
    assert result is False


def test_pid_alive_false_for_none() -> None:
    # Arrange
    pid = None
    # Act
    result = pid_alive(pid)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# pane_process_alive — LIVENESS
# ---------------------------------------------------------------------------


def test_pane_process_alive_false_when_session_absent() -> None:
    # Arrange — exists_fn reports no session.
    exists_fn = lambda _n: False  # noqa: E731
    # Act
    result = pane_process_alive(
        "tui-x", exists_fn=exists_fn, pane_pid_fn=lambda _n: os.getpid()
    )
    # Assert
    assert result is False


def test_pane_process_alive_true_when_pane_pid_live() -> None:
    # Arrange — session exists and its pane pid is the live test process.
    pane_pid_fn = lambda _n: os.getpid()  # noqa: E731
    # Act
    result = pane_process_alive(
        "tui-x", exists_fn=lambda _n: True, pane_pid_fn=pane_pid_fn
    )
    # Assert
    assert result is True


def test_pane_process_alive_false_when_pane_pid_dead() -> None:
    # Arrange — session exists but the pane pid is a dead pid.
    pane_pid_fn = lambda _n: _DEAD_PID  # noqa: E731
    # Act
    result = pane_process_alive(
        "tui-x", exists_fn=lambda _n: True, pane_pid_fn=pane_pid_fn
    )
    # Assert
    assert result is False


def test_pane_process_alive_false_when_pane_dead_flag_true() -> None:
    # Arrange — retained-dead pane (remain-on-exit) short-circuits to dead
    # even though pane_pid would be a live pid.
    pane_dead_fn = lambda _n: True  # noqa: E731
    # Act
    result = pane_process_alive(
        "tui-x",
        exists_fn=lambda _n: True,
        pane_dead_fn=pane_dead_fn,
        pane_pid_fn=lambda _n: os.getpid(),
    )
    # Assert
    assert result is False


def test_pane_process_alive_true_when_no_pid_probe_available() -> None:
    # Arrange — legacy multiplexer without a pane_pid probe: session-exists
    # is itself the liveness signal.
    exists_fn = lambda _n: True  # noqa: E731
    # Act
    result = pane_process_alive("tui-x", exists_fn=exists_fn, pane_pid_fn=None)
    # Assert
    assert result is True


def test_pane_process_alive_true_when_pid_probe_returns_none() -> None:
    # Arrange — probe present but yields an unreadable pid → defer to
    # session-exists (never a false "stopped").
    pane_pid_fn = lambda _n: None  # noqa: E731
    # Act
    result = pane_process_alive(
        "tui-x", exists_fn=lambda _n: True, pane_pid_fn=pane_pid_fn
    )
    # Assert
    assert result is True


# ---------------------------------------------------------------------------
# is_responsive_from_activity — RESPONSIVENESS
# ---------------------------------------------------------------------------


def test_is_responsive_true_when_activity_within_window() -> None:
    # Arrange — activity 10s ago, 300s window.
    activity = 990.0
    # Act
    result = is_responsive_from_activity(activity, now=1000.0, max_idle_s=300.0)
    # Assert
    assert result is True


def test_is_responsive_false_when_activity_stale() -> None:
    # Arrange — activity 400s ago, 300s window.
    activity = 600.0
    # Act
    result = is_responsive_from_activity(activity, now=1000.0, max_idle_s=300.0)
    # Assert
    assert result is False


def test_is_responsive_false_when_activity_none() -> None:
    # Arrange
    activity = None
    # Act
    result = is_responsive_from_activity(activity, now=1000.0, max_idle_s=300.0)
    # Assert
    assert result is False
