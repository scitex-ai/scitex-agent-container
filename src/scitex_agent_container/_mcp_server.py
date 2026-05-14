"""Compatibility shim — re-exports the FastMCP server.

The ``scitex-dev ecosystem audit-mcp-tools`` linter looks for
``<pkg>/_mcp_server.mcp`` to discover the package's MCP server. The
real implementation lives at :mod:`._mcp.server` (matches the
scitex-dataset layout); this module is a thin re-export so the
auditor's hard-coded path works unchanged.

Usage from external code should still prefer::

    from scitex_agent_container._mcp import mcp, run_server
"""

from __future__ import annotations

from ._mcp.server import get_server, run_server


def __getattr__(name: str):
    """Lazy attribute access — ``mcp`` is built on first read so a
    bare ``import scitex_agent_container._mcp_server`` doesn't require
    fastmcp to be installed."""
    if name == "mcp":
        return get_server()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["get_server", "mcp", "run_server"]
