"""``sac image`` noun-group — container image operations."""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .build_cmds import build as _build_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="image", cls=HelpRecursiveGroup)
def image_group() -> None:
    """Container image operations: build the runtime base image."""


image_group.add_command(_rebind(_build_impl, "build"))


__all__ = ["image_group"]
