"""sac introspection tools (F-CS15) — Python API + MCP wrappers.

``list_python_apis`` / ``mcp_list_tools`` / ``mcp_doctor`` reflect the
package back at the caller — useful for an LLM agent that wants to
ask "what surfaces does sac expose?" before driving anything.
"""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def list_python_apis() -> dict[str, Any]:
    """Enumerate sac's public Python API surface. Mirrors
    ``sac list-python-apis --json``."""
    return invoke_cli_json(["list-python-apis", "--json"])


def mcp_list_tools() -> dict[str, Any]:
    """Self-introspection: list every MCP tool this server exposes.
    Mirrors ``sac mcp list-tools --json``."""
    return invoke_cli_json(["mcp", "list-tools", "--json"])


def mcp_doctor() -> dict[str, Any]:
    """Self-diagnose the MCP install (fastmcp version, tool count,
    registration). Mirrors ``sac mcp doctor``."""
    return invoke_cli_text(["mcp", "doctor"])


def register_info_tools(mcp) -> None:
    for fn in (list_python_apis, mcp_list_tools, mcp_doctor):
        mcp.tool()(fn)


__all__ = ["list_python_apis", "mcp_list_tools", "mcp_doctor", "register_info_tools"]
