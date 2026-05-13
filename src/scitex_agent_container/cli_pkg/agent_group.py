"""``sac agents`` noun-group — every agent-scoped operation.

Plural form. Renamed from ``sac agent`` so the verb shape lines up
with the list-of-things commands underneath (``start NAME...``,
``stop NAME...``, ``delete NAME...``, ``tail NAME...``).

``accounts`` is nested under here too (``sac agents accounts``) — the
Claude-Code credential store is one of the agent's concerns, not a
top-level namespace of its own.
"""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .account_cmds import account as _account_group
from .build_cmds import check as _check_impl
from .info_cmds import find as _find_impl
from .info_cmds import tail_session as _tail_impl
from .lifecycle_cmds import delete as _delete_impl
from .lifecycle_cmds import restart as _restart_impl
from .lifecycle_cmds import start as _start_impl
from .lifecycle_cmds import stop as _stop_impl
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
        ("Lifecycle", ["start", "stop", "restart", "delete"]),
        ("Interact", ["send"]),
        ("Inspect", ["list", "health", "tail", "recall"]),
        ("Preflight", ["check"]),
        ("Discovery", ["find"]),
        ("Account", ["accounts"]),
    ]


@click.group(name="agents", cls=_AgentsGroup)
def agent_group() -> None:
    """Agent lifecycle, status, introspection, accounts, and snapshots."""


# Lifecycle verbs
agent_group.add_command(_rebind(_start_impl, "start"))
agent_group.add_command(_rebind(_stop_impl, "stop"))
agent_group.add_command(_rebind(_restart_impl, "restart"))
agent_group.add_command(_rebind(_delete_impl, "delete"))

# Polysemous noun-leaves (allowed under noun groups by §1 loosening)
agent_group.add_command(_rebind(_status_impl, "list"))
agent_group.add_command(_rebind(_tail_impl, "tail"))
agent_group.add_command(_rebind(_health_impl, "health"))

# Verb leaves
agent_group.add_command(_rebind(_find_impl, "find"))
agent_group.add_command(_rebind(_recall_impl, "recall"))
agent_group.add_command(_rebind(_check_impl, "check"))
agent_group.add_command(_rebind(_send_impl, "send"))

# Nested noun group — the account store is agent-scoped, not its own
# top-level concern. Original singular `account` cmd object reused
# as-is; the parent group exposes it as `accounts`.
_account_group.name = "accounts"
agent_group.add_command(_account_group)


__all__ = ["agent_group"]
