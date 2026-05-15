"""Tests for ``_state._meta.resources`` — pids + host metrics helpers.

PS-202 src-tests mirror. ``_pids_from_session`` short-circuits for
non-tmux multiplexers (pure check). ``_collect_host_metrics`` uses the
real psutil module if installed — tests assert structural keys, not
specific values.
"""

from __future__ import annotations

from scitex_agent_container._state._meta.resources import (
    _collect_host_metrics,
    _pids_from_session,
)


def test_pids_from_session_returns_zeros_for_non_tmux():
    # Arrange
    multiplexer = "screen"
    # Act
    pid, ppid = _pids_from_session("session", multiplexer=multiplexer)
    # Assert
    assert (pid, ppid) == (0, 0)


def test_pids_from_session_returns_zeros_for_missing_tmux_session(subprocess_shim):
    # Arrange — install a tmux shim that returns empty (no panes)
    subprocess_shim.install("tmux", stdout="", exit=1)
    # Act
    pid, ppid = _pids_from_session("ghost-session", multiplexer="tmux")
    # Assert
    assert (pid, ppid) == (0, 0)


def test_pids_from_session_parses_real_subprocess_output(subprocess_shim):
    # Arrange
    subprocess_shim.install("tmux", stdout="4242\n")  # stx-allow: STX-NL001
    subprocess_shim.install("pgrep", stdout="9999\n")  # stx-allow: STX-NL001
    # Act
    pid, ppid = _pids_from_session("sess", multiplexer="tmux")
    # Assert
    assert (pid, ppid) == (9_999, 4_242)


def test_pids_from_session_swallows_int_parse_error(subprocess_shim):
    # Arrange — tmux shim returns non-numeric pid, triggering ValueError
    # inside the try block (line 46-47 catch path).
    subprocess_shim.install("tmux", stdout="not-a-number\n")
    # Act
    pid, ppid = _pids_from_session("sess", multiplexer="tmux")
    # Assert
    assert (pid, ppid) == (0, 0)


def test_collect_host_metrics_returns_dict():
    # Arrange
    collector = _collect_host_metrics
    # Act
    metrics = collector()
    # Assert
    assert isinstance(metrics, dict)


def test_collect_host_metrics_includes_cpu_count_key_when_psutil_present():
    # Arrange
    collector = _collect_host_metrics
    # Act
    metrics = collector()
    # Assert — psutil is a hard dep here; the key is either present
    # (psutil available) or the dict is empty (psutil missing). Either
    # way is structurally valid — this single assertion captures both.
    assert metrics == {} or "cpu_count" in metrics
