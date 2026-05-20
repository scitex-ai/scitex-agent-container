"""``sac.agent`` — agent-lifecycle verbs as bare names.

Same function objects as the flat top-level (`sac.agent_list` etc.);
this module is the noun-grouped re-export shim for ergonomic
access (`sac.agent.list()` reads like the CLI tree).

The set mirrors the live ``sac agents`` CLI subcommands. The
``validate`` / ``inspect`` / ``check-priority`` / ``take-snapshot`` /
``attach`` leaves were removed in the ``agent`` → ``agents`` group
rename, so the corresponding re-exports are gone too.
"""

from .._mcp._tools._agent import (
    agent_check as check,
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
