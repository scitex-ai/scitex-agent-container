"""THE SAFETY PREDICATE — the whole point of the worktree GC.

A worktree is removable **iff ALL FOUR** legs pass:

1. **CLEAN** — ``git status --porcelain --ignored`` is empty. Untracked
   AND IGNORED files count as DIRTY: either one is work saved nowhere
   else, which makes it the most expensive thing in the tree, not the
   cheapest. ``--ignored`` was missing until 2026-08-20 and its absence
   was a data-loss bug — git refuses to remove a worktree over an
   untracked file and removes one over an ignored file without comment,
   so ignored content had neither this guard nor git's. See
   :func:`is_clean` for the measurement and the controls.
2. **MERGED** — the ancestor check (``rev-list --count <base>..<head>``
   is 0 against ``develop`` or ``main``) OR a MERGED PR exists for the
   branch. BOTH styles are required: a squash-merged branch is not an
   ancestor of its base, so the ancestor check alone calls every
   squash-merged branch "unmerged" forever and a squash-merging repo
   would never be GC'd at all.
3. **OLD** — the HEAD commit is older than ``min_age_hours``. A young
   worktree is presumed in flight.
4. **IDLE** — no running process has its cwd inside it.

Every leg returns ``True`` / ``False`` / ``None``, and ``None`` (could
not look) is treated exactly like ``False`` (looked, it failed): both
KEEP. That is the one rule this module exists to enforce. A boolean leg
would have to fold "I could not read the tree" into either "clean" or
"dirty", and whichever pole it picked would eventually be wrong in the
direction that destroys work.

The asymmetry is permanent and deliberate: a false KEEP leaves a stale
directory on disk — annoying, and the cap alarm shouts about it. A false
REMOVE destroys work that exists nowhere else. We pick annoying.
"""

from __future__ import annotations

from pathlib import Path

from ._worktree_gc_model import (
    KEEP_AGE_UNKNOWN,
    KEEP_DIRTY,
    KEEP_IN_USE,
    KEEP_IN_USE_UNKNOWN,
    KEEP_LOCKED,
    KEEP_MERGE_UNKNOWN,
    KEEP_MISSING,
    KEEP_TOO_YOUNG,
    KEEP_UNMERGED,
    MERGE_BASES,
    WorktreeInfo,
    WorktreeVerdict,
)
from ._worktree_gc_probe import PrLookup, run_git

__all__ = ["is_clean", "is_idle", "is_merged", "is_old_enough", "verdict_for"]


def is_clean(path: str) -> tuple[bool | None, str]:
    """Leg 1 — clean tree. Untracked AND IGNORED files count as DIRTY.

    An unreadable tree returns ``None`` with the ``dirty`` reason: we
    could not prove it clean, so it is kept.

    ``--ignored`` IS THE LOAD-BEARING FLAG, and its absence was a data-loss
    bug. This function used to run plain ``--porcelain`` and its docstring
    said the untracked default "is the behaviour we want, not an accident" —
    true, and it settled only ONE of the two axes a working tree can be
    non-empty on. Measured 2026-08-20 with a control:

        worktree holding only GITIGNORED/lessons.md
          git status --porcelain              ''              -> read CLEAN
          git status --porcelain --ignored    '!! GITIGNORED/'
          git worktree remove (no --force)    rc=0            -> NOTES GONE

        CONTROL, same position, an UNTRACKED file instead
          git status --porcelain              '?? scratch.txt' -> read DIRTY
          git worktree remove (no --force)    rc=128           -> refused

    So untracked content has TWO independent protections — this predicate
    and git itself — and ignored content had NEITHER. git declines to delete
    a worktree over an untracked file and deletes one over an ignored file
    without comment, which is the opposite of the intuition the old docstring
    was resting on.

    That gap lands exactly where this fleet keeps its notes. CLAUDE.md
    instructs every agent to write ``GITIGNORED/tasks/todo.md`` and
    ``GITIGNORED/tasks/lessons.md``; ``GITIGNORED/`` is ignored by name. And
    ``worktree-gc`` runs unattended on a timer, so the removal would be
    reported as routine housekeeping. This module's own header states the
    trade it exists to make — "REMOVE destroys work that exists nowhere else.
    We pick annoying." — and ignored files were the one class where it was
    silently picking the other way.

    Found by applying dotfiles' parallel finding (git refuses to overwrite an
    untracked file on a merge and silently overwrites an ignored one) to this
    package rather than only acknowledging it.
    """
    ok, out = run_git(path, "status", "--porcelain", "--ignored")
    if not ok:
        return None, KEEP_DIRTY
    return (True, "") if not out.strip() else (False, KEEP_DIRTY)


