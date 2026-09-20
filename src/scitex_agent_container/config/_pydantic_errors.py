"""Stable author-facing diagnostics for strict Pydantic spec models."""

from __future__ import annotations

from pydantic import ValidationError


def format_validation_error(exc: ValidationError, *, prefix: str) -> str:
    """Render validation failures with exact YAML paths and no model noise."""
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_context=False):
        location = ".".join(
            f"[{part}]" if isinstance(part, int) else str(part) for part in error["loc"]
        ).replace(".[", "[")
        path = f"{prefix}.{location}" if location else prefix
        lines.append(f"{path}: {error['msg']}")
    return "\n".join(lines)


__all__ = ["format_validation_error"]
