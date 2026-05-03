"""``sac quota`` noun-group — quota tracking."""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .account_cmds import quota_watch as _watch_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="quota", cls=HelpRecursiveGroup)
def quota_group() -> None:
    """Quota tracking and auto-rotation."""


quota_group.add_command(_rebind(_watch_impl, "watch"))


__all__ = ["quota_group"]
