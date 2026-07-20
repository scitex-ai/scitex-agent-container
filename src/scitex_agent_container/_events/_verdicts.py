"""Route a pass's per-subject verdicts into sac's event log — ONE implementation.

WHY THIS IS SHARED
    Four of sac's unattended passes (the fleet reconciler, the auth-heal
    restarter, the host-sync drift check, the worktree GC) reach the same
    shape at the end of their run: a list of subjects, each in one of three
    states, plus the pass's own verdict token and a human detail line. Each
    one had grown its OWN near-identical copy of the routing — four
    hand-maintained variants of the same forty lines, which is how two of them
    ended up with subtly different rules for the same situation.

    This is the single implementation. A pass's job is to reach a verdict; how
    a verdict is recorded is not four different questions.

THREE STATES, NEVER TWO
    :class:`SubjectState` is DEGRADED / UNKNOWN / HEALTHY. UNKNOWN is not a
    soft HEALTHY — it means sac could not read the subject, so its condition is
    unobserved rather than absent. Collapsing the two produces a false all-clear
    in its most durable form: a record saying sac looked and found nothing
    wrong, on the strength of the pass having failed to look.

TRANSITIONS, NOT A HEARTBEAT PER SUBJECT
    A DEGRADED subject is remembered in a small sac-owned state file, and its
    recovery is recorded when it actually recovers. The alternative — writing a
    record for every healthy subject on every pass — would put roughly a
    hundred agents into the log every five minutes forever and bury the events
    that matter under the ones that do not. sac already keeps small state files
    of exactly this kind for exactly this reason (the accounts-refresh dedupe,
    the reconciler's restart history), so this is the house pattern rather than
    a new invention.

    The state file is an OPTIMISATION OF THE RECORD, never an input to a
    decision. Losing it re-records a degraded subject as newly degraded and
    misses one recovery edge; it can never cause sac to act, or fail to act, on
    the fleet.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from ._log import (
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    SUBJECT_UNKNOWN,
    event_log_path,
    log_event,
)

__all__ = [
    "EmitOutcome",
    "SubjectState",
    "SubjectVerdict",
    "degraded_state_path",
    "emit_subject_verdicts",
    "recover_absent_subjects",
]


class SubjectState(str, Enum):
    """What sac concluded about one subject this pass. Three states, always."""

    #: Observed, and bad in a way this pass could not fix by itself.
    DEGRADED = "degraded"
    #: Could NOT be read. Unobserved — never rendered as healthy.
    UNKNOWN = "unknown"
    #: Observed, and well.
    HEALTHY = "healthy"


@dataclass(frozen=True)
class SubjectVerdict:
    """One subject's outcome, in the emitting pass's OWN vocabulary.

    ``verdict`` is the pass's verdict token verbatim (``"over_budget"``,
    ``"behind"``, …). It is passed through unmapped on purpose: a verdict
    translated on the way into the log can no longer be compared against the
    code that produced it.
    """

    subject: str
    state: SubjectState
    verdict: str
    detail: str
    subject_kind: str = "agent"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmitOutcome:
    """What the routing recorded this run — so the caller is never silent.

    ``failed`` is orthogonal to the other three: a subject whose record could
    not be written lands there INSTEAD of its state bucket, because sac cannot
    claim to have recorded something it did not record.
    """

    degraded: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def summary_line(self) -> str:
        """One human line naming what reached the log this run."""
        parts = [
            f"{len(self.degraded)} degraded",
            f"{len(self.unknown)} unknown",
            f"{len(self.recovered)} recovered",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} UNRECORDED")
        return "sac events: " + ", ".join(parts)


#: The state file sits beside the log it annotates, so redirecting the log in a
#: test (or on an operator's host) carries its state with it automatically.
def degraded_state_path(subsystem: str, *, path: Path | None = None) -> Path:
    """Where ``subsystem``'s currently-degraded subject set is remembered."""
    base = Path(path) if path is not None else event_log_path()
    return base.parent / f"sac-events-{subsystem}-degraded.json"


def _load_degraded(target: Path, *, err_stream: Any) -> set[str]:
    """Read the remembered set. A MISSING file is a normal empty start.

    A file that exists but cannot be read or parsed is NOT normal, and says so
    loudly: it means sac has forgotten which subjects it had already reported,
    and will re-report them as newly degraded.
    """
    if not target.exists():
        return set()
    # stx-allow: fallback (reason: a corrupt or unreadable memory file must
    # degrade to "remember nothing" rather than crash the pass that owns it —
    # the cost is one duplicated degraded record, and it is reported loudly
    # immediately below rather than swallowed.)
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[sac-events] could not read {target} — {exc}. Previously reported "
            f"subjects will be recorded as newly degraded again.",
            file=err_stream,
        )
        return set()
    if not isinstance(loaded, list):
        print(
            f"[sac-events] {target} is not a list of subjects — ignoring it. "
            f"Previously reported subjects will be recorded as newly degraded.",
            file=err_stream,
        )
        return set()
    return {str(item) for item in loaded}


