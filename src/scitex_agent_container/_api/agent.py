"""``sac.agent`` — selected agent verbs as noun-grouped public Python calls.

Each verb is the same function object registered for MCP, with no duplicate
implementation. The package root exposes this noun module rather than flat
verb names. Shared-Registry lifecycle helpers remain in ``_lifecycle``.
"""

from .._mcp._tools._agent import (
    agent_check as check,
)
from .._mcp._tools._agent import (
    agent_create as create,
)
from .._mcp._tools._agent import (
    agent_find as find,
)
from .._mcp._tools._agent import (
    agent_health as health,
)
from .._mcp._tools._agent import (
    agent_list as list,
)
from .._mcp._tools._agent import (
    agent_logs as logs,
)
from .._mcp._tools._agent import (
    agent_recall as recall,
)
from .._mcp._tools._agent import (
    agent_restart as restart,
)
from .._mcp._tools._agent import (
    agent_send as send,
)
from .._mcp._tools._agent import (
    agent_start as start,
)
from .._mcp._tools._agent import (
    agent_status as status,
)
from .._mcp._tools._agent import (
    agent_stop as stop,
)

__all__ = [
    "create",
    "list",
    "status",
    "logs",
    "health",
    "find",
    "check",
    "recall",
    "start",
    "stop",
    "restart",
    "send",
]
