"""Parser for ``spec.restart``."""

from __future__ import annotations

from .._types import RestartSpec


def parse_restart(spec: dict) -> RestartSpec:
    raw = spec.get("restart", {}) or {}
    backoff = raw.get("backoff", {}) or {}
    return RestartSpec(
        policy=raw.get("policy", "never"),
        max_retries=raw.get("max_retries", 3),
        backoff_initial=backoff.get("initial", 30),
        backoff_max=backoff.get("max", 300),
        backoff_multiplier=backoff.get("multiplier", 2),
    )
