"""``sac auth-events`` reads the timeline and never writes to it.

Drives the real click command with ``CliRunner`` against a real log file on
``tmp_path``, redirected via the production env knob — no mocks, no patching.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._authevents import (
    log_restart_attempted,
    log_restart_outcome,
)
from scitex_agent_container.cli_pkg._main import _MainGroup
from scitex_agent_container.cli_pkg.auth_events_cmds import auth_events

_LOG_ENV = "SAC_AUTH_EVENT_LOG"


@pytest.fixture
def event_log(tmp_path: Path):
    """A real, redirected auth-event log — the production env knob, no mocks."""
    target = tmp_path / "auth-events.jsonl"
    saved = os.environ.get(_LOG_ENV)
    os.environ[_LOG_ENV] = str(target)
    try:
        yield target
    finally:
        if saved is None:
            os.environ.pop(_LOG_ENV, None)
        else:
            os.environ[_LOG_ENV] = saved


def test_the_command_is_registered_on_the_main_group() -> None:
    """A command absent from the registry is a command nobody can run."""
    # Arrange
    group = _MainGroup

    # Act
    spec = group.LAZY_COMMANDS.get("auth-events")

    # Assert
    assert spec == "scitex_agent_container.cli_pkg.auth_events_cmds:auth_events"


def test_the_command_has_a_short_help_so_help_stays_lazy() -> None:
    """``--help`` must render without importing every command module."""
    # Arrange
    group = _MainGroup

    # Act
    short_help = group.LAZY_SHORT_HELPS.get("auth-events")

    # Assert
    assert short_help


def test_an_empty_log_says_so_rather_than_printing_nothing(
    event_log: Path,
) -> None:
    """ "Nothing matched" and "nothing was recorded" must not look identical.

    A silent terminal invites the reader to conclude the fleet is fine. Only
    one of those two empties is evidence about the fleet, and the command has
    to say which it is holding.
    """
    # Arrange
    runner = CliRunner()

    # Act
    result = runner.invoke(auth_events, ["--no-rotations"])

    # Assert
    assert "not evidence" in result.output


def test_unresolved_lists_a_restart_that_was_never_shown_to_work(
    event_log: Path,
) -> None:
    """The question the old rail could not answer, answered from the CLI."""
    # Arrange
    log_restart_attempted(agent="figrecipe", detail="wedged", path=event_log)

    # Act
    result = runner_output(["--unresolved", "--no-rotations"])

    # Assert
    assert "figrecipe" in result


def test_unresolved_omits_a_restart_that_was_shown_to_work(
    event_log: Path,
) -> None:
    """The filter must be able to come back empty, or it measures nothing."""
    # Arrange
    attempt_id = log_restart_attempted(
        agent="figrecipe", detail="wedged", path=event_log
    )
    log_restart_outcome(
        agent="figrecipe",
        attempt_id=attempt_id,
        succeeded=True,
        detail="ok",
        path=event_log,
    )

    # Act
    result = runner_output(["--unresolved", "--no-rotations"])

    # Assert
    assert "figrecipe" not in result


def test_reading_the_log_does_not_modify_it(event_log: Path) -> None:
    """READ-ONLY: an observability rail whose reader mutates is not one."""
    # Arrange
    log_restart_attempted(agent="figrecipe", detail="wedged", path=event_log)
    before = event_log.read_bytes()

    # Act
    runner_output(["--no-rotations"])

    # Assert
    assert event_log.read_bytes() == before


def runner_output(args: list[str]) -> str:
    """Invoke the real command and return its output."""
    return CliRunner().invoke(auth_events, args).output
