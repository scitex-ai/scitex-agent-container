"""PS-202 mirror: real tests for the ``auto.daemon`` PID lifecycle.

``run_daemon`` blocks on signals and ``send_accept_once`` pulls in
sibling submodules that move around in this PR's reorganization — so
this mirror file sticks to the deterministic surface: the PID-file
lifecycle helpers (``write_pid`` / ``read_pid`` / ``clear_pid``)
under a ``tmp_path``-redirected ``_PID_DIR``.

Test style (STX-TQ002 / TQ007): explicit ``# Arrange`` / ``# Act`` /
``# Assert`` markers in order; one assertion per test.
"""

from __future__ import annotations

import os

from scitex_agent_container._runners._tmux.auto import daemon as _daemon


def test_write_pid_then_read_pid_returns_current_process_id(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.setattr(_daemon, "_PID_DIR", tmp_path)
    name = "agent-roundtrip"
    # Act
    _daemon.write_pid(name)
    # Assert
    assert _daemon.read_pid(name) == os.getpid()


def test_clear_pid_removes_pid_file_from_registry_dir(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.setattr(_daemon, "_PID_DIR", tmp_path)
    name = "agent-clear"
    _daemon.write_pid(name)
    # Act
    _daemon.clear_pid(name)
    # Assert
    assert _daemon.read_pid(name) is None


def test_read_pid_returns_none_when_pid_file_missing(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.setattr(_daemon, "_PID_DIR", tmp_path)
    # Act
    result = _daemon.read_pid("never-written")
    # Assert
    assert result is None
