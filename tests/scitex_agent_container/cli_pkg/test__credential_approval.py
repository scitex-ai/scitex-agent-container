"""Credential distribution requires an explicit flag, independently of previews."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main


@pytest.mark.parametrize("command", ["send-credentials", "keepalive"])
@pytest.mark.parametrize("assume_yes", ["0", "1"])
def test_distribution_refuses_without_explicit_approval(command, assume_yes):
    # Arrange
    runner = CliRunner(env={"SAC_ASSUME_YES": assume_yes})
    args = ["accounts", command, "--all", "--to", "unresolved-peer", "--json"]

    # Act
    result = runner.invoke(main, args)

    # Assert
    assert (result.exit_code, "requires --yes/-y" in result.stderr, result.stdout) == (
        2,
        True,
        "",
    )


@pytest.mark.parametrize("command", ["send-credentials", "keepalive"])
@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_distribution_preview_takes_precedence_over_approval(command, approval):
    # Arrange
    args = [
        "accounts",
        command,
        approval,
        "--all",
        "--to",
        "unresolved-peer",
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (result.exit_code, plan["dry_run"], plan["scope"], plan["all_accounts"]) == (
        0,
        True,
        "arguments_only",
        True,
    )


@pytest.mark.parametrize(
    ("selection", "optional", "reason"),
    [
        ([], [], "exactly one"),
        (["--all"], ["--optional-peer", "not-targeted"], "absent from --to"),
    ],
)
def test_distribution_checks_pure_arguments_before_refusing_approval(
    selection, optional, reason
):
    # Arrange
    args = ["accounts", "send-credentials", *selection, "--to", "peer", *optional]

    # Act
    result = CliRunner().invoke(main, args)

    # Assert
    assert (
        result.exit_code,
        reason in result.stderr,
        "requires --yes" in result.stderr,
    ) == (
        2,
        True,
        False,
    )
