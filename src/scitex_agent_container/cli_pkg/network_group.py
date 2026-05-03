"""``sac network`` noun-group — network operations."""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .probe_cmds import probe_network as _probe_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="network", cls=HelpRecursiveGroup)
def network_group() -> None:
    """Network operations: liveness probes, fleet connectivity."""


network_group.add_command(_rebind(_probe_impl, "probe"))


__all__ = ["network_group"]
