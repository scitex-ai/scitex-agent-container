"""Dereference-copy a ``to_home/`` symlink to its real content.

Isolation invariant
--------------------
SAC's value is apptainer/capsule isolation: an agent must be
reproducible from its definition (spec + ``to_home/``) ALONE, and the
runtime must NEVER auto-read host state. The single materialization
rule for symlinks is therefore: **resolve every symlink to its real
target content** so the container ``$HOME`` holds only real,
self-contained files — closed to apptainer regardless of the host
filesystem layout.

A symlink under ``to_home/`` is the operator's *explicit* opt-in to
pull host content into the definition (e.g.
``_shared/to_home/.claude/skills -> ~/.claude/skills``). That is
explicit-pass, which is fine; the link is resolved to real content at
deploy time. A symlink whose target cannot be resolved (dangling) is a
real defect in the definition and hard-aborts the deploy via
:class:`DanglingToHomeSymlinkError`.

This module owns ONLY the per-symlink primitive; the traversal/overlay
orchestration lives in :mod:`_to_home`.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class DanglingToHomeSymlinkError(RuntimeError):
    """A ``to_home/`` symlink points at a target that cannot be resolved.

    The definition (spec + ``to_home/``) is the sole source of truth and
    every symlink is dereference-copied to its real content at deploy
    time. A symlink whose target does not exist is a real defect in the
    definition — materializing a dangling link (or silently skipping it)
    would produce an agent that is not reproducible from its definition.
    The deploy hard-aborts so the operator fixes the link instead.
    """


def deref_copy_symlink(src: Path, dst: Path) -> None:
    """Copy the REAL content a ``to_home/`` symlink points at into ``dst``.

    The symlink ``src`` is resolved to its real target (following chained
    links and ``..`` segments). The resolved content is copied so the
    destination is a real file or a real directory tree — never a
    symlink. Nested symlinks inside a resolved directory are dereferenced
    too (``copytree(symlinks=False)``), so no host path leaks into the
    container view.

    Excluded subtrees (see :mod:`.._workdir._walk_exclusions`) are SKIPPED via
    the ``ignore`` callable handed to ``shutil.copytree`` — most
    importantly ``worktrees/`` directories anywhere in the resolved
    tree. Without this, a baseline symlink like
    ``_shared/to_home/.claude/skills -> ~/.claude/skills`` would
    transitively pull every git worktree nested under the host's
    ``~/.claude/`` into the container overlay at start time — one of
    the two SAC-side walkers behind the original F-CS8 outage class
    (corrected 2026-06-04: Claude Code does NOT itself recursively
    walk ``~/.claude`` at startup; the bloat path was the
    ``to_home`` deref-copy and the ``_workdir._audit`` walk).

    Idempotent: an existing ``dst`` (file, dir, or symlink) is removed
    before the copy so repeated deploys always land current content.

    Raises
    ------
    DanglingToHomeSymlinkError
        If the symlink target cannot be resolved to an existing path.
        The message names the symlink path, its literal target, the
        resolved path, and what to do.
    """
    from .._workdir._walk_exclusions import copytree_ignore

    literal_target = os.readlink(src)
    resolved = src.resolve(strict=False)
    if not resolved.exists():
        raise DanglingToHomeSymlinkError(
            f"Dangling to_home symlink: {src} -> {literal_target} "
            f"(resolved to {resolved}, which does not exist). The agent "
            "definition is the sole source of truth and every symlink is "
            "resolved to real content at deploy time. Fix the link to "
            "point at content that exists on this host, or remove it from "
            "the definition."
        )
    _replace_dst(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if resolved.is_dir():
        shutil.copytree(resolved, dst, symlinks=False, ignore=copytree_ignore())
    else:
        shutil.copy2(resolved, dst, follow_symlinks=True)
    logger.info(
        "to_home: resolved symlink %s -> %s (real content from %s)",
        dst,
        literal_target,
        resolved,
    )


def _replace_dst(dst: Path) -> None:
    """Remove ``dst`` (dir, file, or symlink) if it exists.

    Leaves a clean slot for the new resolved copy. A symlink (including
    one whose target no longer exists) is unlinked; a real directory is
    recursively removed. Idempotent — no-op when ``dst`` is absent.
    """
    if dst.is_symlink():
        dst.unlink()
        return
    if not dst.exists():
        return
    if dst.is_dir():
        shutil.rmtree(dst)
    else:
        dst.unlink()


__all__ = [
    "DanglingToHomeSymlinkError",
    "deref_copy_symlink",
]
