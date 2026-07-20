"""Record reconcile verdicts in sac's own event log — and prove the pass ran.

Three rails, all writing to :mod:`..._events`, all SIDE rails (a write that
fails is printed loudly by the log rail itself and can NEVER crash or skip the
restart pass that feeds it — the pass's job is resurrecting corpses; recording
what it did is secondary and must not be able to take the primary down).

1. **Down records** — one per agent this enforcer could NOT recover. An
   enforcer that gives up SILENTLY is just the original bug with extra steps.

2. **The pass record** (:func:`record_pass_completed`) — the answer to "who
   watches the watcher". The reconciler is correctly independent of the agents
   it restarts (a systemd timer, outside their failure domain), but that only
   moves the question: if its timer is never enabled, or its unit fails, or its
   command silently no-ops, the fleet goes back to dying invisibly.

   Precedent, same failure class (scitex-hpc, 2026-07-13): a walltime trap
   FIRED on schedule, but its resubmit silently no-op'd because ``sbatch`` had
   been scrubbed off PATH. ~76 CI runners died. SLURM logged a signal-kill, not
   "renewal failed", so nothing alarmed.

   Hence a pass record is written on EVERY pass including a dry run, and above
   all on a pass that found nothing to do: "0 restarted, all healthy" is the
   most important record there is, because a rail that only writes when there
   is trouble cannot distinguish HEALTHY from DEAD. The record's ``counts``
   carry EVERY verdict this pass reached, including the ones that get no
   per-agent record of their own, so no verdict is ever wholly unrecorded.

3. **Self-impairment** (:func:`record_self_impaired`) — the reconciler cannot
   read its OWN restart memory, so its rate limits are unenforceable and it has
   refused to restart anything.

WHICH VERDICTS GET A PER-AGENT RECORD
    Deliberately narrow, and unchanged from when these were escalations: a
    per-agent DEGRADED record means sac tried and cannot fix this itself.

    Three DOWN verdicts are deliberately NOT per-agent records, because each
    resolves itself without anyone intervening: ``COOLING-DOWN`` (inside its
    30min debounce — the timer ticks every 5min, so a HEALTHY restart is
    cooling down for its next five ticks), ``CAPPED`` (sac's own per-pass
    throttle, not the agent's fault), and ``UNOBSERVED``/``UNKNOWN`` blindness
    that is fleet-WIDE rather than per-agent. They are all still counted in the
    pass record above, and they all still print and still drive the exit code
    non-zero — no per-agent record is not the same as unrecorded.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .._events import (
    EmitOutcome,
    SubjectState,
    SubjectVerdict,
    emit_self_state,
    emit_subject_verdicts,
    log_pass_completed,
)
from ._rule import Verdict

__all__ = [
    "SUBSYSTEM",
    "record_pass_completed",
    "record_reports",
    "record_self_impaired",
    "record_self_recovered",
]

#: The pass this module speaks for — the axis a reader filters the log on.
SUBSYSTEM = "fleet-reconcile"

_SUBJECT_KIND = "agent"

#: Verdicts meaning "this agent is DOWN and restarting is NOT fixing it".
_DEGRADED = (Verdict.FAILED, Verdict.OVER_BUDGET)

#: Verdicts meaning "this agent is UP".
_HEALTHY = (Verdict.OK, Verdict.RESTARTED)


def _verdict_for(report: Any) -> SubjectVerdict | None:
    """Map one agent's report onto sac's states, or ``None`` for no record.

    ``None`` is the documented narrow case described in this module's header —
    a verdict that resolves itself, counted in the pass record instead.
    """
    if report.verdict in _DEGRADED:
        return SubjectVerdict(
            subject=report.name,
            state=SubjectState.DEGRADED,
            verdict=report.verdict.value,
            detail=(
                f"{report.name} is DOWN and sac could not recover it — {report.detail}"
            ),
            subject_kind=_SUBJECT_KIND,
        )
    if report.verdict in _HEALTHY:
        return SubjectVerdict(
            subject=report.name,
            state=SubjectState.HEALTHY,
            verdict=report.verdict.value,
            detail=f"{report.name} is up — {report.detail}",
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
    """Record one event per agent from this pass's ``reports``. Never raises.

    ``reports`` are :class:`._pass.AgentReport` objects (duck-typed here to
    keep this module importable without :mod:`._pass`).
    """
    verdicts = [v for v in (_verdict_for(report) for report in reports) if v]
    return emit_subject_verdicts(
        SUBSYSTEM, verdicts, path=path, now=now, err_stream=err_stream
    )


def record_self_impaired(
    detail: str,
    *,
    state_file: Any = "",
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> bool:
    """Record that the reconciler cannot read its OWN state. Never raises.

    The reconciler exists to catch silent death; it must not die silently
    itself. When its restart history is denied or corrupt the rate limits are
    unenforceable, so it REFUSES to restart — and a refusal nobody hears is a
    no-op, which is exactly the "renewal mechanism that cannot report its own
    failure" class this whole design is built against.

    Measured precedent (Spartan, 2026-07-16): ``~/.scitex`` is a SYMLINK into a
    project whose membership was revoked, so every ``$HOME``-resolved path under
    it became permission-denied for fresh processes — while everything still
    LOOKED installed and configured.
    """
    return emit_self_state(
        SUBSYSTEM,
        impaired=True,
        verdict="state_unreadable",
        detail=(
            f"reconciler cannot read its own restart history at "
            f"{state_file or '?'} — {detail}. Rate limits are unenforceable, "
            f"so it has REFUSED to restart anything; dead agents are staying "
            f"dead until this is fixed."
        ),
        extra={"state_file": str(state_file or "")},
        path=path,
        now=now,
        err_stream=err_stream,
    )


def record_self_recovered(
    *,
    state_file: Any = "",
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> bool:
    """Record that the reconciler can read its own memory again.

    Returns whether a record was written. ``False`` is the ORDINARY answer: it
    means the reconciler was not impaired to begin with, so there was no
    recovery to record. Only a genuine transition writes — a pass runs every
    few minutes, and a "still fine" record on every tick would drown the log
    and rob :data:`SELF_RECOVERED` of any meaning.
    """
    return emit_self_state(
        SUBSYSTEM,
        impaired=False,
        verdict="state_readable",
        detail=(
            f"reconciler can read its restart history at "
            f"{state_file or '?'} again; rate limits are enforceable and "
            f"restarts have resumed"
        ),
        extra={"state_file": str(state_file or "")},
        path=path,
        now=now,
        err_stream=err_stream,
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
    """Record that a reconcile pass RAN. Returns did-write; never raises.

    Note the ``mode`` field: a hand-run ``sac agents reconcile`` (dry run) also
    writes this record. A reader who ignores ``mode`` can therefore believe the
    scheduled timer is alive on the strength of somebody having run the command
    by hand.
    """
    return log_pass_completed(
        subsystem=SUBSYSTEM,
        mode=mode,
        counts=dict(stats),
        detail=(f"fleet-reconcile {mode} pass completed on {host or 'this host'}"),
        path=path,
        now=now,
        err_stream=err_stream,
    )
