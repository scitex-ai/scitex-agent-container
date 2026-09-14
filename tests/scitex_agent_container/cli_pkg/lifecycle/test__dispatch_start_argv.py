"""CLI session overrides survive the cross-host start boundary."""

from scitex_agent_container.cli_pkg.lifecycle._dispatch_start_argv import (
    remote_start_argv,
)


def test_fresh_override_reaches_remote_cli_argv():
    # Arrange / Act
    argv = remote_start_argv("cards", session_mode="fresh")
    # Assert
    assert argv[-2:] == ["--session", "fresh"]


def test_continue_override_reaches_remote_cli_argv():
    # Arrange / Act
    argv = remote_start_argv("cards", session_mode="continue")
    # Assert
    assert argv[-2:] == ["--session", "continue"]


def test_explicit_resume_reaches_remote_cli_argv_without_mode_conflict():
    # Arrange / Act
    argv = remote_start_argv("cards", session_mode="resume", resume_id="session-42")
    # Assert
    assert argv[-2:] == ["--resume", "session-42"]
