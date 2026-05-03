"""``sac agent`` noun-group — query/inspect operations on registered agents.

Five sibling verbs (list / status / logs / inspect / snapshot) — tree
form per CLI grammar. Old top-level names (``list-agents`` /
``show-status`` / ``show-logs`` / ``inspect`` / ``take-snapshot``) remain
as deprecation aliases.
"""

from __future__ import annotations

import click

from .info_cmds import logs as _agent_logs_impl
from .snapshot_cmds import snapshot as _agent_snapshot_impl
from .status_cmds import (
    check_agent as _agent_inspect_impl,
)
from .status_cmds import (
    list_agents as _agent_list_impl,
)
from .status_cmds import (
    status as _agent_status_impl,
)


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="agent")
def agent_group() -> None:
    """Query / inspect registered agents (list / status / logs / ...)."""


agent_group.add_command(_rebind(_agent_list_impl, "list"))
agent_group.add_command(_rebind(_agent_status_impl, "status"))
agent_group.add_command(_rebind(_agent_logs_impl, "logs"))
agent_group.add_command(_rebind(_agent_inspect_impl, "inspect"))
agent_group.add_command(_rebind(_agent_snapshot_impl, "snapshot"))


__all__ = ["agent_group"]
