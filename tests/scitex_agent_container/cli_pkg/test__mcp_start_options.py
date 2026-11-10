"""HTTP launch previews validate port bounds without loading the MCP backend."""

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.mcp_group import mcp


@pytest.mark.parametrize("port", ["-1", "65536"])
@pytest.mark.parametrize("approval", [[], ["--yes"]])
def test_invalid_http_port_refuses_launch_preview(port, approval):
    # Arrange
    args = ["start", "--http", "--port", port, "--dry-run", *approval]

    # Act
    result = CliRunner().invoke(mcp, args)

    # Assert
    assert (
        result.exit_code,
        result.stdout,
        "between 0 and 65535" in result.stderr,
    ) == (
        2,
        "",
        True,
    )


@pytest.mark.parametrize("port", ["0", "1", "65535"])
def test_valid_http_port_keeps_launch_preview(port):
    # Arrange
    args = ["start", "--http", "--port", port, "--dry-run"]

    # Act
    result = CliRunner().invoke(mcp, args)

    # Assert
    assert (result.exit_code, f"port={port}" in result.stdout) == (0, True)


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_stdio_preview_keeps_ignoring_http_port(port):
    # Arrange
    args = ["start", "--port", port, "--dry-run"]

    # Act
    result = CliRunner().invoke(mcp, args)

    # Assert
    assert (result.exit_code, "transport=stdio" in result.stdout) == (0, True)
