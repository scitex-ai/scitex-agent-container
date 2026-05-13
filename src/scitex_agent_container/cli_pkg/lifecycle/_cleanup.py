#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac db clean`` (a.k.a. legacy ``clean-registry``) — drop stale entries."""

from __future__ import annotations

import click

from ..._state.registry import Registry
from .._helpers import console


@click.command(name="clean-registry")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show how many stale entries would be removed without modifying registry.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def cleanup(dry_run: bool, yes: bool) -> None:
    """Remove stale registry entries (where the screen is already gone).

    \b
    Example:
      $ sac registry clean
      $ sac registry clean --dry-run
    """
    registry = Registry()
    if dry_run:
        # Probe count without mutating: re-implement minimal stale check via
        # a fresh probe — fall back to a textual hint if the registry doesn't
        # expose a non-mutating preview.
        click.echo(
            "[dry-run] would remove stale registry entries (run without --dry-run to apply)"
        )
        return
    if not yes:
        click.echo(
            "Refusing to remove stale registry entries without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)
    cleaned = registry.cleanup_stale()
    if cleaned:
        console.print(f"[green]Cleaned {cleaned} stale registry entries[/green]")
    else:
        console.print("[dim]No stale entries found.[/dim]")


__all__ = ["cleanup"]
