"""Record rate-wall verdicts in sac's event log — and prove the pass ran.

Side rails, exactly as :mod:`.._reconcile._alarm` and :mod:`.._authheal._alarm`
are: a write that fails is printed by the log rail itself and can never crash
or skip the pass that feeds it. Waking a parked agent is the primary job;
recording what happened is secondary and must not be able to take it down.

WHO ACTUALLY READS THIS, stated because the constitution requires it
--------------------------------------------------------------------
Two sinks, and the honest ranking between them matters.

**The reader of record is the EXIT CODE**, because this job runs under the
ecosystem supervisor's ``PeriodicRunner``, which writes one record per start
AND per finish — carrying ``exit_code`` and ``ok`` — to
``~/.scitex/dev/runtime/periodic-executions.jsonl``. That log is the
supervisor's product rather than a side effect (its own module says so), and
it is the file an operator greps to answer "did this run, and what happened".
It is also the only place that records a run which never STARTED, which is
the failure with no other witness. So the exit code is not shouted into a
void: it is persisted per run, per host, by the scheduler itself.

**The event log written here is the weaker sink, and it must be said
plainly: it has no production reader today.** Measured 2026-08-28 —
``_events.read_events`` has zero non-test callers in this repository. The
per-agent records below are a durable, structured history for whoever
eventually reads them; they are NOT how a failure reaches a human today. An
enforcer that relied on them alone would be a check whose failure nothing
reads.

RECORD OR CHECK? Say it plainly: at sac's layer this is a **record**, and at
the supervisor's layer it is a **check** — but one whose alarm has no last
mile.

The supervisor half is genuinely a check and not a print: ``PeriodicRunner``
folds each exit into a per-job rollup and, after ``UNHEALTHY_AFTER``
consecutive failures — or for a job that has never once succeeded — writes a
distinct ``job_unhealthy`` record, de-duplicated so it announces once and
re-armed on recovery so a flapping job is announced again, plus an
``unhealthy`` flag per job in the supervisor's ``state.json``. That is a
state machine with hysteresis.

What does NOT exist, measured 2026-08-28 and not dressed up: nothing polls
either file and tells a human. ``job_unhealthy`` and ``unhealthy`` have zero
matches anywhere under ``scitex_dev/_cli``, so no verb even reports them; and
no sac enforcer writes a board card (zero ``scitex_cards`` imports across
``_reconcile``, ``_authheal`` and this package). So the human-facing consumer
today is a person running ``sac agents resume-rate-limited`` or grepping
``periodic-executions.jsonl`` during an incident — which is precisely the
posture that let the outage this job exists to end run 1h46m unnoticed.

That gap is the same for all three enforcers, it is not created here, and it
is tracked rather than left implied:
``liveness-enforcer-failures-reach-no-human-20260828``.

That ranking is why ``WAITING`` exits ZERO. A wall that has not lifted is the
normal state during a rate limit and can persist for hours; a non-zero exit
there would stamp ``ok: false`` on the execution log every five minutes for
the whole window, and a signal that is always on is one its reader learns to
skip — which would demote the strong sink to the weak one.

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
