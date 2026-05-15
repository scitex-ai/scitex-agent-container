"""Tool registration for the sac MCP server.

Single entry point :func:`register_all_tools`. Each leaf module
exposes one ``register_<group>_tools(mcp)`` function that registers
its tools on the given FastMCP server.

Tool naming follows the scitex convention: ``sac_<verb>_<noun>``,
mirroring ``sac <noun> <verb>`` on the CLI. Hyphens in CLI verbs
become underscores (e.g. ``sac agent list-snapshots`` →
``sac_agent_list_snapshots``).
"""

from __future__ import annotations

from ._account import register_account_tools
from ._agent import register_agent_tools
from ._db import register_db_tools
from ._host import register_host_tools
from ._image import register_image_tools
from ._info import register_info_tools
from ._skills import register_skills_tools
from ._template import register_template_tools


def register_all_tools(mcp) -> None:
    """Register every sac MCP tool on ``mcp``."""
    register_agent_tools(mcp)
    register_db_tools(mcp)
    register_host_tools(mcp)
    register_image_tools(mcp)
    register_account_tools(mcp)
    register_skills_tools(mcp)
    register_template_tools(mcp)
    register_info_tools(mcp)


__all__ = ["register_all_tools"]
