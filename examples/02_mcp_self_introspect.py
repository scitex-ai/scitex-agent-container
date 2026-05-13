#!/usr/bin/env python3
"""Spin sac's MCP server in-process and enumerate every registered tool.

Equivalent to:

    sac mcp list-tools --json

…but useful for embedding in a CI gate ("does the MCP surface still
include the tool I rely on?") or feeding an LLM agent a discovery
table at startup.
"""

from __future__ import annotations

import scitex as stx


@stx.session
def main(logger=stx.INJECTED):
    """List every MCP tool the sac server exposes."""
    from scitex_agent_container._mcp.server import get_server
    from scitex_agent_container.cli_pkg.mcp_group import _list_tools

    server = get_server()
    tools = _list_tools(server)
    payload = {"server": server.name, "count": len(tools), "tools": tools}
    logger.info(f"{payload['server']}: {payload['count']} tools")
    stx.io.save(payload, "mcp_tools.json")
    return 0


if __name__ == "__main__":
    main()
