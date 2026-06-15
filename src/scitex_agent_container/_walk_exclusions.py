"""Shared exclusion predicate for SAC's heavy filesystem walks.

Two on-start walkers in SAC enumerate large trees synchronously:

* :func:`_workdir_audit._walk_size_and_count` — the F-CS8 ``.claude/``
  bloat warning. Walks the full ``<workdir>/.claude/`` to compute
  totals; runs on every agent start before ``claude`` spawns.
* :func:`runtimes._symlink_resolve.deref_copy_symlink` — the
  ``to_home`` symlink-dereference step. ``shutil.copytree(...,
  symlinks=False)`` copies the whole resolved target tree into the
  container overlay; a baseline symlink like
  ``_shared/to_home/.claude/skills -> ~/.claude/skills`` transitively
  pulls in anything reachable under that target.

Both walkers used to descend into ``worktrees/`` subtrees, which are
full git checkouts. A single bloated ``.claude/worktrees/agent-*``
(hundreds of files × many worktrees) was enough to make the audit and
the deref-copy take long enough that the agent appeared "alive but
unresponsive" at boot — the original F-CS8 fleet outage class observed
2026-06-03.

(For the avoidance of doubt: Claude Code does NOT itself recursively
walk ``~/.claude`` at startup — the original "SDK boot scan" framing
of F-CS8 was wrong. The bloat path is the SAC-side walkers above.)

This module centralises the exclusion list so future walkers inherit
the same prune behaviour without each call site re-discovering which
directories to skip.

Semantics
---------
The exclusion is BASENAME-based: any directory whose IMMEDIATE name
matches one of the excluded basenames is pruned. Position-agnostic — a
``worktrees/`` directly under ``.claude/`` is pruned, and so is one
nested deeper. The exclusion is NOT applied to the *root* of a walk
(so a probe explicitly targeted at ``<workdir>/.claude/worktrees/``
for per-bucket telemetry still descends into it).

Callers wire the predicate into their walker in the natural idiom:

* ``os.walk`` — call :func:`prune_walk_dirnames` on the yielded
  ``dirnames`` list in place; ``os.walk`` then skips the pruned
  subtrees on its next iteration.
* ``shutil.copytree`` — pass :func:`copytree_ignore` (a factory
  returning the standard ``(src, names) -> ignored_set`` callable)
  as the ``ignore=`` kwarg.
"""

from __future__ import annotations

from typing import Callable, Iterable

# Basenames of directories that any heavy fleet walk should PRUNE.
# Currently a single entry: ``worktrees``. Git worktrees are full
# checkouts whose enumeration cost dominates any walker without
# contributing useful discovery to the walker's downstream consumer
# (claude-agent-sdk doesn't read them; ``to_home`` materialization
# must not pull them transitively via symlink dereference).
#
# Add to this set as new heavy-walk traps surface in the wild — every
# centrally-registered walker inherits the new exclusion automatically.
_EXCLUDED_WALK_DIR_BASENAMES: frozenset[str] = frozenset({"worktrees"})


def is_excluded_walk_dir(name: str) -> bool:
    """True iff a directory basename should be PRUNED from heavy walks.

    Args:
        name: A single directory basename (no path separators).

    Returns:
        ``True`` if walkers should skip the directory; ``False``
        otherwise. Matching is exact: ``"worktrees"`` matches but
        ``"old-worktrees"`` and ``"worktrees-archive"`` do not.
    """
    return name in _EXCLUDED_WALK_DIR_BASENAMES


def prune_walk_dirnames(dirnames: list[str]) -> None:
    """In-place mutation: remove excluded basenames from an ``os.walk``
    ``dirnames`` list.

    Mirrors the standard ``os.walk`` idiom — call from inside the walk
    loop so the walker does not descend the pruned subtrees on its
    next iteration::

        for dirpath, dirnames, filenames in os.walk(root):
            prune_walk_dirnames(dirnames)
            ...

    Mutates ``dirnames`` in place because that is what ``os.walk``
    contracts on — reassigning the local name (``dirnames =
    [...]``) does not affect the iterator.
    """
    dirnames[:] = [d for d in dirnames if not is_excluded_walk_dir(d)]


def copytree_ignore() -> Callable[[str, Iterable[str]], set[str]]:
    """Return a callable suitable for ``shutil.copytree(..., ignore=)``.

    The returned callable takes ``(src, names)`` per the ``copytree``
    contract — where ``names`` is the directory entries currently
    visible at ``src`` — and returns the subset that should be skipped.
    Only directory names matching the shared exclusion are skipped;
    files and other entries are never touched.
    """

    def _ignore(_src: str, names: Iterable[str]) -> set[str]:
        return {n for n in names if is_excluded_walk_dir(n)}

    return _ignore


__all__ = [
    "is_excluded_walk_dir",
    "prune_walk_dirnames",
    "copytree_ignore",
]
