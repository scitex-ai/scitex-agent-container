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
from ._helpers import _json_flag, console, print_agent_list, print_agent_list_json


@click.command()
@click.argument("name", required=False)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.pass_context
def status(ctx: click.Context, name: str | None, as_json: bool) -> None:
    """Show agent status (one agent or all)."""
    use_json = _json_flag(ctx, as_json)
    registry = Registry()

    if name:
        try:
            info = agent_status(name)
        except Exception as exc:
            if use_json:
                click.echo(json_mod.dumps({"error": str(exc)}))
            else:
                console.print(f"[red]Error: {exc}[/red]")
            sys.exit(1)

        if use_json:
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
        if use_json:
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
@click.pass_context
def list_agents(
    ctx: click.Context,
    as_json: bool,
    capability: str | None,
    machine: str | None,
) -> None:
    """List all registered agents."""
    use_json = _json_flag(ctx, as_json)
    registry = Registry()
    if use_json:
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
@click.pass_context
def health(ctx: click.Context, name: str, as_json: bool) -> None:
    """Run a health check on an agent."""
    use_json = _json_flag(ctx, as_json)
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        if use_json:
            click.echo(json_mod.dumps({"error": f"Agent '{name}' not found"}))
        else:
            console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    try:
        config = load_config(entry["config"])
    except Exception as exc:
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


def _detect_agent_state(content: str) -> str:
    """Detect agent state from captured pane content."""
    if not content.strip():
        return "empty (no content captured)"
    if "Enter to confirm" in content and "Bypass Permissions" in content:
        return "waiting: Bypass Permissions prompt"
    if "Enter to confirm" in content and "development channels" in content:
        return "waiting: dev channels prompt"
    if "Enter to confirm" in content:
        return "waiting: TUI prompt"
    if "bypass permissions" in content and "Enter to confirm" not in content:
        return "idle (ready for input)"
    if "Thinking" in content or "thinking" in content:
        return "working (thinking)"
    if "Tool" in content:
        return "working (tool use)"
    return "active"


@click.command(name="inspect")
@click.argument("name")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.pass_context
def check_agent(ctx: click.Context, name: str, as_json: bool) -> None:
    """Check live state of an agent by capturing pane content."""
    use_json = _json_flag(ctx, as_json)
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        if use_json:
            click.echo(json_mod.dumps({"error": f"Agent '{name}' not found"}))
        else:
            console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    try:
        config = load_config(entry["config"])
    except Exception as exc:
        if use_json:
            click.echo(json_mod.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    from ..runtimes.multiplexer import get_multiplexer

    mux = get_multiplexer(config)
    session_name = config.screen_name
    alive = mux.exists(session_name)
    content = mux.capture_content(session_name) if alive else ""
    state = _detect_agent_state(content) if alive else "stopped"

    result = {
        "name": name,
        "session": session_name,
        "multiplexer": config.multiplexer,
        "alive": alive,
        "state": state,
    }

    if use_json:
        click.echo(json_mod.dumps(result, indent=2))
    else:
        status_color = "green" if alive else "red"
        console.print(f"[bold]{name}[/bold] ({config.multiplexer}: {session_name})")
        console.print(f"  Status: [{status_color}]{state}[/{status_color}]")
        if content.strip():
            # Show last 5 non-empty lines of content
            lines = [ln for ln in content.splitlines() if ln.strip()]
            preview = "\n".join(lines[-5:])
            console.print(
                f"  Preview:\n    {preview.replace(chr(10), chr(10) + '    ')}"
            )

    if not alive:
        sys.exit(1)
