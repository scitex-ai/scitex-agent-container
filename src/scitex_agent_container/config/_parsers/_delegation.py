"""Parse the harness-neutral ``spec.delegation`` block."""

from __future__ import annotations

from .._delegation_types import (
    DEFAULT_MAX_CONCURRENT_CHILDREN,
    MAX_CONCURRENT_CHILDREN,
    DelegationSpec,
)

__all__ = ["parse_delegation"]


def parse_delegation(spec: dict) -> DelegationSpec:
    raw = spec.get("delegation")
    if raw is None:
        return DelegationSpec()
    if not isinstance(raw, dict):
        raise ValueError(
            "spec.delegation must be a mapping, got "
            f"{type(raw).__name__}: {raw!r}"
        )
    unknown = set(raw) - {"max_concurrent_children", "worktree_isolation"}
    if unknown:
        raise ValueError(
            f"spec.delegation contains unknown keys {sorted(unknown)}; valid keys "
            "are 'max_concurrent_children' and 'worktree_isolation'."
        )
    maximum = raw.get(
        "max_concurrent_children", DEFAULT_MAX_CONCURRENT_CHILDREN
    )
    if (
        type(maximum) is not int
        or maximum <= 0
        or maximum > MAX_CONCURRENT_CHILDREN
    ):
        raise ValueError(
            "spec.delegation.max_concurrent_children must be an integer between "
            f"1 and {MAX_CONCURRENT_CHILDREN}, "
            f"got {type(maximum).__name__}: {maximum!r}"
        )
    isolation = raw.get("worktree_isolation", True)
    if not isinstance(isolation, bool):
        raise ValueError(
            "spec.delegation.worktree_isolation must be a boolean, got "
            f"{type(isolation).__name__}: {isolation!r}"
        )
    return DelegationSpec(
        max_concurrent_children=maximum,
        worktree_isolation=isolation,
    )
