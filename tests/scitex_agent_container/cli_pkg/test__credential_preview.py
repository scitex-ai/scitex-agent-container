"""Credential distribution previews admit arguments without claiming readiness."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main


@pytest.mark.parametrize("command", ["send-credentials", "keepalive"])
@pytest.mark.parametrize(
    ("selection", "all_accounts", "accounts"),
    [
        (["--all"], True, []),
        (["--account", "unresolved-account"], False, ["unresolved-account"]),
    ],
)
def test_distribution_preview_reports_only_supplied_arguments(
    tmp_path, command, selection, all_accounts, accounts
):
    # Arrange
    credentials = tmp_path / "credential.json"
    credentials.write_text("owned credential content must stay private")
    args = [
        "accounts",
        command,
        *selection,
        "--to",
        "unresolved-peer",
        "--optional-peer",
        "unresolved-peer",
        "--sweep",
        "--dry-run",
        "--json",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["scope"],
        plan["dry_run"],
        plan["all_accounts"],
        plan["accounts"],
        plan["peers"],
        plan["sweep"],
        plan["min_validity"],
        "owned credential content" in result.output,
        credentials.read_text(),
    ) == (
        0,
        "arguments_only",
        True,
        all_accounts,
        accounts,
        ["unresolved-peer"],
        True,
        None,
        False,
        "owned credential content must stay private",
    )


@pytest.mark.parametrize(
    ("selection", "optional", "reason"),
    [
        ([], [], "exactly one"),
        (["--all", "--account", "both"], [], "exactly one"),
        (["--all"], ["--optional-peer", "not-a-target"], "absent from --to"),
    ],
)
def test_distribution_preview_retains_pure_argument_refusals(
    selection, optional, reason
):
    # Arrange
    args = [
        "accounts",
        "send-credentials",
        *selection,
        "--to",
        "peer-a",
        *optional,
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)

    # Assert
    assert (result.exit_code, reason in result.stderr, result.stdout) == (2, True, "")


def test_distribution_preview_preserves_explicit_floor_and_destination():
    # Arrange
    args = [
        "accounts",
        "send-credentials",
        "--account",
        "unresolved",
        "--to",
        "peer-b",
        "--to",
        "peer-a",
        "--min-validity",
        "17",
        "--remote-path",
        "/unresolved/destination.json",
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["peers"],
        plan["min_validity"],
        plan["remote_path"],
        plan["sweep"],
        "ok" in plan,
    ) == (
        0,
        ["peer-b", "peer-a"],
        17,
        "/unresolved/destination.json",
        False,
        False,
    )
