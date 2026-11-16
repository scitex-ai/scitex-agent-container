"""Targeted descriptions come from the registered MCP tool schema."""

import asyncio
import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main

# These registry/protocol controls require the optional MCP dependency.
Client = pytest.importorskip("fastmcp").Client


def test_known_tool_schema_is_available_as_json():
    # Arrange
    args = ["mcp", "show-tool", "agent_send", "--json"]

    # Act
    result = CliRunner().invoke(main, args)

    # Assert
    assert (result.exit_code, json.loads(result.stdout)["name"]) == (0, "agent_send")


async def _advertised_tool(name):
    from scitex_agent_container._mcp.server import get_server

    async with Client(get_server()) as client:
        tools = await client.list_tools()
    return next(tool for tool in tools if tool.name == name)


@pytest.mark.parametrize("name", ["agent_send", "skills_get"])
@pytest.mark.parametrize("prefix,suffix", [(["--json"], []), ([], ["--json"])])
def test_description_matches_actual_protocol_schema(name, prefix, suffix):
    # Arrange
    tool = asyncio.run(_advertised_tool(name))
    expected = {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.inputSchema,
    }

    # Act
    result = CliRunner().invoke(main, [*prefix, "mcp", "show-tool", name, *suffix])

    # Assert
    assert (result.exit_code, json.loads(result.stdout)) == (0, expected)


@pytest.mark.parametrize("name", ["missing", "sac_agent_send", "agent_send_extra"])
def test_unknown_tool_name_is_not_guessed(name):
    # Arrange
    args = ["mcp", "show-tool", name, "--json"]

    # Act
    result = CliRunner().invoke(main, args)

    # Assert
    assert (result.exit_code, result.stdout, "Unknown MCP tool" in result.stderr) == (
        2,
        "",
        True,
    )


def test_human_description_contains_tool_and_schema():
    # Arrange
    args = ["mcp", "show-tool", "skills_get"]

    # Act
    result = CliRunner().invoke(main, args)

    # Assert
    assert (
        result.exit_code,
        "skills_get:" in result.stdout,
        '"properties"' in result.stdout,
    ) == (
        0,
        True,
        True,
    )
