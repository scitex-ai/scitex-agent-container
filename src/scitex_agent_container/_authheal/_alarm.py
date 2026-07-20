"""Record login-expired-restart verdicts in sac's own event log.

Two rails, both writing to :mod:`..._events`, both SIDE rails (a write that
fails is printed loudly by the log rail itself and can NEVER crash or skip the
restart pass that feeds it):

1. **Unrecovered records** — one per agent that is STILL login-expired after
   the hourly restart cap. Restarting is not fixing it, so instead of an
   infinite bounce sac records that it has stopped trying. Recorded on
   OVER-BUDGET/FAILED; a recovery is recorded the moment the agent is no longer
   login-expired (restarted, or recovered on its own and gone from the pass).

2. **The pass record** (:func:`record_pass_completed`) — written on EVERY pass,
   so a pass that finds nothing wrong still leaves proof the timer ticked. A
   rail that only writes when there is trouble cannot distinguish a healthy
   fleet from a restarter that stopped running months ago.

Mirrors :mod:`.._reconcile._alarm` deliberately — same vocabulary, same shared
routing, wording specific to the login-expired case.

ABSENCE MEANS RECOVERY HERE, AND ONLY HERE
    This pass reports only the agents currently login-expired, so an agent that
    was recorded as degraded and is NOT in this pass's reports has recovered on
    its own. That is why :func:`record_reports` calls
    :func:`~..._events.recover_absent_subjects`, which a whole-fleet pass such
    as the reconciler must NOT do.

    An UNOBSERVED agent IS in the reports, so it is not swept: recording a
    recovery on a reading we never took would be a false all-clear in its most
    durable form — the log would say there was nothing left to look at, on the
    strength of the pass having failed to look.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .._events import (
    EmitOutcome,
    SubjectState,
    SubjectVerdict,
    emit_subject_verdicts,
    log_pass_completed,
    recover_absent_subjects,
)
from .._reconcile._rule import Verdict

__all__ = ["SUBSYSTEM", "record_pass_completed", "record_reports"]

#: The pass this module speaks for — the axis a reader filters the log on.
SUBSYSTEM = "auth-heal"

_SUBJECT_KIND = "agent"

#: Verdicts meaning "still login-expired and a restart is NOT fixing it".
_DEGRADED = (Verdict.OVER_BUDGET, Verdict.FAILED)

#: Verdicts meaning "we acted / it is on the mend".
_HEALTHY = (Verdict.RESTARTED,)


def _verdict_for(report: Any) -> SubjectVerdict | None:
    """Map one agent's report onto sac's states, or ``None`` for no record."""
    if report.verdict in _DEGRADED:
        return SubjectVerdict(
            subject=report.name,
            state=SubjectState.DEGRADED,
            verdict=report.verdict.value,
            detail=(
                f"{report.name} is STILL login-expired after auto-restart — "
                f"{report.detail}. sac has stopped restarting it: a restart "
                f"LOOP is worse than a wedged agent, and the usual cause is an "
                f"account that cannot refresh, which no restart fixes."
            ),
            subject_kind=_SUBJECT_KIND,
        )
    if report.verdict in _HEALTHY:
        return SubjectVerdict(
            subject=report.name,
            state=SubjectState.HEALTHY,
            verdict=report.verdict.value,
            detail=f"{report.name} was restarted — {report.detail}",
            subject_kind=_SUBJECT_KIND,
        )
    return None


def record_reports(
    reports: Iterable[Any],
    *,
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> EmitOutcome:
    """Record one event per agent from this pass's ``reports``. Never raises."""
    collected = list(reports)
    verdicts = [v for v in (_verdict_for(report) for report in collected) if v]
    emitted = emit_subject_verdicts(
        SUBSYSTEM, verdicts, path=path, now=now, err_stream=err_stream
    )
    swept = recover_absent_subjects(
        SUBSYSTEM,
        [report.name for report in collected],
        detail=(
            "no longer login-expired — absent from this pass, which reports "
            "only the agents currently wedged behind an auth banner"
        ),
        subject_kind=_SUBJECT_KIND,
        path=path,
        now=now,
        err_stream=err_stream,
    )
    return EmitOutcome(
        degraded=emitted.degraded,
        unknown=emitted.unknown,
        recovered=emitted.recovered + swept.recovered,
        failed=emitted.failed + swept.failed,
    )


def record_pass_completed(
    stats: Mapping[str, int],
    *,
    mode: str,
    host: str = "",
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> bool:
    """Record that a login-expired-restart pass RAN. Never raises."""
    return log_pass_completed(
        subsystem=SUBSYSTEM,
        mode=mode,
        counts=dict(stats),
        detail=f"auth-heal {mode} pass completed on {host or 'this host'}",
        path=path,
        now=now,
        err_stream=err_stream,
    )
