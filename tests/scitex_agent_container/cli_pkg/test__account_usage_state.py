"""Gates for the three-valued usage reading (INCIDENT 2026-08-12).

`sac accounts list` drew a confident 2 % bar over a figure the Anthropic
console had at 92 %. The number was neither stale nor miscomputed — it
belonged to a DIFFERENT account. A bar is an assertion, and the renderer had
no way to say "I did not measure this", so it asserted anyway.

These tests pin the three states apart. The ones that matter most assert
that ``pct_*`` is ``None`` for an unknown reading: making "we don't know"
unrepresentable as a number is what stops a downstream renderer or aggregate
from quietly treating it as data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scitex_agent_container._account.account_verify import (
    MISMATCH,
    UNVERIFIED,
    VERIFIED,
    AccountIdentity,
)
from scitex_agent_container.cli_pkg._account_usage_state import (
    KNOWN,
    STALE,
    UNKNOWN,
    classify_usage,
    format_age_short,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

GOOD = AccountIdentity(
    name="acct", state=VERIFIED, verified_email="a@example.com", verified_uuid="u1"
)
WRONG_OWNER = AccountIdentity(
    name="ywatanabe-scitex-ai",
    state=MISMATCH,
    claimed_email="ywatanabe@scitex.ai",
    verified_email="ywata1989@gmail.com",
)
NOT_CHECKED = AccountIdentity(name="acct", state=UNVERIFIED)


def _usage(age_seconds=10):
    as_of = (NOW - timedelta(seconds=age_seconds)).isoformat()
    return {
        "used_pct_5h": 9.0,
        "used_pct_7d": 3.0,
        "reset_at_5h": "2026-08-12T14:49:59+00:00",
        "reset_at_7d": "2026-08-18T08:00:00+00:00",
        "as_of": as_of,
        "fetched_at": as_of,
    }


# ---------------------------------------------------------------------------
# known
# ---------------------------------------------------------------------------


def test_fresh_usage_from_a_verified_account_is_known():
    # Arrange
    usage = _usage(age_seconds=10)
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.state == KNOWN


def test_known_reading_carries_the_percentage():
    # Arrange
    usage = _usage(age_seconds=10)
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.pct_7d == 3.0


def test_known_reading_is_countable_in_the_fleet_average():
    # Arrange
    usage = _usage(age_seconds=10)
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.countable


# ---------------------------------------------------------------------------
# stale
# ---------------------------------------------------------------------------


def test_snapshot_older_than_the_refresh_window_is_stale():
    # Arrange — a day old, which is what the store actually held.
    usage = _usage(age_seconds=86_400)
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.state == STALE


def test_stale_reading_keeps_its_number_for_display():
    # Arrange — the operator can still judge a day-old figure, if TOLD it is.
    usage = _usage(age_seconds=86_400)
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.pct_7d == 3.0


def test_stale_reading_reports_its_age():
    # Arrange
    usage = _usage(age_seconds=86_400)
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.age_seconds == 86_400


def test_stale_reading_is_not_countable_in_the_fleet_average():
    # Arrange — averaging it in would launder the staleness away.
    usage = _usage(age_seconds=86_400)
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert not reading.countable


# ---------------------------------------------------------------------------
# unknown — identity
# ---------------------------------------------------------------------------


def test_usage_from_a_mismatched_credential_is_unknown():
    # Arrange — the exact incident: freshest possible number, wrong account.
    usage = _usage(age_seconds=1)
    # Act
    reading = classify_usage(usage, WRONG_OWNER, now=NOW)
    # Assert
    assert reading.state == UNKNOWN


def test_mismatched_credential_drops_the_percentage_entirely():
    # Arrange
    usage = _usage(age_seconds=1)
    # Act
    reading = classify_usage(usage, WRONG_OWNER, now=NOW)
    # Assert — not merely undrawn; unrepresentable.
    assert reading.pct_7d is None


def test_mismatch_reason_names_both_accounts():
    # Arrange
    usage = _usage(age_seconds=1)
    # Act
    reading = classify_usage(usage, WRONG_OWNER, now=NOW)
    # Assert
    assert "ywata1989@gmail.com" in reading.reason


def test_unverified_identity_makes_usage_unknown():
    # Arrange — sac could not check whose numbers these are.
    usage = _usage(age_seconds=1)
    # Act
    reading = classify_usage(usage, NOT_CHECKED, now=NOW)
    # Assert
    assert reading.state == UNKNOWN


def test_duplicate_account_usage_is_unknown():
    # Arrange — counting it again is what invented the phantom headroom.
    twin = AccountIdentity(
        name="ywatanabe-scitex-ai",
        state=VERIFIED,
        verified_email="ywata1989@gmail.com",
        duplicate_of="ywata1989-gmail-com",
    )
    # Act
    reading = classify_usage(_usage(age_seconds=1), twin, now=NOW)
    # Assert
    assert reading.reason == "same Anthropic account as ywata1989-gmail-com"


# ---------------------------------------------------------------------------
# unknown — data
# ---------------------------------------------------------------------------


def test_absent_usage_is_unknown():
    # Arrange
    usage = None
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.state == UNKNOWN


def test_untimestamped_usage_is_unknown_not_fresh():
    # Arrange — a figure that cannot be aged is the "fresh-looking but
    # arbitrarily old" shape this module exists to refuse.
    usage = {"used_pct_5h": 9.0, "used_pct_7d": 3.0}
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.state == UNKNOWN


def test_usage_with_no_figures_is_unknown():
    # Arrange
    usage = {"used_pct_5h": None, "used_pct_7d": None, "as_of": NOW.isoformat()}
    # Act
    reading = classify_usage(usage, GOOD, now=NOW)
    # Assert
    assert reading.state == UNKNOWN


# ---------------------------------------------------------------------------
# age formatting
# ---------------------------------------------------------------------------


def test_day_old_age_renders_in_days():
    # Arrange
    age = 90_000
    # Act
    rendered = format_age_short(age)
    # Assert
    assert rendered == "1d"


def test_unknown_age_renders_as_question_mark():
    # Arrange
    age = None
    # Act
    rendered = format_age_short(age)
    # Assert
    assert rendered == "?"
