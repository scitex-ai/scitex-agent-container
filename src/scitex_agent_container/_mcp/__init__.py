"""scitex-agent-container MCP server.

Single FastMCP instance at :data:`server.mcp`; tool definitions live
under :mod:`._tools`. Convention follows :mod:`scitex_dataset._mcp`.

Public surface:

    >>> from scitex_agent_container._mcp import mcp, run_server
    >>> run_server(transport="stdio")
"""

from .server import mcp, run_server

__all__ = ["mcp", "run_server"]
