"""Parser for ``spec.a2a`` (Agent-to-Agent transport)."""

from __future__ import annotations


def parse_a2a(spec: dict) -> "A2ASpec":  # noqa: F821
    """Parse spec.a2a into an :class:`A2ASpec`. Empty if absent."""
    from .._types import A2ASpec

    raw = spec.get("a2a", {}) or {}
    port = raw.get("port")
    return A2ASpec(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(port) if port is not None else None,
    )
