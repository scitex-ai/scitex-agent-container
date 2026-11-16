"""Tool registration for the sac MCP server.

Single entry point :func:`register_all_tools`. Each leaf module
exposes one ``register_<group>_tools(mcp)`` function that registers
its tools on the given FastMCP server.

Standalone tools use bare noun/action names such as ``agent_list`` and
``agent_send``, without a ``sac_`` prefix. Discover actual names and input
schemas through MCP ``tools/list``; CLI and MCP capabilities/options differ.
"""

from __future__ import annotations

from ._account import register_account_tools
from ._agent import register_agent_tools
from ._host import register_host_tools
from ._image import register_image_tools
from ._info import register_info_tools
from ._skills import register_skills_tools
from ._store import register_store_tools
from ._subagent import register_subagent_tools
from ._template import register_template_tools


def register_all_tools(mcp) -> None:
    """Register every sac MCP tool on ``mcp``."""
    register_agent_tools(mcp)
    register_store_tools(mcp)
    register_host_tools(mcp)
    register_image_tools(mcp)
    register_account_tools(mcp)
    register_skills_tools(mcp)
    register_template_tools(mcp)
    register_info_tools(mcp)
    register_subagent_tools(mcp)


__all__ = ["register_all_tools"]
