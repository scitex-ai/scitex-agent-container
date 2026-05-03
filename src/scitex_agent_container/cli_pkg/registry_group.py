"""``sac registry`` noun-group — registry maintenance verbs."""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .lifecycle_cmds import cleanup as _cleanup_impl
from .priority_cmds import singleton_reconcile as _reconcile_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="registry", cls=HelpRecursiveGroup)
def registry_group() -> None:
    """Registry maintenance: clean stale entries, reconcile singletons."""


registry_group.add_command(_rebind(_cleanup_impl, "clean"))
registry_group.add_command(_rebind(_reconcile_impl, "reconcile"))


__all__ = ["registry_group"]
