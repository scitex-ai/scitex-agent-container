"""Status commands: status, list, health."""

from __future__ import annotations

import json as json_mod
import sys

import click
from rich.table import Table

from .._account.credentials import read_credentials_metadata
from .._lifecycle.health import health_check
from .._lifecycle.lifecycle import agent_status
from .._state.registry import Registry
from ..config import load_config
from ._helpers import _json_flag, console, print_agent_list, print_agent_list_json


def _format_claude_account_block(meta: dict) -> list[str]:
    """Render the ``Claude Code account`` section as a list of text lines.

    Missing values render as ``-``. Returns ``[]`` if no fields are set
    (i.e. every value is ``None``) so the section is omitted entirely.
    """
    if not any(v is not None for v in meta.values()):
        return []

    def _fmt(value):
        return "-" if value is None else str(value)

    email = _fmt(meta.get("email_address"))
    org = _fmt(meta.get("organization_name"))
    display = _fmt(meta.get("display_name"))
    billing = _fmt(meta.get("billing_type"))
    sub_type = meta.get("subscription_type")
    tier = meta.get("rate_limit_tier")
    if sub_type is None and tier is None:
        sub_line = "-"
    else:
        sub_line = f"{_fmt(sub_type)}  (tier: {_fmt(tier)})"
    avail = meta.get("has_available_subscription")
    if avail is None:
        avail_line = "-"
    else:
        avail_line = "yes" if avail else "no"
    extra_enabled = meta.get("has_extra_usage_enabled")
    extra_reason = meta.get("cached_extra_usage_disabled_reason")
    if extra_enabled is None and extra_reason is None:
        extra_line = "-"
    elif extra_enabled:
        extra_line = "enabled"
    else:
        extra_line = "disabled"
        if extra_reason:
            extra_line += f" (reason: {extra_reason})"
    since = _fmt(meta.get("subscription_created_at"))

    return [
        "Claude Code account",
        f"  Email:          {email}",
        f"  Organization:   {org}",
        f"  Display name:   {display}",
        f"  Billing type:   {billing}",
        f"  Subscription:   {sub_line}",
        f"  Available:      {avail_line}",
        f"  Extra usage:    {extra_line}",
        f"  Since:          {since}",
    ]


@click.command(name="show-status")
@click.argument("name", required=False)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.option(
    "--terse",
    "terse",
    is_flag=True,
    default=False,
    help="Project JSON output onto the fleet_watch whitelist (todo#300). "
    "Implies --json. Reduces per-agent payload ~18x.",
)
@click.pass_context
def status(ctx: click.Context, name: str | None, as_json: bool, terse: bool) -> None:
    """Show agent status (one agent or all).

    \b
    Example:
      $ sac agent status
      $ sac agent status head-ywata-note-win
      $ sac agent status --json
    """
    use_json = _json_flag(ctx, as_json) or terse
    registry = Registry()

    if terse and not name:
        click.echo(
            json_mod.dumps(
                {"error": "--terse requires an agent NAME (per-agent mode only)"}
            )
        )
        sys.exit(2)

    if name:
        # stx-allow: fallback (reason: agent_status queries registry and multiplexer state that may be unavailable; CLI exits with code 1 and reports the error in the requested format)
        try:
            info = agent_status(name)
        except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            if use_json:
                click.echo(json_mod.dumps({"error": str(exc)}))
            else:
                console.print(f"[red]Error: {exc}[/red]")
            sys.exit(1)

        if use_json:
            if terse:
                from ..terse import TERSE_STATUS_FIELDS, project_terse

                info = project_terse(info, TERSE_STATUS_FIELDS)
            click.echo(json_mod.dumps(info, indent=2))
            return

        table = Table(title=f"Agent: {name}")
        table.add_column("Field", style="bold")
        table.add_column("Value")
        for key, value in info.items():
            style = "green" if key == "status" and value == "running" else ""
            style = "red" if key == "status" and value == "stopped" else style
            table.add_row(key, str(value), style=style)
        console.print(table)
    else:
        try:
            claude_account = read_credentials_metadata()
        except (
            OSError,
            json_mod.JSONDecodeError,
        ):  # stx-allow: fallback (reason: malformed JSON tolerated)
            claude_account = {}
        # RuntimeError from _check_no_secrets() is a load-bearing alarm
        # that a token just leaked; intentionally propagate.

        if use_json:
            from ._helpers import get_agent_list_data

            payload = {
                "agents": get_agent_list_data(registry),
                "claude_account": claude_account,
            }
            click.echo(json_mod.dumps(payload, indent=2))
        else:
            print_agent_list(registry)
            lines = _format_claude_account_block(claude_account)
            if lines:
                console.print("")
                for line in lines:
                    console.print(line)


@click.command(name="list-agents")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.option(
    "--capability",
    "-c",
    default=None,
    help="Filter by capability label (comma-separated in YAML).",
)
@click.option(
    "--machine",
    "-m",
    default=None,
    help="Filter by machine label.",
)
@click.pass_context
def list_agents(
    ctx: click.Context,
    as_json: bool,
    capability: str | None,
    machine: str | None,
) -> None:
    """List all registered agents.

    \b
    Example:
      $ sac list
      $ sac list --json
      $ sac list --capability HPC
    """
    use_json = _json_flag(ctx, as_json)
    registry = Registry()
    if use_json:
        print_agent_list_json(registry, capability=capability, machine=machine)
    else:
        print_agent_list(registry, capability=capability, machine=machine)


@click.command(name="check-health")
@click.argument("name")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.pass_context
def health(ctx: click.Context, name: str, as_json: bool) -> None:
    """Run a health check on an agent.

    \b
    Example:
      $ sac agent health head-ywata-note-win
      $ sac agent health head-ywata-note-win --json
    """
    use_json = _json_flag(ctx, as_json)
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        if use_json:
            click.echo(json_mod.dumps({"error": f"Agent '{name}' not found"}))
        else:
            console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    # stx-allow: fallback (reason: config YAML may be corrupted or missing after registry entry was created; CLI exits with code 1 in both JSON and human output modes)
    try:
        config = load_config(entry["config"])
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if use_json:
            click.echo(json_mod.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    is_healthy, message = health_check(config)

    if use_json:
        click.echo(
            json_mod.dumps(
                {"name": name, "healthy": is_healthy, "message": message},
                indent=2,
            )
        )
        if not is_healthy:
            sys.exit(1)
        return

    if is_healthy:
        console.print(f"[green]{message}[/green]")
    else:
        console.print(f"[red]{message}[/red]")
        sys.exit(1)
