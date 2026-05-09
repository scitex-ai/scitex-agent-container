"""Snapshot subcommand — self-snapshot for an agent (todo#286)."""

from __future__ import annotations

import json as json_mod
import sys

import click

from .._state.snapshot import take_snapshot
from ._helpers import agent_name_complete


@click.command(name="take-snapshot")
@click.argument("agent", shell_complete=agent_name_complete)
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
@click.option(
    "--terse",
    "terse",
    is_flag=True,
    default=False,
    help="Project JSON output onto the fleet_watch whitelist (todo#300). "
    "Reduces per-agent payload size dramatically.",
)
def snapshot(
    agent: str,
    as_json: bool,  # noqa: ARG001 — accepted for back-compat; output is always JSON
    with_diff: bool,
    session: str | None,
    terse: bool,
) -> None:
    """Take a self-snapshot for AGENT and print it as JSON.

    \b
    Example:
      $ sac agent take-snapshot head-ywata-note-win
      $ sac agent take-snapshot head-ywata-note-win --with-diff
    """
    # stx-allow: fallback (reason: CLI command must emit a JSON error and exit cleanly rather than printing a raw traceback)
    try:
        snap = take_snapshot(agent, session=session, with_diff=with_diff)
    except Exception as exc:  # pragma: no cover — defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        click.echo(json_mod.dumps({"error": str(exc)}))
        sys.exit(1)
    if terse:
        from ..terse import TERSE_SNAPSHOT_FIELDS, project_terse

        snap = project_terse(snap, TERSE_SNAPSHOT_FIELDS)
    click.echo(json_mod.dumps(snap, indent=2))
