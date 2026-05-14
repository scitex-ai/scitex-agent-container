#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents stop`` — stop one or more running agents."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..._lifecycle.lifecycle import agent_stop
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console
from ._common import _iter_agent_yamls


@click.command()
@click.argument(
    "targets",
    type=str,
    nargs=-1,
    required=True,
    shell_complete=agent_name_complete,
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Tolerate stale registry, missing configs, and hook failures.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print which agent(s) would be stopped without sending the kill.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt for bulk stop.",
)
def stop(
    targets: tuple[str, ...],
    force: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Stop one or more running agents.

    Each TARGET is an agent name, a YAML path, or a directory containing
    ``<name>/<name>.yaml`` agent layouts. Multiple targets may be given.

    \b
    Example:
      $ sac agent stop foo
      $ sac agent stop foo bar baz
      $ sac agent stop ~/.scitex/agent-container/agents/   # whole dir = bulk
      $ sac agent stop foo --dry-run
    """
    # Classify targets: directory targets expand to all <name>/<name>.yaml
    # under them; non-directory targets are agent names or YAML paths.
    single_targets: list[str] = []
    bulk_yamls_from_dirs: list[str] = []
    for t in targets:
        p = Path(t).expanduser()
        if p.is_dir():
            for _name, yp in _iter_agent_yamls(p):
                bulk_yamls_from_dirs.append(yp)
        else:
            single_targets.append(t)

    if dry_run:
        for t in single_targets:
            click.echo(f"[dry-run] would stop agent '{t}'")
        for yp in bulk_yamls_from_dirs:
            click.echo(f"[dry-run] would stop agent at '{yp}'")
        return

    # Refuse bulk stop without --yes/-y when directory targets resolved to ≥2 yamls.
    if len(bulk_yamls_from_dirs) > 1 and not yes:
        click.echo(
            f"Refusing to stop {len(bulk_yamls_from_dirs)} agents without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)

    # Bulk-from-dir-targets path
    any_error = False
    for yaml_path in bulk_yamls_from_dirs:
        try:
            config = load_config(yaml_path)
            agent_stop(config.name, force=force)
            console.print(f"[green]Agent '{config.name}' stopped[/green]")
        except Exception as exc:  # stx-allow: fallback (reason: one stop failure must not abort the remaining bulk stops)
            any_error = True
            console.print(f"[red]Error ({yaml_path}): {exc}[/red]")

    # Per-target single-stop loop
    for raw_target in single_targets:
        # stx-allow: fallback (reason: config resolution or agent_stop can raise if the agent is not in the registry or the session is already gone)
        try:
            name: str = raw_target
            if "/" in name or name.endswith((".yaml", ".yml")):
                config_path = resolve_with_prefix(name)
                config = load_config(config_path)
                name = config.name
            agent_stop(name, force=force)
            console.print(f"[green]Agent '{name}' stopped[/green]")
        except Exception as exc:
            any_error = True
            console.print(f"[red]Error ({raw_target}): {exc}[/red]")

    if any_error:
        sys.exit(1)


__all__ = ["stop"]
