"""Shared CLI helpers: rich console, recursive help group, agent-list formatting."""

from __future__ import annotations

import json as json_mod

import click
from rich.console import Console
from rich.table import Table

from ..config import load_config
from ..registry import Registry

console = Console()


class HelpRecursiveGroup(click.Group):
    """Click group that supports --help-recursive to dump every subcommand."""

    def format_help(self, ctx, formatter):
        super().format_help(ctx, formatter)

    def get_help_recursive(self, ctx) -> str:
        """Return help text for all commands recursively."""
        lines = []
        lines.append("=" * 60)
        lines.append("SciTeX Agent Container - Complete Command Reference")
        lines.append("=" * 60)
        lines.append("")

        with ctx.scope() as _:
            lines.append(self.get_help(ctx))
            lines.append("")

        for name in sorted(self.list_commands(ctx)):
            cmd = self.get_command(ctx, name)
            if cmd is None:
                continue
            lines.append("-" * 60)
            lines.append(f"Command: {name}")
            lines.append("-" * 60)
            sub_ctx = click.Context(cmd, info_name=name, parent=ctx)
            lines.append(cmd.get_help(sub_ctx))
            lines.append("")

        return "\n".join(lines)


def get_agent_list_data(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> list[dict]:
    """Get agent list as plain dicts for JSON or table output.

    Args:
        registry: The agent registry to query.
        capability: If set, only include agents whose ``capabilities`` label
            contains this value (comma-separated matching).
        machine: If set, only include agents whose ``machine`` label matches.
    """
    from ..runtimes.screen import ScreenManager
    from ..runtimes.tmux import TmuxManager

    def _detect_multiplexer(session_name: str) -> str | None:
        """Detect which multiplexer hosts a session. Tmux preferred."""
        if not session_name or session_name == "?":
            return None
        try:
            if TmuxManager.exists(session_name):
                return "tmux"
        except Exception:
            pass
        try:
            if ScreenManager.exists(session_name):
                return "screen"
        except Exception:
            pass
        return None

    entries = registry.list_all()
    results: list[dict] = []
    for entry in entries:
        name = entry.get("name", "?")
        screen_name = entry.get("screen", "?")
        started = entry.get("started_at", "?")
        labels: dict[str, str] = {}
        remote_host = ""
        config_path = entry.get("config")
        cfg = None
        if config_path:
            try:
                cfg = load_config(config_path)
                labels = cfg.labels
                if cfg.remote.is_remote:
                    remote_host = cfg.remote.host
            except Exception:
                pass

        try:
            if cfg and cfg.remote.is_remote:
                from ..runtimes.claude_code import ClaudeCodeRuntime

                is_running = ClaudeCodeRuntime().is_running(cfg)
            else:
                is_running = ScreenManager.exists(screen_name)
        except Exception:
            is_running = False  # Graceful degradation on SSH timeout etc.

        if machine and labels.get("machine") != machine:
            continue
        if capability:
            caps = [
                c.strip()
                for c in labels.get("capabilities", "").split(",")
                if c.strip()
            ]
            if capability not in caps:
                continue

        multiplexer: str | None = None
        if not (cfg and cfg.remote.is_remote):
            multiplexer = _detect_multiplexer(screen_name)

        row: dict = {
            "name": name,
            "status": "running" if is_running else "stopped",
            "screen": screen_name,
            "multiplexer": multiplexer,
            "started_at": started,
        }
        if remote_host:
            row["remote"] = remote_host
        if labels:
            row["labels"] = labels
        results.append(row)
    return results


def print_agent_list_json(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> None:
    """Print agent list as JSON."""
    data = get_agent_list_data(registry, capability=capability, machine=machine)
    click.echo(json_mod.dumps(data, indent=2))


def print_agent_list(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> None:
    """Print a rich table of all registered agents."""
    data = get_agent_list_data(registry, capability=capability, machine=machine)
    if not data:
        console.print("[dim]No agents registered.[/dim]")
        return

    table = Table(title="Registered Agents")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Location")
    table.add_column("Screen")
    table.add_column("Started")

    for row in data:
        status_str = (
            "[green]running[/green]"
            if row["status"] == "running"
            else "[red]stopped[/red]"
        )
        remote = row.get("remote", "")
        location = f"[cyan]REMOTE: {remote}[/cyan]" if remote else "LOCAL"
        table.add_row(
            row["name"], status_str, location, row["screen"], row["started_at"]
        )

    console.print(table)
