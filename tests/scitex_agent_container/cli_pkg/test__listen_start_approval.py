"""Explicit startup and token initialization require CLI approval."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._listen_start_approval import (
    require_listener_start_approval,
)
from scitex_agent_container.cli_pkg._main import main


@pytest.mark.parametrize("assume_yes", ["0", "1"])
@pytest.mark.parametrize("token_args", [[], ["--print-token"]])
def test_start_refuses_without_explicit_flag(assume_yes, token_args):
    # Arrange
    runner = CliRunner(env={"SAC_ASSUME_YES": assume_yes})
    args = ["listen", "start", *token_args]

    # Act
    result = runner.invoke(main, args)

    # Assert
    assert (result.exit_code, result.stdout, "requires --yes/-y" in result.stderr) == (
        2,
        "",
        True,
    )


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_token_preview_takes_precedence_over_approval(approval):
    # Arrange
    args = ["listen", "start", approval, "--print-token", "--dry-run"]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["action"],
        plan["options"]["print_token"],
        "token" in plan,
    ) == (0, "start", True, False)


def test_explicit_approval_passes_the_admission_helper():
    # Arrange
    approval = True

    # Act
    result = require_listener_start_approval(approval)

    # Assert
    assert result is None
