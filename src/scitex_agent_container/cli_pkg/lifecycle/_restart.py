#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents restart`` — stop-then-start a single agent."""

from __future__ import annotations

import sys

import click

from ..._lifecycle.lifecycle import agent_restart
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console


@click.command()
@click.argument("name", shell_complete=agent_name_complete)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be restarted without making changes.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def restart(name: str, dry_run: bool, yes: bool) -> None:
    """Restart an agent.

    \b
    Example:
      $ sac agent restart foo
      $ sac agent restart foo --dry-run
    """
    if dry_run:
        click.echo(f"[dry-run] would restart agent '{name}'")
        return
    if not yes:
        click.echo(f"Refusing to restart agent '{name}' without --yes/-y.", err=True)
        raise SystemExit(2)
    # stx-allow: fallback (reason: config resolution or agent_restart can raise if the agent is not running or the session cannot be found; error message + sys.exit(1) is cleaner than an unhandled traceback)
    try:
        if "/" in name or name.endswith((".yaml", ".yml")):
            config_path = resolve_with_prefix(name)
            config = load_config(config_path)
            name = config.name
        agent_restart(name)
        console.print(f"[green]Agent '{name}' restarted[/green]")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


__all__ = ["restart"]
