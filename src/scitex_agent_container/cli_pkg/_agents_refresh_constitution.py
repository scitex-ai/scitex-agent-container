"""``sac agents refresh-constitution`` — merged is not live until this runs.

Registered onto ``sac agents`` by :func:`register`, the same way
:mod:`._agents_reconcile` attaches. The engine lives in
:mod:`.._jobs._constitution_refresh`; this module is the operator's window
onto it and the command the declared ``scitex-agent-container-constitution-
refresh`` timer runs every ten minutes.
"""

from __future__ import annotations

from pathlib import Path

import click

from .._jobs._constitution_refresh import (
    OVERLAY_ROOT_ENV,
    SOURCE_ENV,
    default_overlays_root,
    default_source,
    run,
)


@click.command(name="refresh-constitution")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report what WOULD change; write nothing.",
)
@click.option(
    "--source",
    "source",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        f"Constitution to distribute [default: ${SOURCE_ENV}, else "
        "~/.claude/commands/constitution.md]."
    ),
)
@click.option(
    "--overlays-root",
    "overlays_root",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        f"Directory holding one overlay per agent [default: ${OVERLAY_ROOT_ENV}, "
        "else ~/.scitex/agent-container/containers/overlays]."
    ),
)
def refresh_constitution(
    dry_run: bool, source: Path | None, overlays_root: Path | None
) -> None:
    """Push the current constitution into every agent overlay (no restart).

    Writes ``<overlay>/upper/home/agent/.claude/commands/constitution.md`` —
    the path a RUNNING agent reads, visible to the live container at once —
    only where the copy differs, and reads each copy back through the
    overlay before counting it refreshed.

    \b
    Preview:
      $ sac agents refresh-constitution --dry-run
    \b
    Distribute (the scheduled form):
      $ sac agents refresh-constitution

    \b
    Exit codes:
      0  every overlay current
      1  at least one overlay could not be refreshed
      2  the source is missing, unreadable, or under 10000B (a probable
         truncation is refused rather than fanned out to every agent)
    """
    code = run(
        source or default_source(),
        overlays_root or default_overlays_root(),
        dry_run=dry_run,
    )
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``refresh-constitution`` to the parent ``agents`` Click group."""
    agent_group.add_command(refresh_constitution)


__all__ = ["refresh_constitution", "register"]
