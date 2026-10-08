"""Typed Fast admission for an explicitly selected native OpenAI engine."""

from __future__ import annotations


def validate_service_tier(config, engine=None) -> None:
    """Reject an unsupported tier or a harness/provider that cannot honor it."""
    selected = engine if engine is not None else config
    tier = getattr(selected, "service_tier", "")
    if tier in (None, ""):
        return
    harness = getattr(selected, "harness", None) or getattr(config, "harness", "")
    if tier != "fast":
        raise ValueError("service_tier must be 'fast' or omitted")
    if harness != "codex" or getattr(selected, "subscription_provider", "") != "openai":
        raise ValueError(
            "service_tier requires the Codex harness and an explicit OpenAI subscription engine"
        )
