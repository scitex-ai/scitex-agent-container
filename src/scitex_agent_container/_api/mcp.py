"""``sac.mcp`` — MCP self-introspection verbs as bare names."""

from .._mcp._tools._info import (
    mcp_doctor as doctor,
)
from .._mcp._tools._info import (
    mcp_list_tools as list_tools,
)

__all__ = ["list_tools", "doctor"]
