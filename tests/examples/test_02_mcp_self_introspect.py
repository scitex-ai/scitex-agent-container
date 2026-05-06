"""Smoke test for examples/02_mcp_self_introspect.py.

Imports the example and verifies the MCP introspection helpers it
depends on still exist. Doesn't actually start the FastMCP server
(that would bind a port); the API-stability check is what matters
for catching ecosystem drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "02_mcp_self_introspect.py"


def test_example_file_parses() -> None:
    ast.parse(EXAMPLE.read_text())


def test_dependent_apis_present() -> None:
    from scitex_agent_container._mcp.server import get_server
    from scitex_agent_container.cli_pkg.mcp_cmds import _list_tools

    assert callable(get_server)
    assert callable(_list_tools)
