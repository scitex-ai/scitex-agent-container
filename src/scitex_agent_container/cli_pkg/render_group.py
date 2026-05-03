"""``sac render`` noun-group — emit runtime-specific artifacts to stdout.

Three sibling verbs (sbatch / attach / contributor-spec) — tree form
chosen per ``general/03_interface_02_cli/02_subcommand-structure-noun-verb.md``
since the noun has 3+ verbs.

Old top-level names (``render-sbatch`` / ``render-attach`` /
``render-contributor-spec``) remain registered as deprecation aliases that
print a warning to stderr and dispatch to the new verbs. Aliases will be
removed one release after the rename ships.
"""

from __future__ import annotations

import click

from .contributor_spec_cmds import contributor_spec as _contributor_spec_impl
from .render_cmds import render_attach as _render_attach_impl
from .render_cmds import render_sbatch as _render_sbatch_impl


@click.group(name="render")
def render_group() -> None:
    """Emit runtime-specific artifacts (sbatch / attach / contributor-spec)."""


# Re-register the existing command callbacks under shorter verb names
# inside the group. Click commands carry their callback + params, so we
# can rebuild a fresh command bound to the same callback with a new name.
def _rebind(cmd: click.Command, new_name: str) -> click.Command:
    cmd2 = click.Command(
        name=new_name,
        callback=cmd.callback,
        params=list(cmd.params),
        help=cmd.help,
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )
    return cmd2


render_group.add_command(_rebind(_render_sbatch_impl, "sbatch"))
render_group.add_command(_rebind(_render_attach_impl, "attach"))
render_group.add_command(_rebind(_contributor_spec_impl, "contributor-spec"))


__all__ = ["render_group"]
