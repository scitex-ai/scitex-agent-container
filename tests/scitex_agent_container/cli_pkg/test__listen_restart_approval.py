"""Restart requires explicit CLI approval before runtime/backend work."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container._listen._plane_targeting_argv import targets_listen_plane
from scitex_agent_container.cli_pkg._listen_restart_approval import (
    require_listener_restart_approval,
)
from scitex_agent_container.cli_pkg._main import main


@pytest.mark.parametrize("assume_yes", ["0", "1"])
def test_restart_refuses_without_explicit_flag(assume_yes):
    # Arrange
    runner = CliRunner(env={"SAC_ASSUME_YES": assume_yes})
    args = ["listen", "restart"]

    # Act
    result = runner.invoke(main, args)

    # Assert
    assert (result.exit_code, result.stdout, "requires --yes/-y" in result.stderr) == (
        2,
        "",
        True,
    )


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_restart_preview_takes_precedence_over_approval(approval):
    # Arrange
    args = ["listen", "restart", approval, "--dry-run"]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (result.exit_code, plan["action"], plan["dry_run"], "ok" in plan) == (
        0,
        "restart",
        True,
        False,
    )


def test_explicit_approval_passes_the_admission_helper():
    # Arrange
    approval = True

    # Act
    result = require_listener_restart_approval(approval)

    # Assert
    assert result is None


@pytest.mark.parametrize("console", ["sac", "scitex-agent-container"])
def test_approved_restart_retains_disruptive_command_classification(console):
    # Arrange
    args = [console, "listen", "restart", "--yes"]

    # Act
    result = targets_listen_plane(args)

    # Assert
    assert result.targets_plane is True
