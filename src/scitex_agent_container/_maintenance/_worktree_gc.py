"""Reap SAFE, STALE git worktrees — the permanent answer to sprawl.

Agent-tool isolation worktrees auto-clean only when they were never
edited. Anything an agent actually TOUCHED persists forever, and until
now no periodic GC, no cap, and no alarm existed anywhere. One repo
reached **105 worktrees** and helped trigger a host load-spike (card
``incident-worktree-sprawl-permanent-gc-20260710``, operator-declared
P1). Sprawl is not a tidiness problem; it is a standing liability.

This module is the engine. It owns three decisions and delegates the
rest:

* **Report by default.** ``apply=False`` (what ``--dry-run`` gives, and
  the default of this function) removes NOTHING: every worktree is judged
  and reported, and the prune pass runs as ``prune --dry-run``. A GC whose
  default is destructive gets run destructively by accident exactly once.
* **Removal is ``git worktree remove`` WITHOUT ``--force``.** That is not
  belt-and-braces, it is the backstop: git's own refusal to remove a
  dirty worktree is a second, independent implementation of the clean leg
  written by people better at this than we are. ``--force`` would disable
  the only check we did not write ourselves. It is never passed, and a
  test pins the absence.
* **``git worktree prune`` is separate and unconditional-safe.** It only
  drops administrative refs whose directory is ALREADY GONE, so it
  destroys no files by construction and needs no predicate.

The predicate itself lives in :mod:`._worktree_gc_predicate` (four legs,
each three-state, KEEP on any doubt) and the observation in
:mod:`._worktree_gc_probe`. ``pr_merged`` and ``cwd_scan`` are injectable
seams so the whole thing is testable against real temp repos with no
network and no ambient processes.
"""

from __future__ import annotations

import time
from pathlib import Path

from ._worktree_gc_model import (
    DEFAULT_CAP,
    DEFAULT_MIN_AGE_HOURS,
    KEEP_REMOVE_FAILED,
    GcOutcome,
    RepoGcResult,
    WorktreeInfo,
    WorktreeVerdict,
    exit_code_for,
)
from ._worktree_gc_predicate import verdict_for
from ._worktree_gc_probe import (
    CwdScan,
    PrLookup,
    gh_pr_merged,
    list_worktrees,
    run_git,
    running_cwds,
)

__all__ = [
    "DEFAULT_CAP",
    "DEFAULT_MIN_AGE_HOURS",
    "GcOutcome",
    "RepoGcResult",
    "WorktreeInfo",
    "WorktreeVerdict",
    "exit_code_for",
    "gc_repo",
    "gc_repos",
    "gh_pr_merged",
    "list_worktrees",
    "running_cwds",
]


def gc_repo(
    repo: str | Path,
    *,
    apply: bool = False,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    cap: int = DEFAULT_CAP,
    now: float | None = None,
    pr_merged: PrLookup = gh_pr_merged,
    cwd_scan: CwdScan = running_cwds,
) -> RepoGcResult:
    """GC one repo's worktrees. Reports by default; mutates only on ``apply``.

    The main worktree (the repo checkout itself) and bare repos are never
    candidates — they are not sprawl, they are the repo.

    An unreadable repo returns a result carrying ``error`` (UNKNOWN), never
    an empty verdict list: "I could not read this repo" must not render as
    "this repo has no worktrees".
    """
    now = time.time() if now is None else now
    ok, infos, err = list_worktrees(repo)
    if not ok:
        return RepoGcResult(repo=str(repo), applied=apply, cap=cap, error=err)

    cwds = cwd_scan()
    verdicts: list[WorktreeVerdict] = []
    for info in infos:
        if info.is_main or info.is_bare:
            continue  # the repo checkout itself is not a GC candidate
        verdict = verdict_for(
            repo,
            info,
            min_age_hours=min_age_hours,
            now=now,
            pr_merged=pr_merged,
            cwds=cwds,
        )
        if verdict.removable and apply:
            verdict = _remove(repo, verdict)
        verdicts.append(verdict)

    # Always safe: prune only drops admin refs whose directory is gone.
    # On a dry run it reports instead of acting, so --dry-run stays a pure
    # read across the WHOLE pass, not just the remove half.
    #
    # --verbose is not decoration: WITHOUT it `prune` (and `prune
    # --dry-run`) print NOTHING AT ALL, so the pass would silently claim to
    # have pruned — or to have planned to — with no evidence either way.
    # That is the exact "green line nobody can check" shape this whole GC
    # exists to kill, so the prune half reports by name like everything else.
    #
    # merge_stderr because git writes that verbose report to STDERR even on
    # success (measured on git 2.43, not assumed): a stdout-only read comes
    # back empty and the report silently disappears — which is how the first
    # cut of this function "reported" a prune it could not evidence.
    prune_args = ["worktree", "prune", "--verbose"] + ([] if apply else ["--dry-run"])
    prune_ok, prune_out = run_git(repo, *prune_args, merge_stderr=True)
    prune_detail = prune_out if prune_ok else f"prune failed: {prune_out}"

    return RepoGcResult(
        repo=str(repo),
        applied=apply,
        cap=cap,
        verdicts=tuple(verdicts),
        prune_detail=prune_detail,
    )


def _remove(repo: str | Path, verdict: WorktreeVerdict) -> WorktreeVerdict:
    """``git worktree remove <path>`` — no ``--force``, ever.

    A refusal is not an error to route around: it means git disagreed with
    our clean leg, and git wins. The worktree stays, and the disagreement
    is reported as a keep reason rather than swallowed.
    """
    removed_ok, detail = run_git(repo, "worktree", "remove", verdict.path)
    return WorktreeVerdict(
        path=verdict.path,
        branch=verdict.branch,
        head=verdict.head,
        keep_reasons=() if removed_ok else (KEEP_REMOVE_FAILED,),
        removed=removed_ok,
        remove_error="" if removed_ok else detail,
    )


def gc_repos(
    repos: list[str] | list[Path],
    *,
    apply: bool = False,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    cap: int = DEFAULT_CAP,
    now: float | None = None,
    pr_merged: PrLookup = gh_pr_merged,
    cwd_scan: CwdScan = running_cwds,
) -> GcOutcome:
    """Run :func:`gc_repo` over every repo; one bad repo never stops the rest."""
    return GcOutcome(
        results=tuple(
            gc_repo(
                repo,
                apply=apply,
                min_age_hours=min_age_hours,
                cap=cap,
                now=now,
                pr_merged=pr_merged,
                cwd_scan=cwd_scan,
            )
            for repo in repos
        )
    )
