"""Click entry point for scitex-agent-container.

This module wires every subcommand defined in the sibling modules
into a single click group, registered as the ``scitex-agent-container``
console script (see pyproject.toml).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version_lookup

import click

from ._helpers import HelpRecursiveGroup, renamed_redirect


def _pkg_version() -> str:
    """Return the installed package version, or 'dev' off-tree."""
    try:
        return _pkg_version_lookup("scitex-agent-container")
    except PackageNotFoundError:
        return "dev"


from .a2a_cmds import a2a as a2a_group
from .account_cmds import account, quota_watch
from .agent_group import agent_group
from .auto_accept_group import auto_accept_group
from .build_cmds import build, check, validate
from .contributor_spec_cmds import contributor_spec
from .db_group import db_group
from .event_group import event_group
from .hook_cmds import hook_event
from .host_group import host_group
from .image_group import image_group
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
from .mcp_cmds import mcp as mcp_group
from .network_group import network_group
from .peer_cmds import peer_group
from .priority_cmds import priority_check, singleton_reconcile
from .probe_cmds import probe_network
from .quota_group import quota_group
from .recall_cmds import recall
from .registry_group import registry_group
from .skills_group import skills_group
from .snapshot_cmds import snapshot
from .status_cmds import check_agent, health, list_agents, status
from .template_group import template_group

# ---------------------------------------------------------------------------
# Help categories — clean noun-group surface
# ---------------------------------------------------------------------------
COMMAND_CATEGORIES = [
    ("Agent", ["agent"]),
    ("Lifecycle (multiplexer)", ["auto-accept"]),
    ("Account & Quota", ["account", "quota"]),
    ("Network & Peer", ["host", "network", "peer", "a2a"]),
    ("Registry & Events", ["db", "registry", "event", "actions"]),
    ("Build & Install", ["image", "installation", "template"]),
    ("Introspection", ["mcp", "list-python-apis", "skills"]),
]


class _CategorizedHelpGroup(HelpRecursiveGroup):
    command_categories = COMMAND_CATEGORIES


@click.group(
    cls=_CategorizedHelpGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        f"sac (v{_pkg_version()}) — SciTeX Agent Container: declarative "
        f"agent management.\n\n"
        "\b\n"
        "Config resolution order:\n"
        "  1. positional CONFIG path / agent name argument (where applicable)\n"
        "  2. ``$SCITEX_AGENT_CONTAINER_CONFIG``\n"
        "  3. ``~/.scitex/agent-container/agents/<name>/<name>.yaml``\n\n"
        "\b\n"
        "Example:\n"
        "  $ sac --version\n"
        "  $ sac agent list\n"
        "  $ sac agent start ~/.scitex/agent-container/agents/foo/foo.yaml\n"
    ),
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
      $ sac agent list
      $ sac agent start ~/.scitex/agent-container/agents/foo/foo.yaml
    """
    ctx.ensure_object(dict)
    if as_json:
        ctx.obj["json"] = True
    if help_recursive:
        click.echo(ctx.command.get_help_recursive(ctx))  # type: ignore[attr-defined]
        ctx.exit(0)
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# Noun-groups (the new clean surface)
# ---------------------------------------------------------------------------
main.add_command(agent_group)
main.add_command(db_group)
main.add_command(host_group)
main.add_command(registry_group)
main.add_command(event_group)
main.add_command(quota_group)
main.add_command(network_group)
main.add_command(image_group)
main.add_command(template_group)
main.add_command(skills_group)

main.add_command(install_group)  # registered as "install"
# Add cron sub-verb on the install group (renamed leaf; keeps the
# old top-level ``install-post-merge-cron`` alias working).
install_group.add_command(
    click.Command(
        name="setup-cron",
        callback=install_post_merge_cron.callback,
        params=list(install_post_merge_cron.params),
        help=install_post_merge_cron.help,
        short_help=install_post_merge_cron.short_help,
        epilog=install_post_merge_cron.epilog,
    )
)

# Already-noun-group surfaces
main.add_command(auto_accept_group)
main.add_command(account)
main.add_command(a2a_group)
main.add_command(mcp_group)
main.add_command(peer_group)

