"""Lifecycle commands: start, stop, restart, cleanup.

Includes the new ``--all`` / ``--force`` flags for bulk-safe operations.
"""

from __future__ import annotations

import sys
import traceback

import click

from ..config import load_config, resolve_config
from ..lifecycle import (
    agent_restart,
    agent_start,
    agent_stop,
    agent_stop_all,
)
from ..registry import Registry
from ._helpers import console


@click.command()
@click.argument("config_path", type=str)
@click.option(
    "--no-preflight",
    is_flag=True,
    default=False,
    help="Skip preflight checks (useful for slow SSH hosts).",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="If already running or stale, stop first then start fresh.",
)
def start(config_path: str, no_preflight: bool, force: bool) -> None:
    """Start an agent from a YAML definition."""
    try:
        config_path = resolve_config(config_path)
        config = load_config(config_path)
        location = (
            f"REMOTE: {config.remote.host}" if config.remote.is_remote else "LOCAL"
        )
        console.print(
            f"[blue]Starting agent '{config.name}' "
            f"(runtime: {config.runtime}, {location})...[/blue]"
        )
        if no_preflight:
            console.print("[dim]Preflight checks skipped (--no-preflight)[/dim]")
        if force:
            console.print("[dim]Force mode: stopping any existing instance first[/dim]")
        agent_start(config_path, no_preflight=no_preflight, force=force)
        console.print(
            f"[green]Agent '{config.name}' started successfully [{location}][/green]"
        )
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        traceback.print_exc()
        sys.exit(1)


@click.command()
@click.argument("name", required=False)
@click.option(
    "--all",
    "stop_all",
    is_flag=True,
    default=False,
    help="Stop every agent in the registry.",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Tolerate stale registry, missing configs, and hook failures.",
)
def stop(name: str | None, stop_all: bool, force: bool) -> None:
    """Stop a running agent (or --all)."""
    if not stop_all and not name:
        click.echo(
            "Error: provide a NAME or use --all.\n"
            "  scitex-agent-container stop <name>\n"
            "  scitex-agent-container stop --all",
            err=True,
        )
        sys.exit(2)

    if stop_all:
        results = agent_stop_all(force=force)
        if not results:
            console.print("[dim]No agents in registry.[/dim]")
            return
        any_failure = False
        for agent_name, ok, msg in results:
            if ok:
                console.print(f"[green]✓ {agent_name}[/green]: {msg}")
            else:
                any_failure = True
                console.print(f"[red]✗ {agent_name}[/red]: {msg}")
        if any_failure and not force:
            sys.exit(1)
        return

    try:
        agent_stop(name, force=force)  # type: ignore[arg-type]
        console.print(f"[green]Agent '{name}' stopped[/green]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command()
@click.argument("name")
def restart(name: str) -> None:
    """Restart an agent."""
    try:
        agent_restart(name)
        console.print(f"[green]Agent '{name}' restarted[/green]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command()
def cleanup() -> None:
    """Remove stale registry entries (where the screen is already gone)."""
    registry = Registry()
    cleaned = registry.cleanup_stale()
    if cleaned:
        console.print(f"[green]Cleaned {cleaned} stale registry entries[/green]")
    else:
        console.print("[dim]No stale entries found.[/dim]")