def is_merged(
    repo: str | Path, info: WorktreeInfo, pr_merged: PrLookup
) -> tuple[bool | None, str]:
    """Leg 2 — merged by ANCESTOR or by MERGED PR (squash-safe).

    Order matters. The ancestor check is local, free, and cannot lie, so
    it runs first against each base that exists. Only if no base swallows
    the branch do we ask GitHub, because that is exactly the shape a
    squash-merge leaves behind.

    A definite UNMERGED requires TWO sources to agree: not an ancestor of
    a base we actually read, AND GitHub answering that no merged PR
    exists. One source alone is not corroboration — if we could not read
    a single base ref, a bare "gh says no PR" leaves us with an UNKNOWN,
    not a verdict.
    """
    ref = info.branch or info.head
    if not ref:
        return None, KEEP_MERGE_UNKNOWN
    evaluated = False
    for base in MERGE_BASES:
        base_ok, _ = run_git(repo, "rev-parse", "--verify", "--quiet", base)
        if not base_ok:
            continue
        count_ok, count_out = run_git(repo, "rev-list", "--count", f"{base}..{ref}")
        if not count_ok:
            continue
        evaluated = True
        if count_out.strip() == "0":
            return True, ""
    # Not an ancestor of any base we could read. A squash-merged branch
    # looks EXACTLY like this, so ask GitHub before concluding anything.
    # stx-allow: fallback (reason: an injected or real PR seam raising must read as UNKNOWN -> KEEP, never crash the pass)
    try:
        pr = pr_merged(Path(str(repo)), info.branch)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pr = None
    if pr is True:
        return True, ""
    if pr is False and evaluated:
        return False, KEEP_UNMERGED
    return None, KEEP_MERGE_UNKNOWN


def is_old_enough(
    repo: str | Path, info: WorktreeInfo, min_age_hours: float, now: float
) -> tuple[bool | None, str]:
    """Leg 3 — the HEAD commit is older than ``min_age_hours``.

    Commit time, not directory mtime: an mtime moves when anything writes
    into the tree (a build, a linter, a stray editor swap file), so it
    answers "was this touched?" rather than "is this in flight?". A branch
    whose last commit is a day old is a branch nobody is committing to.
    """
    if not info.head:
        return None, KEEP_AGE_UNKNOWN
    ok, out = run_git(repo, "log", "-1", "--format=%ct", info.head)
    if not ok or not out.strip():
        return None, KEEP_AGE_UNKNOWN
    # stx-allow: fallback (reason: an unparseable commit timestamp is an UNKNOWN age -> KEEP, never a crash)
    try:
        committed = float(out.strip().splitlines()[0])
    except ValueError:
        return None, KEEP_AGE_UNKNOWN
    age_hours = (now - committed) / 3600.0
    return (True, "") if age_hours > min_age_hours else (False, KEEP_TOO_YOUNG)


def is_idle(path: str, cwds: set[Path] | None) -> tuple[bool | None, str]:
    """Leg 4 — no running process has its cwd inside the worktree.

    ``cwds is None`` means the signal was UNAVAILABLE (see
    :func:`._worktree_gc_probe.running_cwds`) and returns UNKNOWN -> KEEP.
    It never reads as "nothing is running": that collapse is what would
    let the GC delete the tree an agent is working in right now.
    """
    if cwds is None:
        return None, KEEP_IN_USE_UNKNOWN
    # stx-allow: fallback (reason: an unresolvable worktree path is an UNKNOWN, and unknown means KEEP)
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return None, KEEP_IN_USE_UNKNOWN
    for cwd in cwds:
        if cwd == resolved or resolved in cwd.parents:
            return False, KEEP_IN_USE
    return True, ""


def verdict_for(
    repo: str | Path,
    info: WorktreeInfo,
    *,
    min_age_hours: float,
    now: float,
    pr_merged: PrLookup,
    cwds: set[Path] | None,
) -> WorktreeVerdict:
    """Run all four legs and collect EVERY keep reason, not just the first.

    Reporting every reason is what makes the cap card actionable: "17
    kept: 9 dirty, 6 unmerged, 2 in-use" tells the operator what to do,
    where "17 kept" tells them nothing. So no leg short-circuits.
    """
    reasons: list[str] = []
    if info.is_locked:
        reasons.append(KEEP_LOCKED)
    if info.is_prunable or not Path(info.path).is_dir():
        # The directory is already gone: `worktree remove` would error and
        # there is nothing to destroy. `worktree prune` is the correct —
        # and unconditionally safe — tool for the leftover admin ref.
        return WorktreeVerdict(
            path=info.path,
            branch=info.branch,
            head=info.head,
            keep_reasons=tuple(reasons + [KEEP_MISSING]),
        )
    legs = (
        is_clean(info.path),
        is_merged(repo, info, pr_merged),
        is_old_enough(repo, info, min_age_hours, now),
        is_idle(info.path, cwds),
    )
    for ok, reason in legs:
        if ok is not True and reason:
            reasons.append(reason)
    return WorktreeVerdict(
        path=info.path,
        branch=info.branch,
        head=info.head,
        keep_reasons=tuple(reasons),
    )
