"""Record ``sac host sync --check`` drift verdicts in sac's own event log.

Stage-0 of the one-way central-sync plan shipped ``sac host sync --check`` — a
read-only drift detector that mutates nothing and exits non-zero on drift. But
a detector that only sets an exit code is an ALARM WITH NO ONE LISTENING: its
shout lands in a journald line nobody reads. That is how a five-release-stale
checkout stayed invisible until someone looked by hand.

This module makes the shout DURABLE. Each peer's read-only
:class:`~._sync.SyncResult` becomes a record in :mod:`..._events` — sac's own
append-only account of what sac observed and decided.

It is a REPORT, never an enforcer. It mutates nothing on any peer and never
triggers a sync — that (Stage 1) is deliberately out of scope. The only thing
it writes is sac's own log.

Three-state honest (the rule sac has paid for, twice):

* **DRIFTED** (behind / ahead / diverged / dirty) → a DEGRADED record naming
  the peer and how it differs.
* **UNDETERMINED** (unreachable / no-module / not-a-checkout) → an UNKNOWN
  record, never rendered as clean. "I could not look" must never read as "I
  looked and it was fine".
* **CURRENT / SYNCED** (clean) → a RECOVERED record, if this peer was
  previously recorded as drifted.

Recording is a SIDE rail: a write failure is printed loudly by the log rail
itself and never crashes the check that feeds it.
"""

from __future__ import annotations

from typing import Any

from .._events import EmitOutcome, SubjectState, SubjectVerdict, emit_subject_verdicts
from ._model import PeerSyncReport
from ._sync import SyncResult

__all__ = ["SUBSYSTEM", "record_reports"]

#: The pass this module speaks for — the axis a reader filters the log on.
SUBSYSTEM = "host-sync"

_SUBJECT_KIND = "peer"


def _facts(report: PeerSyncReport) -> dict[str, Any]:
    """The structured drift facts, kept as fields rather than prose.

    Fields are queryable; a sentence is not. ``state`` and ``target`` are what
    a reader joins on when asking which peers were stale at a given hour.
    """
    return {
        "state": report.state.value,
        "target": report.target or None,
        "module": report.module or None,
    }


def _verdict_for(result: SyncResult) -> SubjectVerdict:
    """Map one peer's read-only report onto sac's three states."""
    report = result.before
    if report.is_undetermined:
        return SubjectVerdict(
            subject=result.peer,
            state=SubjectState.UNKNOWN,
            verdict=report.state.value,
            detail=(
                f"could not verify {result.peer} against the centre — "
                f"{report.summary()}; nothing was mutated and its drift is "
                f"unobserved, not absent"
            ),
            subject_kind=_SUBJECT_KIND,
            extra=_facts(report),
        )
    if report.is_drifted:
        return SubjectVerdict(
            subject=result.peer,
            state=SubjectState.DEGRADED,
            verdict=report.state.value,
            detail=(
                f"{result.peer} is NOT running the centre's code — {report.summary()}"
            ),
            subject_kind=_SUBJECT_KIND,
            extra=_facts(report),
        )
    return SubjectVerdict(
        subject=result.peer,
        state=SubjectState.HEALTHY,
        verdict=report.state.value,
        detail=f"{result.peer} matches the centre — {report.summary()}",
        subject_kind=_SUBJECT_KIND,
        extra=_facts(report),
    )


def record_reports(
    results: list[SyncResult],
    *,
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> EmitOutcome:
    """Record one event per peer from ``results``. Never raises.

    ``results`` are the read-only verdicts ``sac host sync --check`` produces.

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
