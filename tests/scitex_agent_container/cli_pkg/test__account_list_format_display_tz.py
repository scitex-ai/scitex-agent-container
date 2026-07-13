"""Tests for ``format_dt_display_tz`` — the ``sac agents list`` Started column
pinned-timezone formatter.

No mocks: the function is pure and takes an injected ``env`` dict, so every
case is exercised with real ISO input + a real env mapping and asserted on
the real ``zoneinfo`` conversion. The test environment resolves ``Asia/Tokyo``
/ ``America/New_York`` (the sibling ``test__account_list_render`` suite already
asserts JST/EST conversions), so the pinned-JST default is deterministic here.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._account_list_format import format_dt_display_tz

# A fixed UTC instant used across cases: 2026-07-12 21:36:30 UTC.
_UTC_ISO = "2026-07-12T21:36:30Z"


def test_defaults_to_pinned_jst_when_no_env_override():
    # Arrange — empty env: no SAC_DISPLAY_TZ → the Asia/Tokyo default applies,
    # independent of the process's own TZ (the WSL host may be UTC).
    src = {}
    # Act
    out = format_dt_display_tz(_UTC_ISO, env=src)
    # Assert — 21:36 UTC = 06:36 JST the next calendar day.
    assert out == "2026-07-13 06:36 (JST)"


def test_uses_space_separator_not_iso_t():
    # Arrange
    src = {}
    # Act
    out = format_dt_display_tz(_UTC_ISO, env=src)
    # Assert — the date-time portion (before the tz paren) uses a space, not
    # the ISO "T" separator. (The "T" in "(JST)" is not the separator.)
    dt_part = out.rsplit(" (", 1)[0]
    assert " " in dt_part and "T" not in dt_part


def test_drops_seconds_minute_precision():
    # Arrange
    src = {}
    # Act
    out = format_dt_display_tz(_UTC_ISO, env=src)
    # Assert — the source seconds (:30) must not appear; only HH:MM colon.
    assert ":30" not in out and out.count(":") == 1


def test_timezone_abbrev_in_parentheses():
    # Arrange
    src = {}
    # Act
    out = format_dt_display_tz(_UTC_ISO, env=src)
    # Assert — the derived abbreviation is shown in parens (JST), not a bare Z.
    assert out.endswith("(JST)") and "Z" not in out


def test_env_override_changes_zone_to_utc():
    # Arrange — SAC_DISPLAY_TZ pins UTC.
    src = {"SAC_DISPLAY_TZ": "UTC"}
    # Act
    out = format_dt_display_tz(_UTC_ISO, env=src)
    # Assert — no conversion; UTC label derived from the tz object.
    assert out == "2026-07-12 21:36 (UTC)"


def test_env_override_abbrev_is_derived_not_hardcoded():
    # Arrange — a non-JST zone must yield ITS abbreviation, proving "(JST)"
    # is derived from the tz object, never a hardcoded label.
    src = {"SAC_DISPLAY_TZ": "America/New_York"}
    # Act
    out = format_dt_display_tz(_UTC_ISO, env=src)
    # Assert — 21:36 UTC on 2026-07-12 is 17:36 EDT (summer, DST).
    assert out == "2026-07-12 17:36 (EDT)" and "JST" not in out


def test_blank_env_override_falls_back_to_default_jst():
    # Arrange — an empty SAC_DISPLAY_TZ must not blank the zone.
    src = {"SAC_DISPLAY_TZ": "   "}
    # Act
    out = format_dt_display_tz(_UTC_ISO, env=src)
    # Assert — the default Asia/Tokyo still applies.
    assert out.endswith("(JST)")


def test_naive_timestamp_is_assumed_utc():
    # Arrange — a naive (no-tz) stamp is treated as UTC (matches what the
    # registry / JSON path writes with the trailing Z).
    naive = "2026-07-12T21:36:30"
    # Act
    out = format_dt_display_tz(naive, env={})
    # Assert — same JST result as the Z-suffixed form.
    assert out == "2026-07-13 06:36 (JST)"


def test_none_input_returns_dash():
    # Arrange
    value = None
    # Act
    out = format_dt_display_tz(value, env={})
    # Assert
    assert out == "-"


def test_empty_string_returns_dash():
    # Arrange
    value = ""
    # Act
    out = format_dt_display_tz(value, env={})
    # Assert
    assert out == "-"


def test_sentinel_dash_returns_dash():
    # Arrange — the registry's "not started" sentinel must not crash / mis-parse.
    value = "-"
    # Act
    out = format_dt_display_tz(value, env={})
    # Assert
    assert out == "-"


def test_unparseable_input_returns_dash():
    # Arrange — a garbage stamp must degrade to the dash, never raise.
    value = "not-a-timestamp"
    # Act
    out = format_dt_display_tz(value, env={})
    # Assert
    assert out == "-"
