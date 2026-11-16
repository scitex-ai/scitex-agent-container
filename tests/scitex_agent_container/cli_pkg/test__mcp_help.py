"""MCP discovery help uses the shared format under every public CLI mount."""

from importlib.metadata import version

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main


@pytest.mark.parametrize(
    "prog", ["sac", "scitex-agent-container", "scitex agent-container"]
)
@pytest.mark.parametrize(
    "command,example,meaning",
    [
        (
            "validate-connections",
            "mcp validate-connections --json",
            "Unknown, disabled or failed connections.",
        ),
        (
            "show-tool",
            "mcp show-tool agent_send --json",
            "Unknown tool name or invalid command usage.",
        ),
    ],
)
def test_discovery_help_renders_version_mount_and_exit_codes(
    prog, command, example, meaning
):
    # Arrange
    prefix = f"scitex-agent-container (v{version('scitex-agent-container')})"

    # Act
    result = CliRunner().invoke(main, ["mcp", command, "--help"], prog_name=prog)

    # Assert
    assert (
        result.exit_code,
        result.stdout.startswith(prefix),
        f"$ {prog} {example}" in result.stdout,
        "Exit codes:" in result.stdout,
        meaning in result.stdout,
    ) == (0, True, True, True, True)
