"""Tests for the shared ``cli_pkg._timefmt`` time-format SSOT.

No-mocks: both helpers are pure and take an injectable ``now`` (for the
relative one), so every case drives the real function with a known
instant and asserts the exact string. ``format_jst`` fixes the zone to
``Asia/Tokyo`` so no env/TZ juggling is needed for a deterministic
result.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scitex_agent_container.cli_pkg._timefmt import (
    format_jst,
    format_relative_until,
)

# ---------------------------------------------------------------------------
# format_jst — readable JST wall clock
# ---------------------------------------------------------------------------


def test_format_jst_converts_utc_iso_to_jst_wall_clock():
    # Arrange — the operator's real ``Since`` value (19:59:34 UTC).
    value = "2025-05-30T19:59:34.010055Z"
    # Act
    rendered = format_jst(value)
    # Assert — 19:59 UTC + 9h = 04:59 JST the next day.
    assert rendered == "2025-05-31 04:59 (JST)"


def test_format_jst_carries_derived_jst_abbreviation():
    # Arrange
    value = "2025-05-30T19:59:34Z"
    # Act
    rendered = format_jst(value)
    # Assert — the abbreviation is present (derived from the tz, %Z).
    assert rendered.endswith("(JST)")


def test_format_jst_date_only_input_lands_at_0900_jst():
    # Arrange — a bare date parses as 00:00 UTC.
    value = "2024-01-01"
    # Act
    rendered = format_jst(value)
    # Assert — 00:00 UTC + 9h = 09:00 JST, same date.
    assert rendered == "2024-01-01 09:00 (JST)"


def test_format_jst_aware_datetime_input():
    # Arrange — an aware datetime, not a string.
    value = datetime(2026, 3, 1, 3, 0, 0, tzinfo=timezone.utc)
    # Act
    rendered = format_jst(value)
    # Assert — 03:00 UTC + 9h = 12:00 JST.
    assert rendered == "2026-03-01 12:00 (JST)"


def test_format_jst_naive_datetime_assumed_utc():
    # Arrange — a naive datetime is treated as UTC.
    value = datetime(2026, 3, 1, 3, 0, 0)
    # Act
    rendered = format_jst(value)
    # Assert
    assert rendered == "2026-03-01 12:00 (JST)"


def test_format_jst_none_returns_dash():
    # Arrange
    value = None
    # Act
    rendered = format_jst(value)
    # Assert
    assert rendered == "-"


def test_format_jst_empty_string_returns_dash():
    # Arrange
    value = "   "
    # Act
    rendered = format_jst(value)
    # Assert
    assert rendered == "-"


def test_format_jst_unparseable_returns_dash():
    # Arrange
    value = "not-a-timestamp"
    # Act
    rendered = format_jst(value)
    # Assert
    assert rendered == "-"


def test_format_jst_custom_empty_sentinel():
    # Arrange — caller can override the missing-value sentinel.
    value = None
    # Act
    rendered = format_jst(value, empty="n/a")
    # Assert
    assert rendered == "n/a"


# ---------------------------------------------------------------------------
# format_relative_until — time remaining until a future instant
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone.utc)


def test_relative_hours_minutes_shape():
    # Arrange — +4h05m from now (the operator's 5h example).
    value = "2026-07-12T04:05:00+00:00"
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == "in 4h05m"


def test_relative_days_hours_shape():
    # Arrange — +2d3h from now (the operator's 7d example).
    value = "2026-07-14T03:00:00+00:00"
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == "in 2d 3h"


def test_relative_minutes_only_under_one_hour():
    # Arrange — +42m from now.
    value = "2026-07-12T00:42:00+00:00"
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == "in 42m"


def test_relative_sub_minute_is_less_than_one_minute():
    # Arrange — +30s from now.
    value = "2026-07-12T00:00:30+00:00"
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == "in <1m"


def test_relative_past_instant_is_now():
    # Arrange — a reset already due (in the past).
    value = "2026-07-11T00:00:00+00:00"
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == "now"


def test_relative_parses_z_suffix():
    # Arrange — the raw ``...Z`` API shape must parse.
    value = "2026-07-12T04:05:00Z"
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == "in 4h05m"


def test_relative_none_returns_empty_string():
    # Arrange — no cached reset → empty hint (caller omits it).
    value = None
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == ""


def test_relative_unparseable_returns_empty_string():
    # Arrange
    value = "not-a-timestamp"
    # Act
    rendered = format_relative_until(value, now=_NOW)
    # Assert
    assert rendered == ""
