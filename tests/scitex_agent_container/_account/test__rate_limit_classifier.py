"""Tests for the rate-limit signal → Mode classifier (task #13, op-2026-06-12-13).

Operator directive op-2026-06-12-13 (Telegram 12676 → 12677): the
classifier decides BACKOFF (Mode A — transient, same account) vs
ROTATE (Mode B — sustained cap, move accounts) by joining the live
signal with the per-account usage% snapshot. The thresholds are
operator-locked (TG 12674):

* Proactive 5h usage >= 99% → ROTATE (95% is warn-only, not actioned)
* Proactive 7d usage >= 95% → ROTATE
* HTTP 429 / 403 reactive: ROTATE iff already in cap territory
  (5h >= 90% OR 7d >= 85%); else BACKOFF
* HTTP 529 (Anthropic overload): ALWAYS BACKOFF — server-capacity,
  not account cap
* TEXTUAL_MATCH / AUTH_EVENT: ALWAYS ROTATE — unambiguous cap

Test style (STX-TQ002 / STX-TQ007): explicit ``# Arrange`` / ``# Act``
/ ``# Assert`` markers each on their own line, in order; one logical
assertion per test. No mocks.
"""

from __future__ import annotations

import dataclasses

import pytest
from scitex_agent_container._account.rate_limit_classifier import (
    AccountUsageSnapshot,
    Mode,
    classify_rate_limit_signal,
)
from scitex_agent_container._account.rate_limit_signals import RateLimitSignal

# ---------------------------------------------------------------------------
# Mode enum — value contract
# ---------------------------------------------------------------------------


def test_mode_none_serialises_to_documented_string():
    # Arrange
    value = Mode.NONE.value
    # Act
    matches = value == "none"
    # Assert
    assert matches is True


def test_mode_backoff_serialises_to_documented_string():
    # Arrange
    value = Mode.BACKOFF.value
    # Act
    matches = value == "backoff"
    # Assert
    assert matches is True


def test_mode_rotate_serialises_to_documented_string():
    # Arrange
    value = Mode.ROTATE.value
    # Act
    matches = value == "rotate"
    # Assert
    assert matches is True


# ---------------------------------------------------------------------------
# AccountUsageSnapshot — defaults + frozen-ness
# ---------------------------------------------------------------------------


def test_account_usage_snapshot_defaults_to_zero_5h():
    # Arrange
    snapshot = AccountUsageSnapshot()
    # Act
    value = snapshot.used_pct_5h
    # Assert
    assert value == 0.0


def test_account_usage_snapshot_defaults_to_zero_7d():
    # Arrange
    snapshot = AccountUsageSnapshot()
    # Act
    value = snapshot.used_pct_7d
    # Assert
    assert value == 0.0


def test_account_usage_snapshot_is_frozen_against_mutation():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=10.0, used_pct_7d=20.0)
    # Act / Assert
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.used_pct_5h = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# USAGE_PCT_5H proactive signal
# ---------------------------------------------------------------------------


def test_usage_pct_5h_at_rotate_threshold_returns_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=99.0)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.USAGE_PCT_5H, snapshot)
    # Assert
    assert mode is Mode.ROTATE


def test_usage_pct_5h_below_rotate_threshold_returns_none():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=98.9)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.USAGE_PCT_5H, snapshot)
    # Assert
    assert mode is Mode.NONE


# ---------------------------------------------------------------------------
# USAGE_PCT_7D proactive signal
# ---------------------------------------------------------------------------


def test_usage_pct_7d_at_rotate_threshold_returns_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_7d=95.0)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.USAGE_PCT_7D, snapshot)
    # Assert
    assert mode is Mode.ROTATE


def test_usage_pct_7d_below_rotate_threshold_returns_none():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_7d=94.9)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.USAGE_PCT_7D, snapshot)
    # Assert
    assert mode is Mode.NONE


# ---------------------------------------------------------------------------
# HTTP_529 — Anthropic overload always BACKOFF
# ---------------------------------------------------------------------------


def test_http_529_at_high_usage_still_returns_backoff_not_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=99.0, used_pct_7d=99.0)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.HTTP_529, snapshot)
    # Assert
    assert mode is Mode.BACKOFF


def test_http_529_at_zero_usage_returns_backoff():
    # Arrange
    snapshot = AccountUsageSnapshot()
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.HTTP_529, snapshot)
    # Assert
    assert mode is Mode.BACKOFF


# ---------------------------------------------------------------------------
# HTTP_429 — reactive with 5h / 7d boundary tests
# ---------------------------------------------------------------------------


def test_http_429_at_5h_boundary_90pct_returns_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=90.0)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.HTTP_429, snapshot)
    # Assert
    assert mode is Mode.ROTATE


def test_http_429_below_both_boundaries_returns_backoff():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=89.9, used_pct_7d=84.9)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.HTTP_429, snapshot)
    # Assert
    assert mode is Mode.BACKOFF


def test_http_429_at_7d_boundary_85pct_returns_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=0.0, used_pct_7d=85.0)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.HTTP_429, snapshot)
    # Assert
    assert mode is Mode.ROTATE


# ---------------------------------------------------------------------------
# HTTP_403 — same boundary behavior as 429
# ---------------------------------------------------------------------------


def test_http_403_at_5h_boundary_returns_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=90.0)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.HTTP_403, snapshot)
    # Assert
    assert mode is Mode.ROTATE


def test_http_403_below_both_boundaries_returns_backoff():
    # Arrange
    snapshot = AccountUsageSnapshot(used_pct_5h=10.0, used_pct_7d=10.0)
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.HTTP_403, snapshot)
    # Assert
    assert mode is Mode.BACKOFF


# ---------------------------------------------------------------------------
# TEXTUAL_MATCH / AUTH_EVENT — always ROTATE
# ---------------------------------------------------------------------------


def test_textual_match_at_zero_usage_still_returns_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot()
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.TEXTUAL_MATCH, snapshot)
    # Assert
    assert mode is Mode.ROTATE


def test_auth_event_at_zero_usage_still_returns_rotate():
    # Arrange
    snapshot = AccountUsageSnapshot()
    # Act
    mode = classify_rate_limit_signal(RateLimitSignal.AUTH_EVENT, snapshot)
    # Assert
    assert mode is Mode.ROTATE
