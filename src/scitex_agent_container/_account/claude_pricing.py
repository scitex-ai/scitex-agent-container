"""Claude API-equivalent list-price estimates for transcript usage.

Prices are USD per million tokens in this order:
base input, 5-minute cache write, 1-hour cache write, cache read, output.
The table follows Anthropic's official pricing page, checked 2026-07-26:
https://platform.claude.com/docs/en/about-claude/pricing

These estimates do not represent Claude Pro/Max subscription charges.
"""

from __future__ import annotations

from typing import Any

PRICE_VERSION = "2026-07-26"
PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"

_STANDARD_PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5": (10.0, 12.5, 20.0, 1.0, 50.0),
    "claude-opus-5": (5.0, 6.25, 10.0, 0.5, 25.0),
    "claude-opus-4-8": (5.0, 6.25, 10.0, 0.5, 25.0),
    "claude-haiku-4-5": (1.0, 1.25, 2.0, 0.1, 5.0),
}
_SONNET_5_PROMO = (2.0, 2.5, 4.0, 0.2, 10.0)
_SONNET_5_STANDARD = (3.0, 3.75, 6.0, 0.3, 15.0)
_SONNET_5_STANDARD_FROM = "2026-09-01"
_FAST_OPUS = (10.0, 12.5, 20.0, 1.0, 50.0)


def _tokens(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _family(model: str, prefix: str) -> bool:
    return model == prefix or model.startswith(f"{prefix}-")


def _prices(
    model: str, usage: dict[str, Any], timestamp: str | None
) -> tuple[float, float, float, float, float] | None:
    speed = usage.get("speed")
    if speed not in (None, "standard", "fast"):
        return None
    if speed == "fast":
        if _family(model, "claude-opus-5") or _family(model, "claude-opus-4-8"):
            return _FAST_OPUS
        return None
    if _family(model, "claude-sonnet-5"):
        day = timestamp[:10] if isinstance(timestamp, str) else PRICE_VERSION
        return _SONNET_5_STANDARD if day >= _SONNET_5_STANDARD_FROM else _SONNET_5_PROMO
    match = ""
    for prefix in _STANDARD_PRICES:
        if _family(model, prefix) and len(prefix) > len(match):
            match = prefix
    return _STANDARD_PRICES.get(match)


def estimate_message_cost_usd(
    usage: dict[str, Any],
    model: str,
    *,
    timestamp: str | None = None,
) -> float | None:
    """Estimate one Claude assistant record at first-party global list price.

    Returns ``None`` for an unknown model/tier or when cache-write duration
    details are missing. Zero-token synthetic records return ``0.0``.
    """
    input_tokens = _tokens(usage, "input_tokens")
    output_tokens = _tokens(usage, "output_tokens")
    cache_read = _tokens(usage, "cache_read_input_tokens")
    cache_write = _tokens(usage, "cache_creation_input_tokens")
    total = input_tokens + output_tokens + cache_read + cache_write
    if total == 0:
        return 0.0
    service_tier = usage.get("service_tier")
    if service_tier not in (None, "standard"):
        return None
    prices = _prices(model, usage, timestamp)
    if prices is None:
        return None
    cache = usage.get("cache_creation")
    cache = cache if isinstance(cache, dict) else {}
    cache_write_5m = _tokens(cache, "ephemeral_5m_input_tokens")
    cache_write_1h = _tokens(cache, "ephemeral_1h_input_tokens")
    if cache_write_5m + cache_write_1h != cache_write:
        return None
    input_price, write_5m_price, write_1h_price, read_price, output_price = prices
    cost = (
        input_tokens * input_price
        + cache_write_5m * write_5m_price
        + cache_write_1h * write_1h_price
        + cache_read * read_price
        + output_tokens * output_price
    ) / 1_000_000
    if usage.get("inference_geo") == "us":
        cost *= 1.1
    return cost


__all__ = [
    "PRICE_SOURCE",
    "PRICE_VERSION",
    "estimate_message_cost_usd",
]
