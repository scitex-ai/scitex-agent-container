"""The boot recovery action and its legacy hook keep their real protocol."""

import json
import re

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main


_DISABLED = {"statuses": {}, "failed": [], "healed": False, "action": "disabled"}


def _disabled_result(command, prefix, suffix, tmp_path):
    runner = CliRunner(
        env={"SAC_MCP_HEALTHCHECK_DISABLED": "1", "XDG_RUNTIME_DIR": str(tmp_path)}
    )
    return runner.invoke(main, [*prefix, "mcp", command, *suffix])


@pytest.mark.parametrize("prefix,suffix", [(["--json"], []), ([], ["--json"])])
def test_disabled_recovery_keeps_fail_open_json(prefix, suffix, tmp_path):
    # Arrange
    command = "recover-connections"

    # Act
    result = _disabled_result(command, prefix, suffix, tmp_path)

    # Assert
    assert (result.exit_code, json.loads(result.stdout), "deprecated" in result.stderr) == (
        0, _DISABLED, False,
    )


@pytest.mark.parametrize("prefix,suffix", [(["--json"], []), ([], ["--json"])])
def test_legacy_recovery_forwards_json_and_warns(prefix, suffix, tmp_path):
    # Arrange
    command = "healthcheck"

    # Act
    result = _disabled_result(command, prefix, suffix, tmp_path)

    # Assert
    assert (
        result.exit_code, json.loads(result.stdout), "deprecated" in result.stderr,
        "recover-connections" in result.stderr, "0.40" in result.stderr,
    ) == (0, _DISABLED, True, True, True)


def test_recovery_action_visible_and_legacy_hidden():
    # Arrange
    runner = CliRunner()

    # Act
    result = runner.invoke(main, ["mcp", "--help"])

    # Assert
    assert (
        result.exit_code,
        bool(re.search(r"^\s+recover-connections\s", result.stdout, re.M)),
        bool(re.search(r"^\s+healthcheck\s", result.stdout, re.M)),
    ) == (0, True, False)


@pytest.mark.parametrize("command", ["recover-connections", "healthcheck"])
def test_recovery_help_uses_canonical_parser(command, tmp_path):
    # Arrange
    runner = CliRunner(env={"XDG_RUNTIME_DIR": str(tmp_path)})

    # Act
    result = runner.invoke(main, ["mcp", command, "--help"])

    # Assert
    assert (
        result.exit_code, "--json" in result.stdout, "FAIL-OPEN" in result.stdout,
        "SAC_MCP_HEALTHCHECK_NO_RESTART" in result.stdout,
    ) == (0, True, True, True)


@pytest.mark.parametrize("command", ["recover-connections", "healthcheck"])
def test_recovery_unknown_options_are_refused(command, tmp_path):
    # Arrange
    runner = CliRunner(
        env={"SAC_MCP_HEALTHCHECK_DISABLED": "1", "XDG_RUNTIME_DIR": str(tmp_path)}
    )

    # Act
    result = runner.invoke(main, ["mcp", command, "--not-an-option"])

    # Assert
    assert (
        result.exit_code, "No such option" in result.stderr,
        '"action":"disabled"' not in result.stdout,
    ) == (2, True, True)
