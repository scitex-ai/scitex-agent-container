"""Data + vocabulary for the worktree GC. No subprocess, no I/O.

The types every other ``_worktree_gc*`` module speaks in, kept apart from
the code that observes the world (:mod:`._worktree_gc_probe`), the code
that decides (:mod:`._worktree_gc_predicate`), and the engine that
orchestrates them (:mod:`._worktree_gc`).

The keep-reason strings below are API, not debug text: they land in
``sac worktree gc --json``, in the console report, and in the cap card's
breakdown that tells the operator WHY 17 worktrees survived.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_CAP",
    "DEFAULT_MIN_AGE_HOURS",
    "KEEP_AGE_UNKNOWN",
    "KEEP_DIRTY",
    "KEEP_IN_USE",
    "KEEP_IN_USE_UNKNOWN",
    "KEEP_LOCKED",
    "KEEP_MERGE_UNKNOWN",
    "KEEP_MISSING",
    "KEEP_REMOVE_FAILED",
    "KEEP_TOO_YOUNG",
    "KEEP_UNMERGED",
    "MERGE_BASES",
    "GcOutcome",
    "RepoGcResult",
    "WorktreeInfo",
    "WorktreeVerdict",
    "exit_code_for",
]

#: A worktree younger than this is presumed IN FLIGHT and never touched.
#: 24h mirrors the SessionStart auto-reaper's threshold and the operator's
#: original card wording.
DEFAULT_MIN_AGE_HOURS = 24.0

#: Linked worktrees above this count make a repo shout on the board. It is
#: NOT a limit the GC enforces by deleting harder — the predicate is never
#: relaxed to hit a number. It is the threshold at which the worktrees the
#: predicate refused to touch become the operator's problem to look at.
DEFAULT_CAP = 20

#: Bases a branch may be merged into, checked in order. A base that does
#: not exist in the repo is skipped, not treated as a failure.
MERGE_BASES = ("develop", "main")

KEEP_DIRTY = "dirty"
KEEP_UNMERGED = "unmerged"
KEEP_MERGE_UNKNOWN = "merge-unknown"
KEEP_TOO_YOUNG = "too-young"
KEEP_AGE_UNKNOWN = "age-unknown"
KEEP_IN_USE = "in-use"
KEEP_IN_USE_UNKNOWN = "in-use-unknown"
KEEP_LOCKED = "locked"
KEEP_MISSING = "missing"
KEEP_REMOVE_FAILED = "remove-failed"


@dataclass(frozen=True)
class WorktreeInfo:
    """One entry from ``git worktree list --porcelain``, as reported."""

    path: str
    head: str = ""
    branch: str = ""  # short name ("feat/x"); empty when detached
    is_main: bool = False
    is_bare: bool = False
    is_locked: bool = False
    is_prunable: bool = False


@dataclass(frozen=True)
class WorktreeVerdict:
    """What we decided about ONE worktree, and why — never just a bool."""

    path: str
    branch: str = ""
    head: str = ""
    keep_reasons: tuple[str, ...] = ()
    removed: bool = False
    remove_error: str = ""

    @property
    def removable(self) -> bool:
        """True iff EVERY leg passed. One reason is enough to keep."""
        return not self.keep_reasons

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "branch": self.branch,
            "head": self.head[:12],
            "removable": self.removable,
            "keep_reasons": list(self.keep_reasons),
            "removed": self.removed,
            "remove_error": self.remove_error,
        }


@dataclass(frozen=True)
class RepoGcResult:
    """One repo's pass: every verdict, what was removed, what still hurts."""

    repo: str
    applied: bool = False
    cap: int = DEFAULT_CAP
    verdicts: tuple[WorktreeVerdict, ...] = ()
    prune_detail: str = ""
    error: str = ""

    @property
    def unreadable(self) -> bool:
        """True when the repo could not be read at all — UNKNOWN, not clean."""
        return bool(self.error)

    @property
    def removed(self) -> tuple[WorktreeVerdict, ...]:
        return tuple(v for v in self.verdicts if v.removed)

    @property
    def kept(self) -> tuple[WorktreeVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.removed)

    @property
    def count_before(self) -> int:
        """LINKED worktrees before the pass (the main checkout is excluded)."""
        return len(self.verdicts)

    @property
    def count_after(self) -> int:
        return self.count_before - len(self.removed)

    @property
    def exceeds_cap(self) -> bool:
        return self.count_after > self.cap

    @property
    def keep_reason_breakdown(self) -> dict[str, int]:
        """``{"dirty": 9, "unmerged": 6}`` — WHY the survivors survived.

        The cap card needs this: "17 kept" tells the operator nothing,
        "9 dirty, 6 unmerged, 2 in-use" tells them what to do.
        """
        counts: dict[str, int] = {}
        for verdict in self.kept:
            for reason in verdict.keep_reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "applied": self.applied,
            "cap": self.cap,
            "error": self.error,
            "count_before": self.count_before,
            "count_after": self.count_after,
            "exceeds_cap": self.exceeds_cap,
            "removed": [v.path for v in self.removed],
            "keep_reasons": self.keep_reason_breakdown,
            "prune": self.prune_detail,
            "worktrees": [v.to_dict() for v in self.verdicts],
        }


@dataclass(frozen=True)
class GcOutcome:
    """The whole pass across every repo — so ``--all`` is never silent."""

    results: tuple[RepoGcResult, ...] = ()

    @property
    def removed_count(self) -> int:
        return sum(len(r.removed) for r in self.results)

    @property
    def kept_count(self) -> int:
        return sum(len(r.kept) for r in self.results)

    @property
    def over_cap(self) -> tuple[RepoGcResult, ...]:
        return tuple(r for r in self.results if r.exceeds_cap)

    @property
    def unreadable(self) -> tuple[RepoGcResult, ...]:
        return tuple(r for r in self.results if r.unreadable)

    def summary_line(self) -> str:
        parts = [
            f"{len(self.results)} repo(s)",
            f"{self.removed_count} removed",
            f"{self.kept_count} kept",
        ]
        if self.over_cap:
            parts.append(f"{len(self.over_cap)} OVER CAP")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} UNREADABLE")
        return ", ".join(parts)


def exit_code_for(outcome: GcOutcome) -> int:
    """Worst verdict wins — ``--all`` never hides one bad repo behind good ones.

    0 = every repo read, none over cap. 1 = at least one repo is still
    over its cap after the pass (a real, actionable problem). 2 = at least
    one repo could not be READ, which OUTRANKS 1: an unknown is worse than
    a known-bad, because it is a known-bad you cannot see.
    """
    if outcome.unreadable:
        return 2
    return 1 if outcome.over_cap else 0
