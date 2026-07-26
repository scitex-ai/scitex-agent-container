"""Tests for Claude transcript list-price estimation."""

from __future__ import annotations

from scitex_agent_container._account.claude_pricing import (
    estimate_message_cost_usd,
)


def _usage(**overrides):
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 2_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
        "service_tier": "standard",
        "speed": "standard",
    }
    usage.update(overrides)
    return usage


def test_opus_48_prices_each_token_class() -> None:
    # Arrange
    usage = _usage()
    # Act
    cost = estimate_message_cost_usd(usage, "claude-opus-4-8")
    # Assert
    assert cost == 46.75


def test_sonnet_5_uses_promotional_price_before_september() -> None:
    # Arrange
    usage = _usage()
    # Act
    cost = estimate_message_cost_usd(
        usage,
        "claude-sonnet-5",
        timestamp="2026-07-26T12:00:00Z",
    )
    # Assert
    assert cost == 18.7


def test_sonnet_5_uses_standard_price_from_september() -> None:
    # Arrange
    usage = _usage()
    # Act
    cost = estimate_message_cost_usd(
        usage,
        "claude-sonnet-5",
        timestamp="2026-09-01T00:00:00Z",
    )
    # Assert
    assert cost == 28.05


def test_dated_haiku_model_matches_family() -> None:
    # Arrange
    usage = _usage()
    # Act
    cost = estimate_message_cost_usd(usage, "claude-haiku-4-5-20251001")
    # Assert
    assert cost == 9.35


def test_missing_cache_duration_breakdown_is_unpriced() -> None:
    # Arrange
    usage = _usage(cache_creation={})
    # Act
    cost = estimate_message_cost_usd(usage, "claude-opus-5")
    # Assert
    assert cost is None


def test_unknown_model_is_unpriced() -> None:
    # Arrange
    usage = _usage()
    # Act
    cost = estimate_message_cost_usd(usage, "claude-future-99")
    # Assert
    assert cost is None


def test_us_inference_geo_applies_published_multiplier() -> None:
    # Arrange
    usage = _usage(inference_geo="us")
    # Act
    cost = estimate_message_cost_usd(usage, "claude-opus-5")
    # Assert
    assert cost == 51.425000000000004


def test_zero_token_synthetic_record_costs_zero() -> None:
    # Arrange
    usage = _usage(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation={},
    )
    # Act
    cost = estimate_message_cost_usd(usage, "<synthetic>")
    # Assert
    assert cost == 0.0
