"""Tests for ``_state._meta.quota`` — quota + account-identity helpers.

PS-202 src-tests mirror. ``_quota_from_statusline`` is pure (dict
input → dict output); the higher-level ``collect_quota_and_account``
combines several IO paths but is tested only via real env / real
file behaviours below.
"""

from __future__ import annotations

from scitex_agent_container._state._meta.quota import (
    _quota_from_statusline,
    _read_account_identity,
    collect_quota_and_account,
)

# --- _quota_from_statusline (pure dict transform) -----------------------


def test_quota_from_statusline_returns_empty_for_empty_input():
    # Arrange
    sl: dict = {}
    # Act
    out = _quota_from_statusline(sl)
    # Assert
    assert out == {}


def test_quota_from_statusline_rounds_5h_used_percentage():
    # Arrange
    sl = {"rate_limits": {"five_hour": {"used_percentage": 12.345}}}
    # Act
    out = _quota_from_statusline(sl)
    # Assert
    assert out["quota_5h_used_pct"] == 12.3


def test_quota_from_statusline_rounds_7d_used_percentage():
    # Arrange
    sl = {"rate_limits": {"seven_day": {"used_percentage": 67.89}}}
    # Act
    out = _quota_from_statusline(sl)
    # Assert
    assert out["quota_7d_used_pct"] == 67.9


def test_quota_from_statusline_surfaces_reset_at_for_5h():
    # Arrange
    sl = {
        "rate_limits": {
            "five_hour": {"used_percentage": 1.0, "resets_at": "2026-01-01T00:00:00Z"}
        }
    }
    # Act
    out = _quota_from_statusline(sl)
    # Assert
    assert out["quota_5h_reset_at"] == "2026-01-01T00:00:00Z"


def test_quota_from_statusline_marks_from_cache_false():
    # Arrange
    sl = {"rate_limits": {"five_hour": {"used_percentage": 1.0}}}
    # Act
    out = _quota_from_statusline(sl)
    # Assert
    assert out["quota_from_cache"] is False


# --- _read_account_identity (defaults shape) ----------------------------


def test_read_account_identity_has_stable_key_set():
    # Arrange
    expected_keys = {
        "account_email",
        "account_plan_label",
        "account_subscription_type",
        "account_rate_limit_tier",
        "account_organization_name",
        "account_uuid",
        "oauth_expires_at",
        "installed_plugins",
        "status_line_command",
    }
    # Act
    out = _read_account_identity()
    # Assert
    assert expected_keys.issubset(out.keys())


# --- collect_quota_and_account (integration, statusline path) ----------


def test_collect_quota_and_account_uses_statusline_when_provided():
    # Arrange
    sl = {"rate_limits": {"five_hour": {"used_percentage": 42.0}}}
    # Act
    out = collect_quota_and_account(sl)
    # Assert
    assert out["quota_5h_used_pct"] == 42.0


def test_collect_quota_and_account_preserves_account_key_shape():
    # Arrange
    sl = {"rate_limits": {"five_hour": {"used_percentage": 0.0}}}
    # Act
    out = collect_quota_and_account(sl)
    # Assert
    assert "account_email" in out
