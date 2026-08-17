"""A STALE quota snapshot is not evidence — it routes into a refresh.

MEASURED 2026-08-17, scitex-hub on scitex-compute-03. Its quota cache was
present, well-formed, and 23 hours old. The boot decision asked only "does a
cache exist", so the armed path trusted it and the picker read the pinned
account's previous-day percentages — 7d=15% — as evidence the pin was fine.
The account was at 7d=100%, capped until Aug 22. hub answered "You've hit
your weekly limit" on every turn while the restart reported success.
Refreshing that one cache revived it: the picker then chose a 7d=8% account
by itself. The selector was never wrong; it was fed a day-old number and had
no way to know.

This is the SECOND time that failure shape reached production — the module's
own docstring records scitex-02 on 2026-08-06, an agent booting onto a
d7=100% account while startup reported success. That fix armed the gate when
the cache was ABSENT. A STALE cache walks straight past an absence check,
which is why it recurred.

No mocks: each test writes a REAL cache file with a REAL `written_at` epoch
and reads it back through the production resolver. Age is supplied via the
``now`` seam rather than by sleeping.
"""

from __future__ import annotations

import json

import pytest

from scitex_agent_container._lifecycle._quota_evidence import (
    QUOTA_EVIDENCE_MAX_AGE_S,
    _has_fresh_quota_evidence,
)

_NOW = 1_786_000_000.0


def _write_cache(tmp_path, *, written_at, accounts=None):
    path = tmp_path / "quota-cache.json"
    path.write_text(
        json.dumps(
            {
                "written_at": written_at,
                "accounts": accounts
                if accounts is not None
                else {"acct": {"short": "a", "h5": 0.0, "d7": 15.0, "ttl_h": 6.0}},
            }
        )
    )
    return path


def test_a_cache_written_now_is_evidence(tmp_path):
    """Positive control: the check can return True at all."""
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is True


def test_a_day_old_cache_is_not_evidence(tmp_path):
    """hub's exact condition: present, well-formed, 23 hours old."""
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW - 23 * 3600)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


def test_a_cache_just_inside_the_window_is_evidence(tmp_path):
    """Boundary, from the literal side — not computed from the constant.

    Deriving the boundary from QUOTA_EVIDENCE_MAX_AGE_S on both sides would
    keep passing if the constant were set to a year.
    """
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW - 3599)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is True


def test_a_cache_just_outside_the_window_is_not_evidence(tmp_path):
    # Arrange
    path = _write_cache(tmp_path, written_at=_NOW - 3601)
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


def test_an_absent_cache_is_not_evidence(tmp_path):
    # Arrange
    missing = tmp_path / "nope" / "quota-cache.json"
    # Act
    fresh = _has_fresh_quota_evidence(missing, now=_NOW)
    # Assert
    assert fresh is False


def test_an_undated_cache_is_not_evidence(tmp_path):
    """No `written_at` means the age is unknowable, and unknown is not OK."""
    # Arrange
    path = tmp_path / "quota-cache.json"
    path.write_text(json.dumps({"accounts": {"acct": {"h5": 0.0, "d7": 15.0}}}))
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


def test_an_unparseable_cache_is_not_evidence(tmp_path):
    """A corrupt cache must fail toward refreshing, never toward launching."""
    # Arrange
    path = tmp_path / "quota-cache.json"
    path.write_text("{not json")
    # Act
    fresh = _has_fresh_quota_evidence(path, now=_NOW)
    # Assert
    assert fresh is False


def test_the_window_is_not_generous_enough_to_hide_a_days_drift():
    """The constant must be well under the drift that caused the incident.

    hub's cache was 23h old. A window anywhere near that would have accepted
    it. Asserted against a literal so widening the constant to paper over a
    flaky refresher fails here rather than silently restoring the bug.
    """
    # Arrange
    a_day = 24 * 3600
    # Act
    window = QUOTA_EVIDENCE_MAX_AGE_S
    # Assert
    assert window <= a_day / 4
