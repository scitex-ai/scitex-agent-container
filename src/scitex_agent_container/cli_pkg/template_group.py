"""``sac template`` noun-group — render templates."""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .contributor_spec_cmds import contributor_spec as _contrib_impl


def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    return click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


@click.group(name="template", cls=HelpRecursiveGroup)
def template_group() -> None:
    """Render text templates (contributor spec)."""


template_group.add_command(_rebind(_contrib_impl, "render-contributor-spec"))


__all__ = ["template_group"]
