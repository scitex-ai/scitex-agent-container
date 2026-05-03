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
from .auto_accept_group import auto_accept_group
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
from .snapshot_cmds import snapshot
from .status_cmds import check_agent, health, list_agents, status

# Domain categories for ``sac --help``. Anything not listed here drops
# into a final "Other" section. Mirrors the scitex-dev CLI grouping UX.
#
# Sections marked "(claude-code only)" are pane-mediated operations on the
# tmux/screen multiplexer used by ``runtime: claude-code``; they don't
# apply to ``runtime: claude-session`` (SDK) agents, which have no
# multiplexer session and use ``--foreground`` / ``POST /v1/turn`` instead.
COMMAND_CATEGORIES = [
    ("Lifecycle", ["start", "stop", "restart", "validate"]),
    (
        "Status / introspection",
        [
            "show-status",
            "list-agents",
            "show-logs",
            "check",
            "check-health",
            "check-priority",
            "find",
            "recall",
        ],
    ),
    (
        "Render / spec",
        ["render-sbatch", "render-attach", "render-contributor-spec"],
    ),
    ("Account / quota", ["account", "watch-quota"]),
    ("Actions / events", ["actions", "ingest-hook-event"]),
    ("Registry", ["clean-registry", "reconcile-singletons"]),
    (
        "Install / build",
        ["installation", "build-image", "install-post-merge-cron"],
    ),
    ("Network", ["probe-network"]),
    ("Interface", ["a2a", "mcp", "peer", "list-python-apis"]),
    (
        "Pane operations (claude-code only)",
        [
            "attach",
            "inspect",
            "take-snapshot",
            "auto-accept",
            "send-accept",
            "start-auto-accept",
            "stop-auto-accept",
        ],
    ),
]


class _CategorizedHelpGroup(HelpRecursiveGroup):
    command_categories = COMMAND_CATEGORIES


@click.group(
    cls=_CategorizedHelpGroup,
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
      $ sac list-agents
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

# Auto-accept noun-group (send / start / stop) — valid noun-group: every
# leaf is a real verb per general/03_interface_02_cli/06_noun-verb-catalog.md.
main.add_command(auto_accept_group)
main.add_command(deprecated_alias(send_accept, new_path="sac auto-accept send"))
main.add_command(deprecated_alias(start_auto_accept, new_path="sac auto-accept start"))
main.add_command(deprecated_alias(stop_auto_accept, new_path="sac auto-accept stop"))

# Status / listing — flat verb-noun compounds (per audit §1: leaves must be
# verbs; "status", "logs", "snapshot" are nouns and only work as compound
# leaves like "show-status").
main.add_command(status)  # registered as "show-status"
main.add_command(list_agents)  # registered as "list-agents"
main.add_command(health)  # registered as "check-health"
main.add_command(check_agent)  # registered as "inspect"
main.add_command(snapshot)  # registered as "take-snapshot"

# Info / introspection
main.add_command(find)
main.add_command(logs)  # registered as "show-logs"
main.add_command(attach)
main.add_command(list_python_apis)

# Build / validation
main.add_command(check)  # bare verb with required positional (audit §1 exception)
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

# Render ports — flat verb-noun compounds (per audit §1: "render" is itself
# a verb in the catalog and cannot be a group name).
main.add_command(render_sbatch)
main.add_command(render_attach)
main.add_command(contributor_spec)  # registered as "render-contributor-spec"

# Connectivity probe (todo#457): fleet-facing WSL ↔ hub liveness.
main.add_command(probe_network)

# Singleton priority check: reports whether this host should yield to a
# higher-priority reachable host (building block for healer reconciler, #250).
main.add_command(priority_check)  # registered as "check-priority"

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
# Mirrors scitex_agent_container._network.peer Python surface. Valid noun-group:
# leaves "post-turn" and "resolve-url" are verb-compound leaves (verb at head).
from .peer_cmds import peer_group  # noqa: E402

main.add_command(peer_group)


if __name__ == "__main__":
    main()
