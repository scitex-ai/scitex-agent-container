"""``sac auto-accept`` noun-group — Claude Code auto-accept TUI handler.

Three sibling verbs (send / start / stop) — tree form per CLI grammar.
Old top-level names (``send-accept`` / ``start-auto-accept`` /
``stop-auto-accept``) remain as deprecation aliases.
"""

from __future__ import annotations

import click

from .lifecycle_cmds import (
    send_accept as _send_accept_impl,
)
from .lifecycle_cmds import (
    start_auto_accept as _start_auto_accept_impl,
)
from .lifecycle_cmds import (
    stop_auto_accept as _stop_auto_accept_impl,
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


@click.group(name="auto-accept")
def auto_accept_group() -> None:
    """Auto-accept TUI handler for Claude Code permission prompts."""


auto_accept_group.add_command(_rebind(_send_accept_impl, "send"))
auto_accept_group.add_command(_rebind(_start_auto_accept_impl, "start"))
auto_accept_group.add_command(_rebind(_stop_auto_accept_impl, "stop"))


__all__ = ["auto_accept_group"]