# Standard introspection (top-level by convention)
main.add_command(list_python_apis)


# ---------------------------------------------------------------------------
# Renamed-command redirects (F-CS13 / scitex CLI convention §5):
# old top-level names still parse so the user gets a helpful redirect
# message, but they hard-error (exit 2) instead of warn-then-run.
# Hidden from --help so the new noun-verb surface is uncluttered.
# Soft warnings let stale scripts persist indefinitely; hard errors
# force the fix in one iteration.
# ---------------------------------------------------------------------------
def _hidden_alias(cmd: click.Command, *, new_path: str, name: str | None = None):
    alias = renamed_redirect(cmd, new_path=new_path)
    if name is not None:
        alias.name = name
    alias.hidden = True
    return alias


# Lifecycle
main.add_command(_hidden_alias(start, new_path="sac agent start"))
main.add_command(_hidden_alias(stop, new_path="sac agent stop"))
main.add_command(_hidden_alias(restart, new_path="sac agent restart"))
main.add_command(_hidden_alias(validate, new_path="sac agent validate"))
main.add_command(_hidden_alias(check, new_path="sac agent check"))

# Auto-accept (already grouped — keep historical flat aliases)
main.add_command(_hidden_alias(send_accept, new_path="sac auto-accept send"))
main.add_command(_hidden_alias(start_auto_accept, new_path="sac auto-accept start"))
main.add_command(_hidden_alias(stop_auto_accept, new_path="sac auto-accept stop"))

# Status / introspection
main.add_command(_hidden_alias(status, new_path="sac agent status"))
main.add_command(_hidden_alias(list_agents, new_path="sac agent list"))
main.add_command(_hidden_alias(health, new_path="sac agent health"))
main.add_command(_hidden_alias(check_agent, new_path="sac agent inspect"))
main.add_command(_hidden_alias(snapshot, new_path="sac agent take-snapshot"))
main.add_command(_hidden_alias(find, new_path="sac agent find"))
main.add_command(_hidden_alias(logs, new_path="sac agent logs"))
main.add_command(_hidden_alias(attach, new_path="sac agent attach"))
main.add_command(_hidden_alias(recall, new_path="sac agent recall"))
main.add_command(_hidden_alias(priority_check, new_path="sac agent check-priority"))

# Render / template
main.add_command(
    _hidden_alias(contributor_spec, new_path="sac template render-contributor-spec")
)

# Quota
main.add_command(_hidden_alias(quota_watch, new_path="sac quota watch"))

# Hook events
main.add_command(_hidden_alias(hook_event, new_path="sac event ingest"))

# Registry — `registry clean` is now `db clean` (F-CS11 phase 5);
# send the top-level alias straight there to avoid double-redirect.
main.add_command(_hidden_alias(cleanup, new_path="sac db clean"))
main.add_command(_hidden_alias(singleton_reconcile, new_path="sac registry reconcile"))

# Build / image
main.add_command(_hidden_alias(build, new_path="sac image build"))

# Network
main.add_command(_hidden_alias(probe_network, new_path="sac network probe"))

# Install
main.add_command(
    _hidden_alias(install_post_merge_cron, new_path="sac installation setup-cron")
)


def cli_entry_point() -> None:
    """Console-script entry. Honours the global ``--on <peer>`` flag.

    Click's group parser normally consumes ``--on`` during ``main``'s
    own arg parsing, but the flag has to be honoured BEFORE the
    subcommand is dispatched: ``sac --on spartan agent list`` must
    run ``sac agent list`` on spartan, not locally. Pre-process
    ``sys.argv`` here, dispatch via host_group.dispatch_remote when
    the flag is present, and fall through to plain ``main()``
    otherwise.
    """
    import sys

    from .host_group import dispatch_remote, split_on_flag

    # stx-allow: fallback (reason: a malformed --on value should still
    # surface a useful error rather than crash the entry point)
    try:
        peer, rest = split_on_flag(sys.argv[1:])
    except click.UsageError as exc:
        click.echo(f"error: {exc.format_message()}", err=True)
        sys.exit(2)
    if peer is not None:
        sys.exit(dispatch_remote(peer, rest))
    main()


if __name__ == "__main__":
    cli_entry_point()
