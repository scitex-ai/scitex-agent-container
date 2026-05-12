"""``sac agent`` noun-group — most agent-scoped operations.

Verbs are taken from existing flat commands and re-registered under
``agent``. The old top-level names are preserved as deprecation aliases
(see ``_main.py``).

``take-snapshot`` and ``check-priority`` are folded into ``status`` as
``--snapshot`` / ``--priority`` flags. ``validate`` is folded into
``check`` (which now runs YAML validation before runtime probes).
"""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .build_cmds import check as _check_impl
from .info_cmds import find as _find_impl
from .info_cmds import tail_session as _tail_impl
from .lifecycle_cmds import restart as _restart_impl
from .lifecycle_cmds import start as _start_impl
from .lifecycle_cmds import stop as _stop_impl
from .recall_cmds import recall as _recall_impl
from .status_cmds import health as _health_impl
from .status_cmds import status as _status_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


class _AgentGroup(HelpRecursiveGroup):
    """Render ``sac agent --help`` with grouped sections instead of one
    flat alphabetical list. Categories follow the verbs' actual purpose:
    *Lifecycle* mutates state; *Inspect* reads it; *Preflight* validates
    before launch; *Discovery* finds peers."""

    COMMAND_CATEGORIES = [
        ("Lifecycle", ["start", "stop", "restart"]),
        ("Inspect", ["status", "health", "tail", "recall"]),
        ("Preflight", ["check"]),
        ("Discovery", ["find"]),
    ]


@click.group(name="agent", cls=_AgentGroup)
def agent_group() -> None:
    """Agent lifecycle, status, introspection, and snapshots."""


# Lifecycle verbs
agent_group.add_command(_rebind(_start_impl, "start"))
agent_group.add_command(_rebind(_stop_impl, "stop"))
agent_group.add_command(_rebind(_restart_impl, "restart"))

# Polysemous noun-leaves (allowed under noun groups by §1 loosening)
agent_group.add_command(_rebind(_status_impl, "status"))
agent_group.add_command(_rebind(_tail_impl, "tail"))
agent_group.add_command(_rebind(_health_impl, "health"))

# Verb leaves
agent_group.add_command(_rebind(_find_impl, "find"))
agent_group.add_command(_rebind(_recall_impl, "recall"))
agent_group.add_command(_rebind(_check_impl, "check"))


__all__ = ["agent_group"]
