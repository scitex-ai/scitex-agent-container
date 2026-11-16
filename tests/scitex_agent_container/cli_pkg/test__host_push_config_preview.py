"""Approval and side-effect-free argument plans for configuration push."""

import importlib
import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main
from scitex_agent_container.cli_pkg._host_push_config import host_push_config


@pytest.fixture
def forbidden_state():
    module = importlib.import_module("scitex_agent_container.cli_pkg._host_push_config")
    original = module._load_cfg
    calls = []

    def forbidden():
        calls.append("load-config")
        raise AssertionError("preview/refusal must precede config or token access")

    module._load_cfg = forbidden
    try:
        yield calls
    finally:
        module._load_cfg = original


@pytest.mark.parametrize(
    "args",
    [
        ["peer-a"],
        ["--all"],
        ["peer-a", "--adopt"],
        ["peer-a", "--with-tokens"],
        ["--rotate-tokens", "peer-a"],
    ],
)
def test_unapproved_mutation_refuses_before_state_access(forbidden_state, args):
    # Arrange
    # Act
    result = CliRunner().invoke(host_push_config, args)
    # Assert
    assert (
        result.exit_code,
        "requires --yes/-y" in result.output,
        forbidden_state,
    ) == (2, True, [])


@pytest.mark.parametrize(
    "args",
    [
        ["peer-a", "--yes", "--dry-run"],
        ["peer-a", "-y", "--check"],
    ],
)
def test_conflicting_approval_refuses_before_state_access(forbidden_state, args):
    # Arrange
    # Act
    result = CliRunner().invoke(host_push_config, args)
    # Assert
    assert (
        result.exit_code,
        "cannot be combined" in result.output,
        forbidden_state,
    ) == (2, True, [])


@pytest.mark.parametrize(
    "args, expected",
    [
        (
            ["peer-a"],
            {
                "peer": "peer-a",
                "mode": "push",
                "all_peers": False,
                "adopt": False,
                "with_tokens": False,
                "rotate_peer": "",
            },
        ),
        (
            ["--all"],
            {
                "peer": None,
                "mode": "push",
                "all_peers": True,
                "adopt": False,
                "with_tokens": False,
                "rotate_peer": "",
            },
        ),
        (
            ["peer-a", "--adopt"],
            {
                "peer": "peer-a",
                "mode": "push",
                "all_peers": False,
                "adopt": True,
                "with_tokens": False,
                "rotate_peer": "",
            },
        ),
        (
            ["peer-a", "--with-tokens"],
            {
                "peer": "peer-a",
                "mode": "push",
                "all_peers": False,
                "adopt": False,
                "with_tokens": True,
                "rotate_peer": "",
            },
        ),
        (
            ["--rotate-tokens", "peer-a"],
            {
                "peer": None,
                "mode": "rotate-tokens",
                "all_peers": False,
                "adopt": False,
                "with_tokens": False,
                "rotate_peer": "peer-a",
            },
        ),
        (
            ["peer-a", "--check"],
            {
                "peer": "peer-a",
                "mode": "check",
                "all_peers": False,
                "adopt": False,
                "with_tokens": False,
                "rotate_peer": "",
            },
        ),
    ],
)
def test_preview_describes_modes_without_state_access(forbidden_state, args, expected):
    # Arrange
    flags = [
        "--dry-run",
        "--json",
        "--timeout",
        "17",
        "--listen-port",
        "19001",
        "--diff",
        "--no-tokens",
    ]
    # Act
    result = CliRunner().invoke(host_push_config, [*args, *flags])
    # Assert
    assert (result.exit_code, json.loads(result.output), forbidden_state) == (
        0,
        {
            "operation": "host.push-config",
            "dry_run": True,
            "scope": "arguments_only",
            "verified": False,
            "no_tokens": True,
            "listen_port": 19001,
            "timeout": 17,
            "show_diff": True,
            **expected,
        },
        [],
    )


@pytest.mark.parametrize("approval", ["--yes", "--dry-run"])
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["peer-a", "--all"],
        ["--all", "--adopt"],
        ["--rotate-tokens", "peer-a", "--all"],
        ["--rotate-tokens", "peer-a", "--check"],
        ["--rotate-tokens", "peer-a", "peer-b"],
        ["--rotate-tokens", "peer-a", "--adopt"],
        ["--check", "--with-tokens", "peer-a"],
        ["--check", "--adopt", "peer-a"],
        ["peer-*"],
    ],
)
def test_existing_structural_refusals_still_precede_state(
    forbidden_state, approval, args
):
    # Arrange
    # Act
    result = CliRunner().invoke(host_push_config, [*args, approval])
    # Assert
    assert (result.exit_code, forbidden_state) == (2, [])


@pytest.mark.parametrize("prefix, suffix", [(["--json"], []), ([], ["--json"])])
def test_root_and_local_json_preview_match(forbidden_state, prefix, suffix):
    # Arrange
    args = [
        *prefix,
        "host",
        "push-config",
        "--rotate-tokens",
        "peer-a",
        "--dry-run",
        *suffix,
    ]
    # Act
    result = CliRunner().invoke(main, args)
    # Assert
    assert (result.exit_code, json.loads(result.output)["mode"], forbidden_state) == (
        0,
        "rotate-tokens",
        [],
    )


def test_text_preview_disclaims_token_access_and_restart(forbidden_state):
    # Arrange
    # Act
    result = CliRunner().invoke(
        host_push_config, ["peer-a", "--with-tokens", "--dry-run"]
    )
    # Assert
    assert (
        result.exit_code,
        "no config/token read, SSH, write or listener restart" in result.output,
        forbidden_state,
    ) == (0, True, [])
