"""Tests for the sac MCP server (F-CS15)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

# fastmcp may be absent on a minimal install — gate every test on it.
fastmcp = pytest.importorskip("fastmcp")

from scitex_agent_container._mcp.server import get_server  # noqa: E402
from scitex_agent_container.cli_pkg.mcp_cmds import (  # noqa: E402
    mcp as mcp_cli_group,
)


def _tool_names(server) -> list[str]:
    """Async-safe enumeration via the same helper the CLI uses."""
    from scitex_agent_container.cli_pkg.mcp_cmds import _list_tool_names

    return _list_tool_names(server)


def test_server_constructs_with_expected_name():
    s = get_server()
    assert s.name == "scitex-agent-container"


def test_every_tool_has_sac_prefix():
    """Per scitex MCP convention §2: every tool name is `<pkg>_<verb>_<noun>`."""
    names = _tool_names(get_server())
    assert names, "MCP server registered no tools"
    bad = [n for n in names if not n.startswith("sac_")]
    assert bad == [], f"tools missing sac_ prefix: {bad}"


def test_expected_noun_groups_present():
    """Spot-check the noun groups F-CS15 must mirror from the CLI."""
    names = set(_tool_names(get_server()))
    must_have = {
        "sac_agent_list",
        "sac_agent_status",
        "sac_agent_start",
        "sac_agent_stop",
        "sac_db_show",
        "sac_db_query",
        "sac_host_show",
        "sac_host_list",
        "sac_image_build",
        "sac_template_render_contributor_spec",
        "sac_skills_list",
        "sac_skills_get",
        "sac_mcp_list_tools",
        "sac_mcp_doctor",
    }
    missing = must_have - names
    assert not missing, f"missing required tools: {missing}"


def test_cli_doctor_succeeds():
    runner = CliRunner()
    result = runner.invoke(mcp_cli_group, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "fastmcp" in result.output
    assert "sac MCP server" in result.output


def test_cli_list_tools_json_shape():
    runner = CliRunner()
    result = runner.invoke(mcp_cli_group, ["list-tools", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] >= 1
    assert all("name" in t for t in payload["tools"])
    assert all(t["name"].startswith("sac_") for t in payload["tools"])


def test_cli_install_claude_code_format():
    runner = CliRunner()
    result = runner.invoke(mcp_cli_group, ["install", "--claude-code"])
    assert result.exit_code == 0, result.output
    assert '"scitex-agent-container"' in result.output
    assert '"command": "sac"' in result.output
    assert '"args": ["mcp", "start"]' in result.output


def test_cli_start_dry_run_default_transport():
    runner = CliRunner()
    result = runner.invoke(mcp_cli_group, ["start", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "transport=stdio" in result.output


def test_cli_start_dry_run_http_transport():
    runner = CliRunner()
    result = runner.invoke(
        mcp_cli_group, ["start", "--http", "--port", "9999", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "transport=http" in result.output
    assert "9999" in result.output


def test_skills_list_returns_known_skills():
    """sac_skills_list reads from _skills/scitex-agent-container/."""
    from scitex_agent_container._mcp._tools._skills import register_skills_tools

    captured: dict = {}

    class _Capture:
        def tool(self):
            def _decorate(fn):
                captured[fn.__name__] = fn
                return fn

            return _decorate

    register_skills_tools(_Capture())
    result = captured["sac_skills_list"]()
    assert result["count"] >= 1
    assert all("name" in s for s in result["skills"])


def test_skills_get_returns_404_for_unknown():
    from scitex_agent_container._mcp._tools._skills import register_skills_tools

    captured: dict = {}

    class _Capture:
        def tool(self):
            def _decorate(fn):
                captured[fn.__name__] = fn
                return fn

            return _decorate

    register_skills_tools(_Capture())
    result = captured["sac_skills_get"](name="definitely-not-a-real-skill-xxxx")
    assert "error" in result
    assert "available" in result
