"""Record worktree-sprawl verdicts in sac's own event log.

A GC that quietly reaps what it can and says nothing about what it could NOT
reap is how a repo reaches 105 worktrees while a green cron line scrolls past
every night. The reaping is the easy half; the half that prevents the incident
is RECORDING the worktrees the predicate refused to touch, because those are
the ones that accumulate forever.

So after a pass, each repo becomes a record in :mod:`..._events`.

Three-state honest, like every rail here:

* **OVER CAP** → a DEGRADED record naming the repo, the count, and the
  kept-reasons breakdown. The breakdown IS the value: "17 worktrees" is a
  number, "9 dirty" is an instruction.
* **UNREADABLE** (not a git repo, git missing, unreadable path) → an UNKNOWN
  record. "I could not look" must never read as "I looked and it was fine".
* **UNDER CAP** → a RECOVERED record, if this repo was previously over.

Recording is a SIDE rail: a write failure is printed loudly by the log rail
itself and never crashes the GC that feeds it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._events import EmitOutcome, SubjectState, SubjectVerdict, emit_subject_verdicts
from ._worktree_gc_model import RepoGcResult

__all__ = ["SUBSYSTEM", "record_gc_results"]

#: The pass this module speaks for — the axis a reader filters the log on.
SUBSYSTEM = "worktree-gc"

_SUBJECT_KIND = "repo"


def _subject_for(repo: str | Path) -> str:
    """The repo's stable label in the log.

    Keyed on the BASENAME so the label stays readable. Two checkouts of the
    same repo under different parents collide by design: the fact is about
    "the repo called scitex-agent-container", and the full path rides along in
    the record's own ``repo`` field either way.
    """
    return Path(str(repo)).name or "repo"


def _breakdown(result: RepoGcResult) -> str:
    counts = result.keep_reason_breakdown
    if not counts:
        return "(no kept worktrees)"
    return ", ".join(f"{n} {reason}" for reason, n in counts.items())


def _verdict_for(result: RepoGcResult) -> SubjectVerdict:
    """Map one repo's GC result onto sac's three states."""
    subject = _subject_for(result.repo)
    if result.unreadable:
        return SubjectVerdict(
            subject=subject,
            state=SubjectState.UNKNOWN,
            verdict="unreadable",
            detail=(
                f"could not enumerate worktrees for {result.repo} — "
                f"{result.error}; nothing was removed and its sprawl is "
                f"unobserved, not absent"
            ),
            subject_kind=_SUBJECT_KIND,
            extra={"repo": str(result.repo), "error": str(result.error)},
        )
    facts: dict[str, Any] = {
        "repo": str(result.repo),
        "count_after": result.count_after,
        "cap": result.cap,
        "removed": len(result.removed),
        "kept": len(result.kept),
        "keep_reasons": dict(result.keep_reason_breakdown),
    }
    if result.exceeds_cap:
        return SubjectVerdict(
            subject=subject,
            state=SubjectState.DEGRADED,
            verdict="over_cap",
            detail=(
                f"{result.repo} still has {result.count_after} worktrees "
                f"(cap {result.cap}) after removing {len(result.removed)}; "
                f"kept {len(result.kept)} — {_breakdown(result)}"
            ),
            subject_kind=_SUBJECT_KIND,
            extra=facts,
        )
    return SubjectVerdict(
        subject=subject,
        state=SubjectState.HEALTHY,
        verdict="under_cap",
        detail=(
            f"{result.repo} is within its cap — {result.count_after} "
            f"worktrees (cap {result.cap})"
        ),
        subject_kind=_SUBJECT_KIND,
        extra=facts,
    )


def record_gc_results(
    results: list[RepoGcResult],
    *,
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> EmitOutcome:
    """Record one event per repo from ``results``. Never raises.

    Parameters
    ----------
    path
        Event-log path. ``None`` = sac's resolved runtime log. Tests pass a
        real temp path — no mocks.
    now, err_stream
        Test seams: a fixed clock and a replacement stderr.
    """
    return emit_subject_verdicts(
        SUBSYSTEM,
        [_verdict_for(result) for result in results],
        path=path,
        now=now,
        err_stream=err_stream,
    )
