"""``sac template`` noun-group — render templates.

Replaces the flat ``render-sbatch`` / ``render-attach`` /
``render-contributor-spec`` compounds. Verb-noun-compound leaves keep
their head verb so they remain valid §1 leaves under a noun group.
"""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .contributor_spec_cmds import contributor_spec as _contrib_impl
from .render_cmds import render_attach as _render_attach_impl
from .render_cmds import render_sbatch as _render_sbatch_impl


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
    """Render text templates (sbatch wrapper, attach command, contributor spec)."""


template_group.add_command(_rebind(_render_sbatch_impl, "render-sbatch"))
template_group.add_command(_rebind(_render_attach_impl, "render-attach"))
template_group.add_command(_rebind(_contrib_impl, "render-contributor-spec"))


__all__ = ["template_group"]
