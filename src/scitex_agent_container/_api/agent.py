"""``sac.agent`` — agent-lifecycle verbs as bare names.

Same function objects as the flat top-level (`sac.agent_list` etc.);
this module is the noun-grouped re-export shim for ergonomic
access (`sac.agent.list()` reads like the CLI tree).
"""

from .._mcp._tools._agent import (
    agent_attach as attach,
)
from .._mcp._tools._agent import (
    agent_check as check,
)
from .._mcp._tools._agent import (
    agent_check_priority as check_priority,
)
from .._mcp._tools._agent import (
    agent_find as find,
)
from .._mcp._tools._agent import (
    agent_health as health,
)
from .._mcp._tools._agent import (
    agent_inspect as inspect,
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
from .._mcp._tools._agent import (
    agent_take_snapshot as take_snapshot,
)
from .._mcp._tools._agent import (
    agent_validate as validate,
)

__all__ = [
    "list",
    "status",
    "logs",
    "health",
    "find",
    "check",
    "validate",
    "inspect",
    "recall",
    "check_priority",
    "take_snapshot",
    "attach",
    "start",
    "stop",
    "restart",
    "send",
]
