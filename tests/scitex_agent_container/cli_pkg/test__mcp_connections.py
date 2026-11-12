"""Read-only connection checks remain distinct from the boot recovery hook."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main


@pytest.mark.parametrize("prefix,suffix", [(["--json"], []), ([], ["--json"])])
def test_disabled_connection_check_is_structured_and_unverified(prefix, suffix):
    # Arrange
    args = [*prefix, "mcp", "validate-connections", *suffix]
    runner = CliRunner(env={"SAC_MCP_HEALTHCHECK_DISABLED": "1"})

    # Act
    result = runner.invoke(main, args)

    # Assert
    assert (result.exit_code, json.loads(result.stdout)) == (
        1,
        {"statuses": {}, "failed": [], "healed": False, "action": "disabled"},
    )


def test_disabled_connection_text_does_not_report_healthy():
    # Arrange
    args = ["mcp", "validate-connections"]
    runner = CliRunner(env={"SAC_MCP_HEALTHCHECK_DISABLED": "1"})

    # Act
    result = runner.invoke(main, args)

    # Assert
    assert (result.exit_code, "action=disabled" in result.stdout) == (1, True)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"action": "ok", "statuses": {"server": "connected"}}, 0),
        ({"action": "ok", "statuses": {}}, 1),
        ({"action": "ok", "statuses": {"server": "unknown"}}, 1),
        ({"action": "ok", "statuses": {"server": "failed"}}, 1),
        ({"action": "ok", "statuses": {"server": "connected"}, "failed": ["peer"]}, 1),
        ({"action": "ok", "statuses": {"server": "connected"}, "unknown": ["peer"]}, 1),
        ({"action": "unknown"}, 1),
        ({"action": "disabled"}, 1),
        ({"action": "error"}, 1),
        ({"action": "restart-requested"}, 1),
    ],
)
def test_connection_exit_status_requires_verified_result(result, expected):
    # Arrange
    from scitex_agent_container.cli_pkg._mcp_connections import connection_exit_code

    # Act
    code = connection_exit_code(result)

    # Assert
    assert code == expected
