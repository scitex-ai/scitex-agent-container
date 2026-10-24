"""Optional per-project worktree policy controls (``spec.worktree``).

The fleet default stays the bundled manifest: the authority checkout sits
on ``develop`` and only linked worktrees are writable. A project whose
main checkout lives on another branch (e.g. ``main``) opts in per spec::

    spec:
      workdir: ...
      worktree:
        authority_branch: main

Absent (or empty) ``authority_branch`` means "no opinion" — the manifest
default applies unchanged, so every existing spec keeps today's behaviour
without declaring anything new (the same posture ``residency`` and
``to_home_layers`` took). The block is therefore absent from the
explicit-required map in ``config._explicit_fields`` deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "WorktreeSpec",
    "parse_spec_worktree",
    "validate_worktree",
]


@dataclass(frozen=True)
class WorktreeSpec:
    """Per-project worktree controls. Empty = manifest default."""

    authority_branch: str = ""


def parse_spec_worktree(spec: Mapping) -> WorktreeSpec:
    """Read ``spec.worktree`` without changing behaviour when absent."""
    raw = spec.get("worktree")
    if raw is None:
        return WorktreeSpec()
    if not isinstance(raw, Mapping):
        raise ValueError(
            "spec.worktree must be a mapping, "
            f"got {type(raw).__name__}: {raw!r}"
        )
    unknown = sorted(set(map(str, raw)) - {"authority_branch"})
    if unknown:
        raise ValueError(
            f"spec.worktree has unknown fields: {unknown}. "
            "The only supported key is 'authority_branch'."
        )
    branch = raw.get("authority_branch", "")
    if branch is None:
        return WorktreeSpec()
    if not isinstance(branch, str):
        raise ValueError(
            "spec.worktree.authority_branch must be a string, "
            f"got {type(branch).__name__}: {branch!r}"
        )
    branch = branch.strip()
    return WorktreeSpec(authority_branch=branch)


def validate_worktree(spec: dict) -> list[str]:
    """Return ``spec.worktree`` shape errors (``validate_raw`` surface)."""
    errors: list[str] = []
    raw = spec.get("worktree")
    if raw is None:
        return errors
    if not isinstance(raw, dict):
        errors.append(
            f"spec.worktree must be a mapping; got {type(raw).__name__}"
        )
        return errors
    unknown = sorted(set(raw) - {"authority_branch"})
    for key in unknown:
        errors.append(
            f"Unknown spec.worktree field '{key}'. "
            "The only supported key is 'authority_branch'."
        )
    branch = raw.get("authority_branch")
    if branch is not None and not isinstance(branch, str):
        errors.append(
            "spec.worktree.authority_branch must be a string, "
            f"got {type(branch).__name__}"
        )
    return errors
