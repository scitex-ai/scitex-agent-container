"""CLI session overrides survive the cross-host start boundary."""

from scitex_agent_container.cli_pkg.lifecycle._dispatch_start_argv import (
    remote_start_argv,
)


def test_force_controls_runtime_replacement() -> None:
    argv = remote_start_argv("cards", force=True)
    assert "--force" in argv


def test_fresh_override_reaches_remote_cli_argv():
    # Arrange
    session_mode = "fresh"
    # Act
    argv = remote_start_argv("cards", session_mode=session_mode)
    # Assert
    assert argv[-2:] == ["--session", "fresh"]


def test_continue_override_reaches_remote_cli_argv():
    # Arrange
    session_mode = "continue"
    # Act
    argv = remote_start_argv("cards", session_mode=session_mode)
    # Assert
    assert argv[-2:] == ["--session", "continue"]


def test_explicit_resume_reaches_remote_cli_argv_without_mode_conflict():
    # Arrange
    session_mode = "resume"
    # Act
    argv = remote_start_argv("cards", session_mode=session_mode, resume_id="session-42")
    # Assert
    assert argv[-2:] == ["--resume", "session-42"]
