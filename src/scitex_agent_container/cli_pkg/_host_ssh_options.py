"""Inspect the shared SSH option renderer without opening a connection."""

from __future__ import annotations

import json
from copy import copy

import click

from .._state.host_config import ssh_control_options, ssh_control_options_str
from ._helpers import _json_flag

__all__ = ["host_ssh_opts", "register_ssh_options_command"]


@click.command("show-ssh-options")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit SSH option words as a JSON array under options.",
)
@click.pass_context
def host_ssh_opts(ctx: click.Context, as_json: bool) -> None:
    """Print the shared SSH ControlMaster options.

    JSON supplies option words for an argv list, preserving paths with spaces.
    The human output remains shell-quoted: parse it with shlex.split rather
    than plain shell command substitution, which does not reapply quotes.

    This uses the existing renderer, including control-directory creation,
    opt-out and unavailable-directory fallback. It does not test a connection.

    \b
    Examples:
      sac host show-ssh-options
      sac host show-ssh-options --json
      sac --json host show-ssh-options
    """
    if _json_flag(ctx, as_json):
        click.echo(json.dumps({"options": ssh_control_options()}, indent=2))
        return
    click.echo(ssh_control_options_str())


def register_ssh_options_command(group: click.Group) -> None:
    """Attach option inspection and its hidden legacy spelling to the group."""
    group.add_command(host_ssh_opts)
    legacy = copy(host_ssh_opts)
    legacy.name = "ssh-opts"
    legacy.hidden = True
    legacy.deprecated = "Use host show-ssh-options instead."
    group.add_command(legacy)
