"""Threshold admission shared by cleanup providers and their CLI adapter."""

import math


def validate_gc_options(min_age_hours: float, cap: int) -> None:
    """Raise before inspection when cleanup thresholds are invalid."""
    if not math.isfinite(min_age_hours) or min_age_hours < 0:
        raise ValueError("min_age_hours must be finite and non-negative.")
    if cap < 0:
        raise ValueError("cap must be non-negative.")
