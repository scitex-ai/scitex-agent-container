"""Click entry point for scitex-agent-container.

This module wires every subcommand defined in the sibling modules
into a single click group, registered as the ``scitex-agent-container``
console script (see pyproject.toml).
"""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup
from .account_cmds import account, quota_watch
from .action_cmds import actions_cli
from .build_cmds import build, check, validate
from .hook_cmds import hook_event
from .info_cmds import attach, find, list_python_apis, logs
from .lifecycle_cmds import cleanup, restart, start, stop
from .probe_cmds import probe_network
from .render_cmds import render_attach, render_sbatch
from .snapshot_cmds import snapshot
from .status_cmds import check_agent, health, list_agents, status


@click.group(cls=HelpRecursiveGroup, invoke_without_command=True)
@click.version_option(package_name="scitex-agent-container")
@click.option(
    "--help-recursive",
    is_flag=True,
    default=False,
    help="Show help for all commands recursively.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as structured JSON (propagates to subcommands).",
)
@click.pass_context
def main(ctx: click.Context, help_recursive: bool, as_json: bool) -> None:
    """SciTeX Agent Container -- Declarative agent management."""
    ctx.ensure_object(dict)
    if as_json:
        ctx.obj["json"] = True
    if help_recursive:
        click.echo(ctx.command.get_help_recursive(ctx))  # type: ignore[attr-defined]
        ctx.exit(0)
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# Lifecycle
main.add_command(start)
main.add_command(stop)
main.add_command(restart)
main.add_command(cleanup)

# Status / listing
main.add_command(status)
main.add_command(list_agents)  # registered as 'list'
main.add_command(health)
main.add_command(check_agent)  # registered as 'inspect'
main.add_command(snapshot)

# Info / introspection
main.add_command(find)
main.add_command(logs)
main.add_command(attach)
main.add_command(list_python_apis)  # registered as 'list-python-apis'

# Build / validation
main.add_command(check)
main.add_command(validate)
main.add_command(build)

# Account management and quota monitoring
main.add_command(account)
main.add_command(quota_watch)

# Claude Code hook event ingestor
main.add_command(hook_event)

# Action subsystem: run PaneActions, query attempts, aggregate stats.
main.add_command(actions_cli)

# Render ports: emit sbatch/attach text for external consumers.
main.add_command(render_sbatch)
main.add_command(render_attach)

# Connectivity probe (todo#457): fleet-facing WSL ↔ hub liveness.
main.add_command(probe_network)


if __name__ == "__main__":
    main()
