"""Harness-neutral delegated-worker policy."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_CONCURRENT_CHILDREN = 2
MAX_CONCURRENT_CHILDREN = 8

__all__ = [
    "DEFAULT_MAX_CONCURRENT_CHILDREN",
    "DelegationSpec",
    "MAX_CONCURRENT_CHILDREN",
]


@dataclass
class DelegationSpec:
    """Bounds for child work created by an agent harness.

    Permission remains in :class:`LineageSpec`; this block only controls the
    width and filesystem isolation of work that permission allows.
    """

    max_concurrent_children: int = DEFAULT_MAX_CONCURRENT_CHILDREN
    worktree_isolation: bool = True
