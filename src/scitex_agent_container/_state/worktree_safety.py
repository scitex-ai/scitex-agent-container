"""Advisory predicate for "safe to ``git worktree prune`` this path?".

Lead-learnings/19 captured the silent destruction mode that motivated
this module: host-side ``git worktree prune`` walks the host's view of
``$GIT_DIR/worktrees/*/gitdir`` and removes any whose recorded path is
absent. Container-created worktrees record a path inside the container
namespace (e.g. ``/work/.worktrees/...``), which the host process does
NOT see, so the host concludes the worktree is orphaned and prunes
it — discarding the linked ``HEAD``, ``index``, and any unmerged
branch state along with it. Combined with a not-yet-rescued dirty
tree, that is silent loss of work.

PR #369 (``feat(lifecycle): fleet-default pre-stop rescue for dirty
worktrees``) closes the destruction window on the *write* side by
auto-committing dirty worktrees on capsule stop. This module is the
*read* side of that pair: a small, mock-free predicate that the
dotfiles janitor
and the future ``sac janitor`` CLI call before invoking
``git worktree prune`` (or ``git worktree remove``), so the prune-bug
window from lead-learnings/19 cannot re-open.

The predicate is intentionally conservative:

    is_safe_to_reap(path)  is True  iff  ALL of

        path/.git exists                    (it really IS a worktree)
        git status --porcelain is empty     (clean tree — no dirty
                                             work to lose)
        git rev-list develop..HEAD is empty (HEAD is not AHEAD of
                                             develop — i.e. fully
                                             merged or behind)

Any subprocess failure, missing ``git``, unreadable path, or unexpected
condition returns ``False``. The default-False posture means a janitor
that misreads the predicate (e.g. ``git`` is temporarily unavailable)
will *skip* a reap rather than risk destroying state. That is the
correct asymmetry for an advisory safety gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["is_safe_to_reap"]


def is_safe_to_reap(path: Path) -> bool:
    """Return True iff ``path`` is a clean, fully-merged worktree.

    Used by the dotfiles janitor and ``sac janitor`` to gate
    ``git worktree prune`` / ``git worktree remove`` against the
    silent-destruction mode documented in lead-learnings/19.

    The predicate returns ``True`` only when **all** of:

    * ``path/.git`` exists — the path really is a git worktree.
    * ``git -C <path> status --porcelain`` is empty — no dirty
      work, no untracked files. Anything porcelain reports is
      potential loss-of-work and disqualifies the worktree.
    * ``git -C <path> rev-list develop..HEAD`` is empty — ``HEAD``
      is not ahead of ``develop``. A worktree that IS ahead has
      commits not yet on ``develop`` and must not be reaped. This
      is what pairs the predicate with the PR #369 pre-stop rescue,
      and it carries MORE weight since the rescue stopped pushing
      (2026-07-17): a rescue commit now exists ONLY on local disk,
      so ``develop..HEAD`` being non-empty is the sole thing
      standing between it and a janitor. Reaping stays blocked
      until the work lands on ``develop`` via PR.

    Any error — subprocess failure, missing ``git``, unreadable
    path, ``develop`` not present, etc. — returns ``False``. The
    default-False posture is deliberate: a janitor that cannot
    *prove* safety must skip the reap. False-negatives leave a
    stale worktree on disk (annoying); false-positives destroy
    work (the lead-learnings/19 failure mode). We pick annoying.

    Args:
        path: Filesystem path to a candidate worktree.

    Returns:
        ``True`` iff the predicate can prove the worktree is safe
        to reap; ``False`` on any error or ambiguity.
    """
    try:
        if not (path / ".git").exists():
            return False
        # Clean tree: porcelain output empty.
        porcelain = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        if porcelain.stdout.strip():
            return False
        # Not ahead of develop: rev-list develop..HEAD empty.
        ahead = subprocess.run(
            ["git", "-C", str(path), "rev-list", "develop..HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        if ahead.stdout.strip():
            return False
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
