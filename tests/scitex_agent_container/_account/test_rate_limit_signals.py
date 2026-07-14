"""Tests for the rate-limit signal taxonomy (task #13, op-2026-06-12-13).

Operator directive op-2026-06-12-13 (Telegram 12676 → 12677): the
signal taxonomy is the foundation of the auto-rotate classifier.
These tests pin the public surface — enum values (which serialise
into observability events) MUST stay stable, ``classify_http_status``
MUST NOT silently re-map a future status to a known one, and the
textual scanner MUST tolerate a malformed user pattern without
crashing the runner.

Test style (STX-TQ002 / STX-TQ007): explicit ``# Arrange`` / ``# Act``
/ ``# Assert`` markers each on their own line, in order; one logical
assertion per test. No mocks.
"""

from __future__ import annotations

import pytest
from scitex_agent_container._account.rate_limit_signals import (
    DEFAULT_TEXTUAL_PATTERNS,
    RateLimitSignal,
    classify_http_status,
    detect_signal_from_text,
    scan_textual_cap_markers,
)

# ---------------------------------------------------------------------------
# RateLimitSignal enum — value contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member, expected_value",
    [
        (RateLimitSignal.USAGE_PCT_5H, "usage_pct_5h"),
        (RateLimitSignal.USAGE_PCT_7D, "usage_pct_7d"),
        (RateLimitSignal.HTTP_429, "http_429"),
        (RateLimitSignal.HTTP_403, "http_403"),
        (RateLimitSignal.HTTP_529, "http_529"),
        (RateLimitSignal.TEXTUAL_MATCH, "textual_match"),
        (RateLimitSignal.AUTH_EVENT, "auth_event"),
    ],
)
def test_rate_limit_signal_member_serialises_to_documented_string(
    member, expected_value
):
    # Arrange
    actual_value = member.value
    # Act
    matches = actual_value == expected_value
    # Assert
    assert matches is True


# ---------------------------------------------------------------------------
# classify_http_status — HTTP-status → signal mapping
# ---------------------------------------------------------------------------


def test_classify_http_status_429_returns_http_429_signal():
    # Arrange
    status = 429
    # Act
    signal = classify_http_status(status)
    # Assert
    assert signal is RateLimitSignal.HTTP_429


def test_classify_http_status_403_returns_http_403_signal():
    # Arrange
    status = 403
    # Act
    signal = classify_http_status(status)
    # Assert
    assert signal is RateLimitSignal.HTTP_403


def test_classify_http_status_529_returns_http_529_signal():
    # Arrange
    status = 529
    # Act
    signal = classify_http_status(status)
    # Assert
    assert signal is RateLimitSignal.HTTP_529


def test_classify_http_status_200_returns_none():
    # Arrange
    status = 200
    # Act
    signal = classify_http_status(status)
    # Assert
    assert signal is None


def test_classify_http_status_500_returns_none():
    # Arrange
    status = 500
    # Act
    signal = classify_http_status(status)
    # Assert
    assert signal is None


def test_classify_http_status_future_530_returns_none_not_silent_429():
    # Arrange
    status = 530
    # Act
    signal = classify_http_status(status)
    # Assert
    assert signal is None


# ---------------------------------------------------------------------------
# scan_textual_cap_markers — text-blob → (signal, pattern) | None
# ---------------------------------------------------------------------------


def test_scan_textual_cap_markers_empty_string_returns_none():
    # Arrange
    text = ""
    # Act
    result = scan_textual_cap_markers(text)
    # Assert
    assert result is None


def test_scan_textual_cap_markers_whitespace_only_returns_none():
    # Arrange
    text = "   \n\t  \n"
    # Act
    result = scan_textual_cap_markers(text)
    # Assert
    assert result is None


