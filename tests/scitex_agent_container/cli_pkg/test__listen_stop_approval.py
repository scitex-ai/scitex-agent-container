"""Listener stop refuses before teardown unless explicitly approved."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main


@pytest.mark.parametrize("assume_yes", ["0", "1"])
@pytest.mark.parametrize("json_option", [[], ["--json"]])
def test_stop_refuses_without_explicit_flag(assume_yes, json_option):
    # Arrange
    runner = CliRunner(env={"SAC_ASSUME_YES": assume_yes})
    args = ["listen", "stop", *json_option]

    # Act
    result = runner.invoke(main, args)

    # Assert
    assert (result.exit_code, "requires --yes/-y" in result.stderr, result.stdout) == (
        2,
        True,
        "",
    )


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_stop_preview_takes_precedence_over_approval(approval):
    # Arrange
    args = ["listen", "stop", approval, "--json", "--dry-run"]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (result.exit_code, plan["action"], plan["dry_run"], "ok" in plan) == (
        0,
        "stop",
        True,
        False,
    )
