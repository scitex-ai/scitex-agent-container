"""Pure admission for worktree cleanup CLI controls."""

import math

import click


def validate_cleanup_options(min_age_hours: float, cap: int) -> None:
    """Refuse invalid thresholds before discovery or repository inspection."""
    if not math.isfinite(min_age_hours) or min_age_hours < 0:
        raise click.UsageError("--min-age-hours must be finite and non-negative.")
    if cap < 0:
        raise click.UsageError("--cap must be non-negative.")
