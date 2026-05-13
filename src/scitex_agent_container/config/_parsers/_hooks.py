"""Parser for ``spec.hooks``."""

from __future__ import annotations

from ._helpers import HOOK_KEYS


def parse_hooks(spec: dict) -> dict[str, list[str]]:
    raw = spec.get("hooks", {}) or {}
    return {key: list(raw.get(key, []) or []) for key in HOOK_KEYS}
