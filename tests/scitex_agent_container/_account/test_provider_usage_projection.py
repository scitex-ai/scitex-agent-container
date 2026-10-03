"""Pure metric/privacy contracts, using actual provider payload shapes."""

from datetime import datetime, timedelta, timezone

import pytest

from scitex_agent_container._account.provider_usage_projection import (
    number,
    project,
    safe_snapshot,
    snapshot_state,
    window,
)

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
RESET = (NOW + timedelta(hours=5)).isoformat()


@pytest.mark.parametrize("value", [True, False, -1, float("nan"), float("inf"), "42", None])
def test_invalid_measurements_stay_unknown(value):
    # Arrange
    measurement = value
    # Act
    result = number(measurement)
    # Assert
    assert result is None


@pytest.mark.parametrize("stamp", [None, "garbage", "2026-10-03", (NOW + timedelta(seconds=1)).isoformat()])
def test_missing_or_future_snapshot_is_unknown(stamp):
    # Arrange
    fetched = stamp
    # Act
    state = snapshot_state(fetched, NOW)
    # Assert
    assert state == "unknown"


def test_old_snapshot_is_stale():
    # Arrange
    fetched = (NOW - timedelta(seconds=301)).isoformat()
    # Act
    state = snapshot_state(fetched, NOW)
    # Assert
    assert state == "stale"


def test_past_reset_does_not_claim_current_capacity():
    # Arrange
    reset = (NOW - timedelta(seconds=1)).isoformat()
    # Act
    result = window("rolling", 20, reset, NOW)
    # Assert
    assert result["state"] == "stale"


def test_malformed_reset_is_unknown():
    # Arrange
    reset = "not-a-reset"
    # Act
    result = window("rolling", 20, reset, NOW)
    # Assert
    assert result["state"] == "unknown"


def test_go_monthly_exhaustion_retains_exact_percent():
    # Arrange
    payload = {"quota": {"usage": {"monthly": {"percent": 100, "resetsAt": RESET}}}}
    # Act
    result = project("opencode-go", payload, NOW)
    # Assert
    assert result["windows"][2]["remaining"] == 0


def test_go_missing_windows_are_represented():
    # Arrange
    payload = {"quota": {"usage": {}}}
    # Act
    result = project("opencode-go", payload, NOW)
    # Assert
    assert [row["state"] for row in result["windows"]] == ["unknown"] * 3


def test_codex_quota_does_not_invent_costs():
    # Arrange
    payload = {"quota": {"rate_limit": {"primary_window": {
        "used_percent": 25, "reset_at": (NOW + timedelta(hours=5)).timestamp(),
        "limit_window_seconds": 18_000,
    }}}}
    # Act
    result = project("openai", payload, NOW)
    # Assert
    assert result["cost"] is None


def test_codex_window_keeps_provider_duration():
    # Arrange
    payload = {"quota": {"rate_limit": {"primary_window": {
        "used_percent": 25, "reset_at": (NOW + timedelta(hours=5)).timestamp(),
        "limit_window_seconds": 18_000,
    }}}}
    # Act
    result = project("openai", payload, NOW)
    # Assert
    assert result["windows"][0]["duration_seconds"] == 18_000


@pytest.fixture
def commandcode_usage():
    # Arrange
    payload = {"credits": {"credits": {"monthlyCredits": 12, "purchasedCredits": 0, "freeCredits": 0},
               "windowLimits": {"fiveHour": {"used": 1, "cap": 4, "resetAt": (NOW + timedelta(hours=5)).timestamp() * 1000}}},
               "cost": {"totalCost": 3.25}, "cost_since": (NOW - timedelta(days=30)).isoformat()}
    # Act
    return project("commandcode", payload, NOW)


def test_commandcode_credit_unit_is_usd(commandcode_usage):
    # Arrange
    result = commandcode_usage
    # Act
    currency = result["currency"]
    # Assert
    assert currency == "USD"


def test_commandcode_window_uses_actual_millisecond_reset(commandcode_usage):
    # Arrange
    result = commandcode_usage
    # Act
    reset = result["windows"][0]["resetAt"]
    # Assert
    assert reset == RESET


def test_commandcode_cost_keeps_explicit_period(commandcode_usage):
    # Arrange
    result = commandcode_usage
    # Act
    start = result["cost"]["window_start"]
    # Assert
    assert start == (NOW - timedelta(days=30)).isoformat()


def test_subscription_fee_is_not_derived_from_usage(commandcode_usage):
    # Arrange
    result = commandcode_usage
    # Act
    fee = result["subscription_fee"]
    # Assert
    assert fee is None


def test_cache_revalidation_removes_private_fields(commandcode_usage):
    # Arrange
    raw = {**commandcode_usage, "api_key": "synthetic-private", "org_id": "private-org"}
    # Act
    result = safe_snapshot(raw, NOW)
    # Assert
    assert set(result) == {"fetchedAt", "usage_state", "windows", "balances", "cost", "subscription_fee", "currency"}


def test_cache_with_only_expired_window_becomes_stale():
    # Arrange
    raw = project("opencode-go", {"quota": {"usage": {"rolling": {"percent": 20, "resetsAt": RESET}}}}, NOW)
    # Act
    result = safe_snapshot(raw, NOW + timedelta(hours=6))
    # Assert
    assert result["usage_state"] == "stale"


def test_cache_without_measurement_does_not_claim_current():
    # Arrange
    raw = {"fetchedAt": NOW.isoformat(), "usage_state": "known", "windows": None}
    # Act
    result = safe_snapshot(raw, NOW)
    # Assert
    assert result["usage_state"] == "unknown"