def _save_degraded(target: Path, subjects: set[str], *, err_stream: Any) -> None:
    """Persist the remembered set atomically (tmp + rename). Never raises."""
    # stx-allow: fallback (reason: this file is an optimisation of the RECORD,
    # never an input to a fleet decision — failing to persist it can only cause
    # a duplicate degraded record next pass, so it must not crash the pass. The
    # failure is printed loudly rather than swallowed.)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(sorted(subjects), indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[sac-events] could not persist {target} — {exc}. Subjects already "
            f"reported this pass may be recorded as newly degraded next pass.",
            file=err_stream,
        )


_EVENT_FOR = {
    SubjectState.DEGRADED: SUBJECT_DEGRADED,
    SubjectState.UNKNOWN: SUBJECT_UNKNOWN,
}


def emit_subject_verdicts(
    subsystem: str,
    verdicts: Iterable[SubjectVerdict],
    *,
    path: Path | None = None,
    now: float | None = None,
    err_stream: Any = None,
) -> EmitOutcome:
    """Record one event per subject that has something to report.

    DEGRADED and UNKNOWN subjects are recorded every pass — an ongoing problem
    is an ongoing fact, and a log that mentions a wedged agent once and then
    goes quiet cannot be distinguished from a log written by a pass that died.

    HEALTHY subjects are recorded only on the TRANSITION out of a remembered
    bad state, so a well fleet does not write a record per agent per tick.

    Never raises: a subject whose record could not be written is reported by
    the log rail itself and recorded in :attr:`EmitOutcome.failed`, so one
    unwritable file never suppresses the rest of the fleet's records — nor
    unwinds work the pass already performed.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    state_file = degraded_state_path(subsystem, path=path)
    remembered = _load_degraded(state_file, err_stream=stream)

    degraded: list[str] = []
    unknown: list[str] = []
    recovered: list[str] = []
    failed: list[str] = []

    for item in verdicts:
        if item.state is SubjectState.HEALTHY:
            if item.subject not in remembered:
                continue
            written = log_event(
                event=SUBJECT_RECOVERED,
                subsystem=subsystem,
                subject=item.subject,
                subject_kind=item.subject_kind,
                verdict=item.verdict,
                detail=item.detail,
                extra=item.extra,
                path=path,
                now=now,
                err_stream=stream,
            )
            if written:
                remembered.discard(item.subject)
                recovered.append(item.subject)
            else:
                failed.append(item.subject)
            continue

        written = log_event(
            event=_EVENT_FOR[item.state],
            subsystem=subsystem,
            subject=item.subject,
            subject_kind=item.subject_kind,
            verdict=item.verdict,
            detail=item.detail,
            extra=item.extra,
            path=path,
            now=now,
            err_stream=stream,
        )
        if not written:
            failed.append(item.subject)
            continue
        remembered.add(item.subject)
        (degraded if item.state is SubjectState.DEGRADED else unknown).append(
            item.subject
        )

    _save_degraded(state_file, remembered, err_stream=stream)
    return EmitOutcome(
        degraded=tuple(degraded),
        unknown=tuple(unknown),
        recovered=tuple(recovered),
        failed=tuple(failed),
    )


def recover_absent_subjects(
    subsystem: str,
    observed: Iterable[str],
    *,
    detail: str,
    verdict: str = "absent",
    subject_kind: str = "agent",
    path: Path | None = None,
    now: float | None = None,
    err_stream: Any = None,
) -> EmitOutcome:
    """Record recovery for remembered subjects this pass did NOT see.

    Only meaningful for a pass whose subject list is ITSELF the problem list —
    one that reports only the agents currently in a bad way. For such a pass,
    a remembered subject's absence IS the observation that it recovered.

    It is deliberately a separate call rather than a flag on
    :func:`emit_subject_verdicts`, because for a pass that enumerates the whole
    fleet an absent subject means something entirely different — it was deleted
    or has become unreadable, not that it healed — and recording that as a
    recovery would be a false all-clear. Which meaning applies is a property of
    the calling pass, so it is stated at the call site.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    state_file = degraded_state_path(subsystem, path=path)
    remembered = _load_degraded(state_file, err_stream=stream)
    present = set(observed)

    recovered: list[str] = []
    failed: list[str] = []
    for subject in sorted(remembered - present):
        written = log_event(
            event=SUBJECT_RECOVERED,
            subsystem=subsystem,
            subject=subject,
            subject_kind=subject_kind,
            verdict=verdict,
            detail=detail,
            path=path,
            now=now,
            err_stream=stream,
        )
        if written:
            remembered.discard(subject)
            recovered.append(subject)
        else:
            failed.append(subject)

    _save_degraded(state_file, remembered, err_stream=stream)
    return EmitOutcome(recovered=tuple(recovered), failed=tuple(failed))
