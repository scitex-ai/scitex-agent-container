"""Explicit CLI admission before remote scratch lookup or submission."""

import click


def require_run_approval(yes: bool) -> None:
    """Require the supplied flag; environment assumptions are not approval."""
    if not yes:
        raise click.UsageError(
            "Spartan pytest submission requires --yes/-y; use --dry-run to preview."
        )
