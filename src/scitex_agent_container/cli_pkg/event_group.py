"""``sac event`` noun-group — event log operations."""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .hook_cmds import hook_event as _ingest_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="event", cls=HelpRecursiveGroup)
def event_group() -> None:
    """Event log operations: ingest hook events into the per-agent ring buffer."""


event_group.add_command(_rebind(_ingest_impl, "ingest"))


__all__ = ["event_group"]
