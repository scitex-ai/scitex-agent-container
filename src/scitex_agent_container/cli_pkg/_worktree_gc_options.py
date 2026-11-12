"""Pure admission for worktree cleanup CLI controls."""

import click


def validate_cleanup_options(min_age_hours: float, cap: int) -> None:
    """Refuse invalid thresholds before discovery or repository inspection."""
    from .._maintenance._worktree_gc_options import validate_gc_options

    try:
        validate_gc_options(min_age_hours, cap)
    except ValueError as exc:
        parameter, _, reason = str(exc).partition(" ")
        raise click.UsageError(f"--{parameter.replace('_', '-')} {reason}") from exc
