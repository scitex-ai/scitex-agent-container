"""Explicit CLI admission before listener stop can inspect or alter runtime."""

import click


def require_listener_stop_approval(yes: bool) -> None:
    """Require a supplied flag without inferring approval from the environment."""
    if not yes:
        raise click.UsageError(
            "Listener stop requires --yes/-y; use --dry-run to preview arguments."
        )
