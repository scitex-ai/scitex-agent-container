"""Parser for ``spec.extensions`` (opaque pass-through)."""

from __future__ import annotations


def parse_extensions(spec: dict) -> dict:
    """Return ``spec.extensions`` verbatim (opaque pass-through)."""
    raw = spec.get("extensions", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}
