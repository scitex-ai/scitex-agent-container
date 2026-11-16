"""Explicit CLI admission before startup or token initialization."""

import click


def require_listener_start_approval(yes: bool) -> None:
    """Do not infer explicit CLI approval from environment or token options."""
    if not yes:
        raise click.UsageError(
            "Listener start requires --yes/-y; use --dry-run to preview arguments."
        )
