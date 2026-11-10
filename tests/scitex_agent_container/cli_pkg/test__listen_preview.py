"""Listener previews describe arguments without claiming running state."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_listener_preview_reports_action_and_default_bind(action):
    # Arrange
    args = ["listen", action, "--dry-run"]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["action"],
        plan["target"],
        plan["dry_run"],
        plan["scope"],
        plan["host"],
        plan["port"],
        "ok" in plan,
    ) == (0, action, "listen", True, "arguments_only", "127.0.0.1", 7878, False)


def test_start_preview_preserves_verb_precedence_and_hides_token(tmp_path):
    # Arrange
    token = tmp_path / "owned token"
    token.write_text("owned-token-must-stay-private")
    args = [
        "listen",
        "--bind",
        "127.0.0.1:7123",
        "start",
        "--bind",
        "[::1]:7345",
        "--token-file",
        str(token),
        "--print-token",
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["host"],
        plan["port"],
        plan["options"]["token_file"],
        plan["options"]["print_token"],
        "owned-token-must-stay-private" in result.output,
        token.read_text(),
    ) == (0, "::1", 7345, str(token), True, False, "owned-token-must-stay-private")


def test_start_preview_inherits_group_options_without_observing_state(tmp_path):
    # Arrange
    token = tmp_path / "unresolved token"
    args = [
        "listen",
        "--bind",
        "0.0.0.0:7790",
        "--token-file",
        str(token),
        "--allow-non-loopback",
        "--print-token",
        "start",
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["host"],
        plan["port"],
        plan["options"],
        token.exists(),
    ) == (
        0,
        "0.0.0.0",
        7790,
        {"token_file": str(token), "allow_non_loopback": True, "print_token": True},
        False,
    )


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_listener_preview_preserves_grace_and_group_bind(action):
    # Arrange
    args = [
        "listen",
        "--bind",
        "127.0.0.1:7891",
        action,
        "--grace-secs",
        "3.5",
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (result.exit_code, plan["port"], plan["options"], "status" in plan) == (
        0,
        7891,
        {"grace_secs": 3.5},
        False,
    )


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["start", "--bind", "0.0.0.0:7878"], "not loopback"),
        (["start", "--bind", "broken"], "--bind"),
        (["stop", "--grace-secs", "nan"], "finite"),
        (["restart", "--grace-secs", "inf"], "finite"),
    ],
)
def test_listener_preview_refuses_invalid_arguments_before_side_effects(args, reason):
    # Arrange
    command = ["listen", *args, "--dry-run"]

    # Act
    result = CliRunner().invoke(main, command)

    # Assert
    assert (result.exit_code, reason in result.stderr, result.stdout) == (2, True, "")
