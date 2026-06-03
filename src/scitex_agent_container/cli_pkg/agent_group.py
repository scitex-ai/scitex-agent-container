"""``sac agents`` noun-group — every agent-scoped operation.

Plural form. Renamed from ``sac agent`` so the verb shape lines up
with the list-of-things commands underneath (``start NAME...``,
``stop NAME...``, ``delete NAME...``, ``tail NAME...``).

``accounts`` lives at the top level (``sac accounts``); the credential
store is fleet-wide, not agent-scoped.
"""

from __future__ import annotations

import click

from ._agent_prune_claude import prune_claude as _prune_claude_impl
from ._helpers import HelpRecursiveGroup
from .build_cmds import check as _check_impl
from .info_cmds import find as _find_impl
from .info_cmds import tail_session as _tail_impl
from .lifecycle import delete as _delete_impl
from .lifecycle import forget as _forget_impl
from .lifecycle import restart as _restart_impl
from .lifecycle import start as _start_impl
from .lifecycle import stop as _stop_impl
from .recall_cmds import recall as _recall_impl
from .send_cmds import send as _send_impl
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


class _AgentsGroup(HelpRecursiveGroup):
    """Render ``sac agents --help`` with grouped sections instead of one
    flat alphabetical list."""

    COMMAND_CATEGORIES = [
        ("Lifecycle", ["start", "stop", "restart", "delete", "forget"]),
        ("Interact", ["send"]),
        ("Inspect", ["list", "status", "health", "tail", "recall"]),
        ("Preflight", ["check"]),
        ("Discovery", ["find"]),
        ("Account", ["accounts"]),
        ("Maintenance", ["prune-claude"]),
    ]


@click.group(name="agents", cls=_AgentsGroup)
def agent_group() -> None:
    """Agent lifecycle, status, introspection, and snapshots."""


# Lifecycle verbs
agent_group.add_command(_rebind(_start_impl, "start"))
agent_group.add_command(_rebind(_stop_impl, "stop"))
agent_group.add_command(_rebind(_restart_impl, "restart"))
agent_group.add_command(_rebind(_delete_impl, "delete"))
agent_group.add_command(_rebind(_forget_impl, "forget"))

# Polysemous noun-leaves (allowed under noun groups by §1 loosening)
agent_group.add_command(_rebind(_status_impl, "list"))
# `status` is a muscle-memory alias for `list` — the top-level CLI
# help text + the README example call out `sac agents status`, and
# operators expect it to exist (foundation-polish bug 2).
agent_group.add_command(_rebind(_status_impl, "status"))
agent_group.add_command(_rebind(_tail_impl, "tail"))
agent_group.add_command(_rebind(_health_impl, "health"))

# Verb leaves
agent_group.add_command(_rebind(_find_impl, "find"))
agent_group.add_command(_rebind(_recall_impl, "recall"))
agent_group.add_command(_rebind(_check_impl, "check"))
agent_group.add_command(_rebind(_send_impl, "send"))
# F-CS8 prune — dry-run-by-default purge of the two known workdir
# bloat sources (.pending/ records + merged-only worktrees/agent-*).
agent_group.add_command(_rebind(_prune_claude_impl, "prune-claude"))

__all__ = ["agent_group"]
