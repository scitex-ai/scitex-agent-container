"""Click entry point for scitex-agent-container.

This module wires every subcommand defined in the sibling modules
into a single click group, registered as the ``scitex-agent-container``
console script (see pyproject.toml).
"""

from __future__ import annotations

import click

from ._helpers import HelpRecursiveGroup, deprecated_alias
from .account_cmds import account, quota_watch
from .action_cmds import actions_cli
from .build_cmds import build, check, validate
from .contributor_spec_cmds import contributor_spec
from .hook_cmds import hook_event
from .info_cmds import attach, find, list_python_apis, logs
from .install_cmds import install_group, install_post_merge_cron
from .lifecycle_cmds import (
    cleanup,
    restart,
    send_accept,
    start,
    start_auto_accept,
    stop,
    stop_auto_accept,
)
from .priority_cmds import priority_check, singleton_reconcile
from .probe_cmds import probe_network
from .recall_cmds import recall
from .render_cmds import render_attach, render_sbatch
from .render_group import render_group
from .snapshot_cmds import snapshot
from .status_cmds import check_agent, health, list_agents, status


@click.group(
    cls=HelpRecursiveGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(
    None,
    "-V",
    "--version",
    package_name="scitex-agent-container",
    prog_name="scitex-agent-container",
)
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
    """SciTeX Agent Container -- Declarative agent management.

    \b
    Config resolution order:
      1. positional CONFIG path / agent name argument (where applicable)
      2. ``$SCITEX_AGENT_CONTAINER_CONFIG``
      3. ``~/.scitex/agent-container/agents/<name>/<name>.yaml``

    \b
    Example:
      $ sac --version
      $ sac list
      $ sac start ~/.scitex/agent-container/agents/foo/foo.yaml
    """
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

# Auto-accept
main.add_command(send_accept)
main.add_command(start_auto_accept)
main.add_command(stop_auto_accept)

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

# Recall: read back a previous session's jsonl (post-crash recovery,
# context inspection without --continue).
main.add_command(recall)

# Action subsystem: run PaneActions, query attempts, aggregate stats.
main.add_command(actions_cli)

# Render noun-group (sbatch / attach / contributor-spec).
main.add_command(render_group)

# Deprecation aliases — old top-level forms still work but warn to stderr.
main.add_command(deprecated_alias(render_sbatch, new_path="sac render sbatch"))
main.add_command(deprecated_alias(render_attach, new_path="sac render attach"))
main.add_command(
    deprecated_alias(contributor_spec, new_path="sac render contributor-spec")
)

# Connectivity probe (todo#457): fleet-facing WSL ↔ hub liveness.
main.add_command(probe_network)

# Singleton priority check: reports whether this host should yield to a
# higher-priority reachable host (building block for healer reconciler, #250).
main.add_command(priority_check)

# Singleton reconciliation: sweep all local registered agents and yield any
# that have a higher-priority reachable host. Closes the automation gap in #250.
main.add_command(singleton_reconcile)

# Install helpers: host bootstrap + cron installer.
main.add_command(install_group)
main.add_command(install_post_merge_cron)

# A2A protocol — generic agent-to-agent surface (no fleet deps).
from .a2a_cmds import a2a as a2a_group  # noqa: E402

main.add_command(a2a_group)

# MCP introspection group — empty by design (audit §1a requires this surface).
from .mcp_cmds import mcp as mcp_group  # noqa: E402

main.add_command(mcp_group)

# Peer noun-group — outbound A2A calls into other agents' /v1/turn.
# Mirrors scitex_agent_container.peer Python surface.
from .peer_cmds import peer_group  # noqa: E402

main.add_command(peer_group)


if __name__ == "__main__":
    main()
