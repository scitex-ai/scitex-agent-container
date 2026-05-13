"""Parser for ``spec.health``."""

from __future__ import annotations

from .._types import HealthSpec


def parse_health(spec: dict) -> HealthSpec:
    raw = spec.get("health", {}) or {}
    return HealthSpec(
        enabled=raw.get("enabled", False),
        interval=raw.get("interval", 30),
        timeout=raw.get("timeout", 5),
        method=raw.get("method", "multiplexer-alive"),
    )
