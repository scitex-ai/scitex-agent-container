"""Snapshot subcommand — self-snapshot for an agent (todo#286)."""

from __future__ import annotations

import json as json_mod
import sys

import click

from ..snapshot import take_snapshot


@click.command(name="snapshot")
@click.option("--agent", "agent", required=True, help="Agent name to snapshot.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=True,
    help="Output as JSON (default; no human mode).",
)
@click.option(
    "--diff/--no-diff",
    "with_diff",
    default=True,
    help="Include diff against previous snapshot (default: --diff).",
)
@click.option(
    "--session",
    default=None,
    help="Multiplexer session name (defaults to agent name).",
)
def snapshot(agent: str, as_json: bool, with_diff: bool, session: str | None) -> None:
    """Take a self-snapshot for AGENT and print it as JSON."""
    try:
        snap = take_snapshot(agent, session=session, with_diff=with_diff)
    except Exception as exc:  # pragma: no cover — defensive
        click.echo(json_mod.dumps({"error": str(exc)}))
        sys.exit(1)
    click.echo(json_mod.dumps(snap, indent=2))