def test_scan_textual_cap_markers_weekly_limit_returns_textual_match_signal():
    # Arrange
    text = "You've hit your weekly limit · resets 2026-06-18T05:00Z"
    # Act
    result = scan_textual_cap_markers(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.TEXTUAL_MATCH


def test_scan_textual_cap_markers_weekly_limit_returns_matching_pattern_string():
    # Arrange
    text = "You've hit your weekly limit · resets 2026-06-18T05:00Z"
    # Act
    result = scan_textual_cap_markers(text)
    # Assert
    assert result is not None and result[1] == "hit your weekly limit"


def test_scan_textual_cap_markers_quota_exhausted_returns_quota_pattern():
    # Arrange
    text = "quota exhausted by the org"
    # Act
    result = scan_textual_cap_markers(text)
    # Assert
    assert result is not None and result[1] == "quota (?:exhausted|exceeded|reached)"


def test_scan_textual_cap_markers_is_case_insensitive():
    # Arrange
    text = "ERROR: WEEKLY LIMIT exceeded for this account"
    # Act
    result = scan_textual_cap_markers(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.TEXTUAL_MATCH


def test_scan_textual_cap_markers_miss_returns_none():
    # Arrange
    text = "200 OK — request completed in 12ms"
    # Act
    result = scan_textual_cap_markers(text)
    # Assert
    assert result is None


def test_scan_textual_cap_markers_skips_malformed_user_pattern_without_raising():
    # Arrange
    text = "quota exhausted by the org"
    patterns = ("(unclosed", r"quota (?:exhausted|exceeded|reached)")
    # Act
    result = scan_textual_cap_markers(text, patterns=patterns)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.TEXTUAL_MATCH


# ---------------------------------------------------------------------------
# DEFAULT_TEXTUAL_PATTERNS — immutability contract
# ---------------------------------------------------------------------------


def test_default_textual_patterns_is_a_tuple():
    # Arrange
    patterns = DEFAULT_TEXTUAL_PATTERNS
    # Act
    is_tuple = isinstance(patterns, tuple)
    # Assert
    assert is_tuple is True


# ---------------------------------------------------------------------------
# detect_signal_from_text — REACTIVE entry point (task #13 wiring)
# ---------------------------------------------------------------------------


def test_detect_signal_from_text_empty_string_returns_none():
    # Arrange
    text = ""
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is None


def test_detect_signal_from_text_unrelated_text_returns_none():
    # Arrange
    text = "connection reset by peer"
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is None


def test_detect_signal_from_text_anthropic_rate_limit_error_type():
    # Arrange — the real Anthropic JSON error body shape.
    text = '{"type":"error","error":{"type":"rate_limit_error","message":"..."}}'
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.HTTP_429


def test_detect_signal_from_text_anthropic_overloaded_error_type():
    # Arrange
    text = '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.HTTP_529


def test_detect_signal_from_text_bare_429_word_boundary():
    # Arrange
    text = "ProcessError: API Error: 429 Too Many Requests"
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.HTTP_429


def test_detect_signal_from_text_bare_529_word_boundary():
    # Arrange
    text = "ProcessError: API Error: 529 Overloaded"
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.HTTP_529


def test_detect_signal_from_text_bare_403_word_boundary():
    # Arrange
    text = "ProcessError: API Error: 403 Forbidden — organization rate limit"
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.HTTP_403


def test_detect_signal_from_text_does_not_misfire_on_unrelated_number():
    # Arrange — "429" does NOT appear anywhere here; only an unrelated
    # numeric substring risk-case ("42900" contains "429" but not as a
    # word-boundary token).
    text = "processed 42900 rows in 12ms"
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is None


def test_detect_signal_from_text_falls_back_to_textual_cap_marker():
    # Arrange — no HTTP-status signature present, only the textual marker.
    text = "You've hit your weekly limit · resets 2026-06-18T05:00Z"
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.TEXTUAL_MATCH


def test_detect_signal_from_text_status_signature_wins_over_textual_order():
    # Arrange — rate_limit_error appears first in the scan order; a
    # textual marker later in the SAME text must not override it.
    text = (
        '{"type":"error","error":{"type":"rate_limit_error"}} '
        "— quota exceeded"
    )
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[0] is RateLimitSignal.HTTP_429


def test_detect_signal_from_text_returns_the_matched_pattern():
    # Arrange
    text = '{"error":{"type":"overloaded_error"}}'
    # Act
    result = detect_signal_from_text(text)
    # Assert
    assert result is not None and result[1] == "overloaded_error"
