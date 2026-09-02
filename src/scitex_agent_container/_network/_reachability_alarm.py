"""Record ``sac a2a reachability`` verdicts in sac's own event log.

The probe (:mod:`._reachability`) answers, per peer, whether the cross-host
forwarder's transport works from this host. An answer that only sets an exit
code is an alarm nobody hears: the scheduled run's status lands in the
supervisor's execution log and nothing else. This module makes each verdict
DURABLE on the rail every other unattended pass already records to —
:mod:`.._events`, the same append-only ``sac-events.jsonl`` that
``fleet-reconcile`` (:mod:`.._reconcile._alarm`) and ``host-sync-check``
(:mod:`.._hostsync._alarm`) write — under its own ``subsystem`` axis so a
reader filtering the log sees the three passes as three passes.

Three-state honest, exactly as the probe is:

* ``reachable=False`` → a DEGRADED record naming the peer and the leg that
  failed. Re-recorded every pass while it stands.
* ``reachable=None`` → an UNKNOWN record. "I could not probe" is never
  rendered as "it was fine". The one exception is THIS host's own row: it
  is unknown because there is no leg to probe, not because sac failed to
  look, and re-recording that every 15 minutes forever would say nothing.
* ``reachable=True`` → a RECOVERED record, if and only if this peer was
  previously recorded as degraded — the transition, not a heartbeat.

Recording is a SIDE rail: a write failure is printed loudly by the log rail
itself and never crashes the probe that feeds it.
"""

from __future__ import annotations

from typing import Any

from .._events import (
    EmitOutcome,
    SubjectState,
    SubjectVerdict,
    emit_subject_verdicts,
    log_pass_completed,
)
from ._reachability import HostReachability, ReachabilityReport

__all__ = ["SUBSYSTEM", "record_pass_completed", "record_report"]

#: The pass this module speaks for — the axis a reader filters the log on.
SUBSYSTEM = "a2a-reachability"

_SUBJECT_KIND = "host"


def _facts(row: HostReachability) -> dict[str, Any]:
    """The structured facts, as fields — queryable, unlike a sentence."""
    return {
        "ssh_alias": row.ssh_alias,
        "transport": row.transport,
        "elapsed_ms": row.elapsed_ms,
    }


def _verdict_for(row: HostReachability) -> SubjectVerdict:
    if row.reachable is False:
        return SubjectVerdict(
            subject=row.host,
            state=SubjectState.DEGRADED,
            verdict="unreachable",
            detail=(
                f"cross-host a2a to {row.host} is DOWN from this host — "
                f"{row.error}"
            ),
            subject_kind=_SUBJECT_KIND,
            extra=_facts(row),
        )
    if row.reachable is None:
        return SubjectVerdict(
            subject=row.host,
            state=SubjectState.UNKNOWN,
            verdict="unknown",
            detail=(
                f"cross-host a2a to {row.host} could not be probed — "
                f"{row.error}; its reachability is unobserved, not absent"
            ),
            subject_kind=_SUBJECT_KIND,
            extra=_facts(row),
        )
    return SubjectVerdict(
        subject=row.host,
        state=SubjectState.HEALTHY,
        verdict="reachable",
        detail=(
            f"cross-host a2a to {row.host} is up from this host "
            f"({row.elapsed_ms} ms over ssh://{row.ssh_alias})"
        ),
        subject_kind=_SUBJECT_KIND,
        extra=_facts(row),
    )


def record_report(
    report: ReachabilityReport,
    *,
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> EmitOutcome:
    """Record one event per peer in ``report``. Never raises.

    This host's own row (``report.probed_from``) is skipped — see the
    module docstring. ``path`` / ``now`` / ``err_stream`` are the test
    seams the shared routing exposes: a real temp log, a fixed clock, a
    replacement stderr.
    """
    local = report.probed_from.lower()
    verdicts = [
        _verdict_for(row) for row in report.rows if row.host.lower() != local
    ]
    return emit_subject_verdicts(
        SUBSYSTEM, verdicts, path=path, now=now, err_stream=err_stream
    )


def record_pass_completed(
    report: ReachabilityReport,
    *,
    mode: str,
    path: Any = None,
    now: float | None = None,
    err_stream: Any = None,
) -> bool:
    """Record that a reachability pass RAN. Returns did-write; never raises.

    Written on EVERY pass, above all on one where every peer was reachable:
    "all reachable" is the record that distinguishes a healthy fleet from a
    probe that stopped running. ``mode`` is ``"all"`` for the scheduled
    fleet-wide sweep and ``"subset"`` for a hand-run ``--host`` probe, so a
    reader does not mistake a partial by-hand run for the timer being alive.
    """
    return log_pass_completed(
        subsystem=SUBSYSTEM,
        mode=mode,
        counts=report.counts(),
        detail=(
            f"a2a-reachability {mode} pass completed on {report.probed_from} "
            f"(exit {report.exit_code})"
        ),
        path=path,
        now=now,
        err_stream=err_stream,
    )
