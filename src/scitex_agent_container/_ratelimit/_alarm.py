"""Record rate-wall verdicts in sac's event log — and prove the pass ran.

Side rails, exactly as :mod:`.._reconcile._alarm` and :mod:`.._authheal._alarm`
are: a write that fails is printed by the log rail itself and can never crash
or skip the pass that feeds it. Waking a parked agent is the primary job;
recording what happened is secondary and must not be able to take it down.

WHO ACTUALLY READS THIS, stated because the constitution requires it
--------------------------------------------------------------------
Honestly: **the event log has no production reader today.** Measured
2026-08-28 — ``_events.read_events`` has zero non-test callers in this
repository. So the log is a durable record, not a signal, and this enforcer
does NOT rely on it to reach a human.

The signal that does reach one is the EXIT CODE, and it is why
:meth:`.._ratelimit._pass.PassOutcome.exit_code` is written the way it is. A
non-zero exit fails the ``systemd --user`` unit, and a failed unit is visible
in ``systemctl --user list-units --failed``, in the journal, and to the
ecosystem supervisor that owns the host. That is the reader of record.

It is also why ``WAITING`` exits ZERO. A wall that has not lifted is the
normal state during a rate limit and can persist for hours; failing the unit
for it would leave the only real signal permanently on, and a permanently
failing unit is one nobody looks at — which would put this job back in the
same class as the log it cannot rely on.

WHICH VERDICTS GET A PER-AGENT RECORD
-------------------------------------
Narrow, and the narrowness is the point: a DEGRADED record means sac tried
and cannot fix this itself.

* ``FAILED`` — we nudged and could not prove the agent took it.
* ``OVER-BUDGET`` — it keeps falling back behind a wall faster than the
  hourly cap allows us to wake it, which is a pattern a human should see.
* ``RESET-UNKNOWN`` — a wall we can see and cannot time. Nothing here can
  resolve it; only a new pattern in :data:`.._ratelimit._banner.LIMIT_RE`'s
  reset clause can.

``COOLING-DOWN`` and ``CAPPED`` get no per-agent record and are carried in
the pass record's counts instead: both resolve themselves on the next tick
without anyone intervening, and an alarm that fires on a working rate limit
teaches its reader to ignore it.

``WAITING`` likewise gets none, for the strongest version of that reason: it
is not a problem at all. It is this job doing exactly what it was built to
do.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .._events import (
    EmitOutcome,
    SubjectState,
    SubjectVerdict,
    emit_subject_verdicts,
    log_pass_completed,
)
from ._rule import Verdict

__all__ = ["SUBSYSTEM", "record_pass_completed", "record_reports"]

SUBSYSTEM = "rate-limit-resume"
_SUBJECT_KIND = "agent"
_DEGRADED = (Verdict.FAILED, Verdict.OVER_BUDGET, Verdict.RESET_UNKNOWN)
_HEALTHY = (Verdict.RESUMED,)


def _verdict_for(report: Any) -> SubjectVerdict | None:
    """Map one agent's report onto sac's states, or ``None`` for no record."""
    if report.verdict in _DEGRADED:
        return SubjectVerdict(
            subject=report.name,
            state=SubjectState.DEGRADED,
            verdict=report.verdict.value,
            detail=(
                f"{report.name} is still parked behind a rate wall and sac "
                f"could not get it working again — {report.detail}"
            ),
            subject_kind=_SUBJECT_KIND,
        )
    if report.verdict in _HEALTHY:
        return SubjectVerdict(
            subject=report.name,
            state=SubjectState.HEALTHY,
            verdict=report.verdict.value,
            detail=f"{report.name} is working again — {report.detail}",
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
    verdicts = [v for v in (_verdict_for(report) for report in reports) if v]
    return emit_subject_verdicts(
        SUBSYSTEM, verdicts, path=path, now=now, err_stream=err_stream
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
    """Record that a resume pass RAN. Returns did-write; never raises.

    Written on EVERY pass, above all one that found nothing to do. A rail
    that writes only when there is trouble cannot tell HEALTHY from DEAD —
    and "this enforcer stopped running" is precisely the failure this fleet
    has already had: ``fleet-reconcile``'s timer sat in systemd's ``elapsed``
    state for nine days, reporting ``active``, firing never, and nothing
    noticed because a silent enforcer and a satisfied one look identical.
    """
    return log_pass_completed(
        subsystem=SUBSYSTEM,
        mode=mode,
        counts=dict(stats),
        detail=f"rate-limit resume {mode} pass completed on {host or 'this host'}",
        path=path,
        now=now,
        err_stream=err_stream,
    )
