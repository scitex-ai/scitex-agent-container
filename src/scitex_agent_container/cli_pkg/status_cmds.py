"""Status commands: status, list, health."""

from __future__ import annotations

import json as json_mod
import sys

import click
from rich.table import Table

from ..config import load_config
from ..health import health_check
from ..lifecycle import agent_status
from ..registry import Registry
from ._helpers import console, print_agent_list, print_agent_list_json


@click.command()
@click.argument("name", required=False)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
def status(name: str | None, as_json: bool) -> None:
    """Show agent status (one agent or all)."""
    registry = Registry()

    if name:
        try:
            info = agent_status(name)
        except Exception as exc:
            if as_json:
                click.echo(json_mod.dumps({"error": str(exc)}))
            else:
                console.print(f"[red]Error: {exc}[/red]")
            sys.exit(1)

        if as_json:
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
        if as_json:
            print_agent_list_json(registry)
        else:
            print_agent_list(registry)


@click.command(name="list")
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
def list_agents(as_json: bool, capability: str | None, machine: str | None) -> None:
    """List all registered agents."""
    registry = Registry()
    if as_json:
        print_agent_list_json(registry, capability=capability, machine=machine)
    else:
        print_agent_list(registry, capability=capability, machine=machine)


@click.command()
@click.argument("name")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
def health(name: str, as_json: bool) -> None:
    """Run a health check on an agent."""
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        if as_json:
            click.echo(json_mod.dumps({"error": f"Agent '{name}' not found"}))
        else:
            console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    try:
        config = load_config(entry["config"])
    except Exception as exc:
        if as_json:
            click.echo(json_mod.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    is_healthy, message = health_check(config)

    if as_json:
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
