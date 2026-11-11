"""Listener previews describe arguments without claiming running state."""

import json
import re

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg.listen_cmds import listen


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


@pytest.mark.parametrize("route", ["listener", "listen"])
@pytest.mark.parametrize(
    ("action", "extra", "options"),
    [
        (
            "start",
            [],
            {"token_file": None, "allow_non_loopback": False, "print_token": False},
        ),
        ("stop", ["--grace-secs", "3.5"], {"grace_secs": 3.5}),
        ("restart", ["--grace-secs", "3.5"], {"grace_secs": 3.5}),
    ],
)
def test_listener_routes_preserve_argument_only_previews(
    route, action, extra, options, tmp_path
):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path)})
    args = [route, "--bind", "127.0.0.1:7345", action, "--dry-run"]
    args.extend(extra)
    # Act
    result = runner.invoke(main, args)
    plan = json.loads(result.stdout) if result.exit_code == 0 else {}
    # Assert
    assert (
        result.exit_code,
        tuple(
            plan.get(key)
            for key in ("action", "target", "dry_run", "scope", "host", "port")
        ),
        plan.get("options"),
    ) == (
        0,
        (action, "listen", True, "arguments_only", "127.0.0.1", 7345),
        options,
    )


@pytest.mark.parametrize("route", ["listener", "listen"])
@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_listener_routes_preserve_explicit_approval(route, action, tmp_path):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path), "SAC_ASSUME_YES": "1"})
    # Act
    result = runner.invoke(main, [route, action])
    # Assert
    assert (result.exit_code, result.stdout, "requires --yes/-y" in result.stderr) == (
        2,
        "",
        True,
    )


@pytest.mark.parametrize("route", ["listener", "listen"])
def test_listener_routes_forward_lifecycle_help(route, tmp_path):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path)})
    # Act
    result = runner.invoke(main, [route, "start", "--help"])
    # Assert
    assert (result.exit_code, "--yes" in result.stdout, "--bind" in result.stdout) == (
        0,
        True,
        True,
    )


def test_listener_namespace_hides_legacy_name_from_help():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--help"])
    # Assert
    assert (
        result.exit_code,
        bool(re.search(r"^\s+listener\s", result.stdout, re.M)),
        bool(re.search(r"^\s+listen\s", result.stdout, re.M)),
    ) == (0, True, False)


def test_listener_canonical_group_keeps_existing_backend():
    # Arrange
    ctx = click.Context(main)
    # Act
    group = main.get_command(ctx, "listener")
    # Assert
    assert (group is listen, group.invoke_without_command if group else None) == (
        True,
        True,
    )


def test_listener_legacy_alias_warns_on_stderr(tmp_path):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path)})
    # Act
    result = runner.invoke(main, ["listen", "start", "--dry-run"])
    # Assert
    assert (
        result.exit_code,
        "deprecated" in result.stderr,
        "listener" in result.stderr,
        "0.40.0" in result.stderr,
        json.loads(result.stdout)["action"],
    ) == (0, True, True, True, "start")


@pytest.mark.parametrize("route", ["listener", "listen"])
def test_listener_bare_route_keeps_nonloopback_guard(route, tmp_path):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path)})
    # Act
    result = runner.invoke(main, [route, "--bind", "0.0.0.0:7345"])
    # Assert
    assert (result.exit_code, "not loopback" in result.stderr, result.stdout) == (
        2,
        True,
        "",
    )
