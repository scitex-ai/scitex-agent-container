"""Explicit CLI admission before listener restart inspects or alters runtime."""

import click


def require_listener_restart_approval(yes: bool) -> None:
    """Require a supplied flag without inferring approval from the environment."""
    if not yes:
        raise click.UsageError(
            "Listener restart requires --yes/-y; use --dry-run to preview arguments."
        )
