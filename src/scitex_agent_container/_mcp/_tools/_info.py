"""sac introspection MCP tools (F-CS15) — list-python-apis,
mcp self-introspection, etc."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def register_info_tools(mcp) -> None:
    @mcp.tool()
    def list_python_apis() -> dict[str, Any]:
        """Enumerate sac's public Python API surface. Mirrors
        ``sac list-python-apis --json``."""
        return invoke_cli_json(["list-python-apis", "--json"])

    @mcp.tool()
    def mcp_list_tools() -> dict[str, Any]:
        """Self-introspection: list every MCP tool this server exposes.
        Mirrors ``sac mcp list-tools --json``."""
        return invoke_cli_json(["mcp", "list-tools", "--json"])

    @mcp.tool()
    def mcp_doctor() -> dict[str, Any]:
        """Self-diagnose the MCP install (fastmcp version, tool count,
        registration). Mirrors ``sac mcp doctor``."""
        return invoke_cli_text(["mcp", "doctor"])


__all__ = ["register_info_tools"]
