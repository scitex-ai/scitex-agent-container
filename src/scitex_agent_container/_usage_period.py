"""Shared UTC time-window helpers for per-agent usage accounting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_usage_timestamp(value: Any) -> datetime | None:
    """Return an aware UTC datetime for a persisted usage timestamp."""
    if isinstance(value, datetime):
        parsed = value
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) >= 0.0
    ):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def require_usage_timestamp(value: str | datetime, option: str) -> datetime:
    """Parse a user-supplied period bound or raise a useful value error."""
    parsed = parse_usage_timestamp(value)
    if parsed is None:
        raise ValueError(
            f"{option} must be an ISO-8601 timestamp (for example 2026-07-26T00:00:00Z)"
        )
    return parsed


def usage_timestamp_iso(value: datetime | None) -> str | None:
    """Render a period bound as a canonical ISO-8601 UTC timestamp."""
    if value is None:
        return None
    utc = value.astimezone(timezone.utc)
    base = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        fraction = f"{utc.microsecond:06d}".rstrip("0")
        return f"{base}.{fraction}Z"
    return f"{base}Z"


def timestamp_in_period(
    timestamp: datetime | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    """Return whether ``timestamp`` is in the half-open ``[since, until)``."""
    if timestamp is None:
        return since is None and until is None
    if since is not None and timestamp < since:
        return False
    if until is not None and timestamp >= until:
        return False
    return True


__all__ = [
    "parse_usage_timestamp",
    "require_usage_timestamp",
    "timestamp_in_period",
    "usage_timestamp_iso",
]
