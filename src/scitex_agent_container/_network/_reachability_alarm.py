"""Record ``sac a2a reachability`` TRANSITIONS in sac's own event log.

The probe (:mod:`._reachability`) answers, per peer, whether the cross-host
forwarder's transport works from this host. An answer that only sets an exit
code is an alarm nobody hears: the scheduled run's status lands in the
supervisor's execution log and nothing else. This module makes each verdict
DURABLE on the rail every other unattended pass already records to —
:mod:`.._events`, the same append-only ``sac-events.jsonl`` that
``fleet-reconcile`` (:mod:`.._reconcile._alarm`) and ``host-sync-check``
(:mod:`.._hostsync._alarm`) write — under its own ``subsystem`` axis so a
reader filtering the log sees the three passes as three passes.

TRANSITIONS, NOT A HEARTBEAT PER HOST (review round 2 of PR #1285)
    The shared rail re-records a DEGRADED or UNKNOWN subject on EVERY pass,
    by design: for a pass whose only durable output is the log, a subject
    mentioned once and then never again cannot be told from a pass that
    died. This pass has TWO other durable outputs that settle that question
    — the ``--record`` report file (the full per-pass picture, every host,
    every pass; ``sac a2a reachability --last`` reads it) and the
    PASS_COMPLETED record written every pass with the counts. So here a
    host is recorded only when its state DIFFERS from the last state this
    module recorded for it, plus once at first observation. Measured on
    compute-04 before this: ten ``subject-unknown`` records every fifteen
    minutes, forever, for ten peers whose only fact was "no token yet".

    The memory is a small JSON map beside the event log
    (:func:`last_state_path`), in the same posture as the rail's own
    degraded set: an OPTIMISATION OF THE RECORD, never an input to a
    decision. Losing it re-records every bad host once and costs nothing
    else.

Three-state honest, exactly as the probe is:

* ``reachable=False`` → a DEGRADED record naming the peer and the leg that
  failed, on the pass it first fails and on every CHANGE thereafter.
* ``reachable=None`` → an UNKNOWN record, likewise. "I could not probe" is
  never rendered as "it was fine".
* ``reachable=True`` → a RECOVERED record, if and only if this peer was
  previously recorded as degraded or unknown — the transition. A host that
  has been reachable since first sight writes nothing per host; the
  PASS_COMPLETED counts are its evidence.

THIS host's own row is skipped by the ``local`` flag the probe carried into
the row — the SAME decision :func:`._reachability.resolve_targets` made —
never by comparing the row's name to ``probed_from``. Those two spellings
can differ (``canonical_host()`` may say ``DXP480TPLUS-994`` where the
registry says ``scitex-nas-03``), and a name comparison recorded the self
row as unknown every pass on such a host.

Recording is a SIDE rail: a write failure is printed loudly by the log rail
itself and never crashes the probe that feeds it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .._events import (
    EmitOutcome,
    SubjectState,
    SubjectVerdict,
    emit_subject_verdicts,
    log_pass_completed,
)
from .._events._log import event_log_path
from .._events._verdicts import _warn
from ._reachability_report import HostReachability, ReachabilityReport

__all__ = [
    "SUBSYSTEM",
    "last_state_path",
    "record_pass_completed",
    "record_report",
]

#: The pass this module speaks for — the axis a reader filters the log on.
SUBSYSTEM = "a2a-reachability"

_SUBJECT_KIND = "host"


def last_state_path(*, path: Path | None = None) -> Path:
    """Where the last RECORDED state per host is remembered.

    Beside the event log — like the rail's own ``-degraded.json`` — so
    redirecting the log (a test, an operator's host) carries the memory
    with it. Its own file rather than the rail's set because the rail
    remembers only "bad or not", and unknown→degraded is a transition worth
    a record.
    """
    base = Path(path) if path is not None else event_log_path()
    return base.parent / f"sac-events-{SUBSYSTEM}-last-state.json"


def _load_last(target: Path, *, err_stream: Any) -> dict[str, str]:
    """The remembered host→state map. A MISSING file is a normal empty start.

    A file that exists but cannot be read is NOT normal, and says so loudly:
    every host will be re-recorded once as newly observed.
    """
    if not target.exists():
        return {}
    # stx-allow: fallback (reason: a corrupt memory file must degrade to
    # "remember nothing" rather than crash the pass — the cost is one
    # duplicated record per host, reported loudly right here.)
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        _warn(
            f"could not read {target} — {exc}. Every host will be recorded "
            f"again as newly observed.",
            err_stream=err_stream,
        )
        return {}
    if not isinstance(loaded, dict):
        _warn(
            f"{target} is not a host→state map — ignoring it. Every host will "
            f"be recorded again as newly observed.",
            err_stream=err_stream,
        )
        return {}
    return {str(k): str(v) for k, v in loaded.items()}


def _save_last(target: Path, states: dict[str, str], *, err_stream: Any) -> None:
    """Persist the memory atomically (tmp + rename). Never raises."""
    # stx-allow: fallback (reason: this file is an optimisation of the RECORD,
    # never an input to a fleet decision — failing to persist it can only
    # cause a duplicate record next pass, so it must not crash the pass.)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(states, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        _warn(
            f"could not persist {target} — {exc}. Hosts recorded this pass may "
            f"be recorded again next pass.",
            err_stream=err_stream,
        )


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
                f"cross-host a2a to {row.host} is DOWN from this host — {row.error}"
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
    """Record one event per peer whose state CHANGED since last recorded.

    Never raises. This host's own row (``row.local``) is skipped — see the
    module docstring. A host is offered to the shared rail only when its
    state differs from the one remembered for it (or nothing is remembered);
    the rail then applies its own rule (DEGRADED / UNKNOWN written, HEALTHY
    written as RECOVERED only after a remembered bad state). The memory is
    advanced for every host the pass saw except those whose record failed to
    land, so an unwritten transition is retried next pass rather than lost.

    ``path`` / ``now`` / ``err_stream`` are the test seams the shared routing
    exposes: a real temp log, a fixed clock, a replacement stderr.
    """
    memory = last_state_path(path=path)
    last = _load_last(memory, err_stream=err_stream)

    seen: dict[str, str] = {}
    verdicts: list[SubjectVerdict] = []
    for row in report.rows:
        if row.local:
            continue
        verdict = _verdict_for(row)
        seen[row.host] = verdict.state.value
        if last.get(row.host) == verdict.state.value:
            continue  # unchanged — the --record report carries this pass
        verdicts.append(verdict)

    outcome = emit_subject_verdicts(
        SUBSYSTEM, verdicts, path=path, now=now, err_stream=err_stream
    )
    for host, state in seen.items():
        if host in outcome.failed:
            continue
        last[host] = state
    _save_last(memory, last, err_stream=err_stream)
    return outcome


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
    probe that stopped running — and, now that per-host records are
    transitions only, it is the per-pass liveness signal for this rail.
    ``mode`` is ``"all"`` for the scheduled fleet-wide sweep and
    ``"subset"`` for a hand-run ``--host`` probe, so a reader does not
    mistake a partial by-hand run for the timer being alive.
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
