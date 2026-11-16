"""Canonical and retained Stop entries share the real protocol backend."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container._never_stop_when_task_remains._loop_guard import (
    MAX_CONSECUTIVE_BLOCKS,
)
from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg.hooks_group import hooks
from scitex_agent_container.cli_pkg.never_stop_when_task_remains_cmds import (
    never_stop_when_task_remains,
)

from .._never_stop_when_task_remains._fake_detector import (
    LIVE_STORE_JSON,
    write_detector,
)


@pytest.fixture
def owned_hook_env(tmp_path):
    provider = write_detector(
        tmp_path,
        returncode=2,
        stdout=json.dumps(
            {"decision": "block", "reason": "Continue the owned fixture", "future": [1]}
        ),
    )
    awaiting = write_detector(
        tmp_path, returncode=0, stdout="[]", name="owned-awaiting"
    )
    store = write_detector(
        tmp_path, returncode=0, stdout=LIVE_STORE_JSON, name="owned-store"
    )
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_RUNTIME_DIR": str(tmp_path),
        "SCITEX_DIR": str(tmp_path / "state"),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SAC_MAY_STOP_CMD": str(provider),
        "SAC_AWAITING_CARDS_CMD": str(awaiting),
        "SAC_AWAITING_STORE_CMD": str(store),
        "SAC_AWAITING_CARDS_TTL_S": "0",
    }


@pytest.mark.parametrize(
    "route", [["dev", "hooks", "decide-stop"], ["never-stop-when-task-remains"]]
)
def test_both_entries_forward_provider_hook_json(route, owned_hook_env):
    # Arrange
    args = [*route, "--agent", "owned-stop"]

    # Act
    result = CliRunner(env=owned_hook_env).invoke(
        main, args, input='{"hook_event_name":"Stop"}'
    )

    # Assert
    assert (result.exit_code, json.loads(result.stdout)) == (
        0,
        {"decision": "block", "reason": "Continue the owned fixture", "future": [1]},
    )


@pytest.mark.parametrize(
    "route", [["dev", "hooks", "decide-stop"], ["never-stop-when-task-remains"]]
)
def test_both_entries_allow_without_protocol_stdout(route, owned_hook_env, tmp_path):
    # Arrange
    provider = write_detector(tmp_path, returncode=0, name="owned-allow")
    env = {**owned_hook_env, "SAC_MAY_STOP_CMD": str(provider)}

    # Act
    result = CliRunner(env=env).invoke(
        main, [*route, "--agent", "owned-stop"], input="{}"
    )

    # Assert
    assert (result.exit_code, result.stdout) == (0, "")


def test_alternating_entries_share_the_bounded_loop_guard(owned_hook_env):
    # Arrange
    runner = CliRunner(env=owned_hook_env)
    routes = [["dev", "hooks", "decide-stop"], ["never-stop-when-task-remains"]] * 2

    # Act
    results = [
        runner.invoke(main, [*route, "--agent", "owned-stop"], input="{}")
        for route in routes
    ]
    decisions = [
        json.loads(result.stdout or "{}").get("decision") for result in results
    ]

    # Assert
    assert decisions == ["block"] * MAX_CONSECUTIVE_BLOCKS + [None]


def test_new_entry_uses_the_same_callback_and_options():
    # Arrange
    canonical = hooks.commands["decide-stop"]

    # Act
    binding = (
        canonical.callback is never_stop_when_task_remains.callback,
        canonical.params == never_stop_when_task_remains.params,
    )

    # Assert
    assert binding == (True, True)


def test_legacy_stop_route_warns_without_polluting_protocol_stdout(
    owned_hook_env, tmp_path
):
    # Arrange
    env = {**owned_hook_env, "XDG_RUNTIME_DIR": str(tmp_path)}
    # Act
    result = CliRunner(env=env).invoke(
        main, ["never-stop-when-task-remains", "--agent", "owned-stop"], input="{}"
    )
    # Assert
    assert (
        result.exit_code,
        json.loads(result.stdout),
        "deprecated" in result.stderr,
        "dev hooks decide-stop" in result.stderr,
        "v0.40.0" in result.stderr,
    ) == (
        0,
        {"decision": "block", "reason": "Continue the owned fixture", "future": [1]},
        True,
        True,
        True,
    )


def test_legacy_stop_route_is_hidden_from_root_help():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["--help"])
    # Assert
    assert (result.exit_code, "never-stop-when-task-remains" in result.stdout) == (
        0,
        False,
    )


def test_legacy_stop_help_forwards_canonical_options(tmp_path):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path)})
    # Act
    result = runner.invoke(main, ["never-stop-when-task-remains", "--help"])
    # Assert
    assert (
        result.exit_code,
        "--agent" in result.stdout,
        "dev hooks decide-stop" in result.stderr,
    ) == (0, True, True)


@pytest.mark.parametrize(
    "route", [["dev", "hooks", "decide-stop"], ["never-stop-when-task-remains"]]
)
def test_stop_routes_reject_unknown_options(route, tmp_path):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path)})
    # Act
    result = runner.invoke(main, [*route, "--not-a-real-option"], input="{}")
    # Assert
    assert (result.exit_code, "No such option" in result.stderr) == (2, True)
