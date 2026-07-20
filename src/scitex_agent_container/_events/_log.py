"""sac's append-only OPERATIONAL EVENT LOG — one JSONL line per fact sac records.

WHY THIS EXISTS
    sac runs unattended passes on timers: the fleet reconciler, the auth-heal
    login-expired restarter, the host-sync drift check, the worktree GC, the
    accounts refresh. Each one makes decisions about the fleet every few
    minutes, forever, with nobody watching. Until now those decisions were
    written to a THIRD-PARTY APPLICATION's data store and to stderr. Neither is
    sac's own record:

    * the stderr line lands in a journal nobody opens, which is exactly how a
      dead cron job stayed dead for 49 days;
    * a store owned by another application can be absent, unwritable, or
      renamed, and when it is, sac retains NO account of what its own timers
      decided. sac's auditability cannot be contingent on software sac does
      not own.

    An unlogged decision is an undebuggable one. This module is sac's record of
    sac's own behaviour, kept by sac, for sac.

WHAT IT IS AND IS NOT
    It RECORDS. It never detects, restarts, refreshes or remediates anything —
    the passes own their remediation and keep owning it. A recorder is not an
    actor.

    It is also not addressed to anybody. These records describe what sac
    observed and what sac decided, in sac's own vocabulary. Nothing outside sac
    is modelled here, and no field exists to suit a reader other than sac.

RELATIONSHIP TO :mod:`.._authevents._log`
    That module is a different log holding different facts: the auth TIMELINE
    (rotations, 401s, restart attempt/outcome pairs), whose value is that it
    joins events across accounts and agents on a single account axis. This log
    holds pass VERDICTS — what a scheduled pass concluded about a subject, and
    whether the pass ran at all. They are not two doors onto the same data, and
    neither one is a copy of the other.

FAIL-OPEN, BUT NEVER SILENT
    Every write is best-effort and returns a bool: a full disk or a read-only
    mount must never abort the reconcile pass being recorded. The thing
    observed always outranks the observing of it.

    But a failed write ALWAYS prints, loudly, to stderr — the rail reports its
    own failure rather than trusting each caller to remember to. A logging rail
    that can fail quietly is worse than no rail, because it is believed.

TRI-STATE, ALWAYS
    ``subject``, ``subject_kind`` and ``verdict`` are ALWAYS present and are
    ``null`` when they do not apply or could not be determined. An absent field
    and an unknown value are different facts: the first says nobody thought to
    record it, the second says we looked and could not tell.

    The same discipline governs the event vocabulary. :data:`SUBJECT_UNKNOWN`
    exists so that "sac could not read this subject" can never be recorded as,
    or mistaken for, :data:`SUBJECT_RECOVERED`. "I could not look" must never
    read as "I looked and it was fine".
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "EVENT_LOG_ENV",
    "EVENT_LOG_FILENAME",
    "KNOWN_EVENTS",
    "PASS_COMPLETED",
    "SELF_IMPAIRED",
    "SELF_RECOVERED",
    "SUBJECT_DEGRADED",
    "SUBJECT_RECOVERED",
    "SUBJECT_UNKNOWN",
    "SacEvent",
    "event_log_path",
    "log_event",
    "log_pass_completed",
    "read_events",
]

#: Filename of the append-only log, relative to sac's runtime dir.
EVENT_LOG_FILENAME = "sac-events.jsonl"

#: Env override for the log location. Lets a test drive a REAL file in a temp
#: dir (no mocks) and lets an operator relocate the rail without a code change.
EVENT_LOG_ENV = "SAC_EVENT_LOG"

#: A scheduled pass RAN to completion. Written on EVERY pass, including passes
#: that found nothing wrong — those are the ticks that prove the mechanism is
#: alive at all. A record that only appears when there is trouble cannot
#: distinguish HEALTHY from DEAD, which is the failure this whole rail exists
#: to abolish.
PASS_COMPLETED = "pass-completed"

#: sac observed a subject in a bad state it could not remediate itself.
SUBJECT_DEGRADED = "subject-degraded"

#: sac observed that a previously-degraded subject is well again. Written on
#: the TRANSITION, so the log records recoveries rather than restating health
#: every few minutes forever.
SUBJECT_RECOVERED = "subject-recovered"

#: sac could NOT determine a subject's state. Deliberately its own event and
#: never folded into either of the two above: an unreadable peer is not a peer
#: without drift, it is a peer whose drift sac failed to observe.
SUBJECT_UNKNOWN = "subject-unknown"

#: sac observed that SAC ITSELF cannot do its job — the reconciler that cannot
#: read its own restart history, for instance. Distinct from a degraded
#: subject: the fault is sac's, and while it stands sac's other verdicts are
#: not trustworthy.
SELF_IMPAIRED = "self-impaired"

#: A previously recorded self-impairment has cleared.
SELF_RECOVERED = "self-recovered"

#: The closed set. Unknown values are still written (forward-compat: a record
#: we do not recognise is evidence too) but flagged with ``event_known: false``
#: so a reader can tell a new event type from a typo.
KNOWN_EVENTS = frozenset(
    {
        PASS_COMPLETED,
        SUBJECT_DEGRADED,
        SUBJECT_RECOVERED,
        SUBJECT_UNKNOWN,
        SELF_IMPAIRED,
        SELF_RECOVERED,
    }
)


def event_log_path() -> Path:
    """Where the event log lives. Resolved PER CALL, never cached.

    Per-call resolution matters: a module-level constant computed at import
    cannot be redirected by an env var a test sets afterwards, which is how a
    suite ends up writing into the REAL fleet runtime dir while believing it is
    hermetic.
    """
    override = os.environ.get(EVENT_LOG_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / EVENT_LOG_FILENAME


def _resolve_host() -> str:
    """Best-effort canonical host label. Never raises."""
    # stx-allow: fallback (reason: the host is a label on an observability
    # record; a resolver failure must degrade to a short hostname and then to
    # "unknown", never break the write it annotates.)
    try:
        from .._state.state_db_hostname import resolve_host

        return resolve_host(None)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        try:
            return socket.gethostname()
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            return "unknown"


def log_event(
    *,
    event: str,
    subsystem: str,
    detail: str,
    subject: str | None = None,
    subject_kind: str | None = None,
    verdict: str | None = None,
    extra: dict[str, Any] | None = None,
    path: Path | None = None,
    now: float | None = None,
    err_stream: Any = None,
) -> bool:
    """Append ONE event. Returns whether it was written; NEVER raises.

    A failed write is printed loudly to ``err_stream`` (default stderr) before
    returning ``False``. Callers do not have to remember to report it, and no
    caller can accidentally silence it.

    Parameters
    ----------
    event
        One of :data:`KNOWN_EVENTS`. Unknown values are recorded anyway and
        marked ``event_known: false``.
    subsystem
        Which sac pass is speaking (``"fleet-reconcile"``, ``"host-sync"``, …).
        This is the axis a reader filters on first when asking "did my timer
        run, and what did it decide".
    subject
        The thing observed — an agent, a peer, a repo, an account. ``None``
        when the fact is fleet-wide and belongs to no single subject.
    verdict
        The emitting pass's OWN verdict token, verbatim. Passing it through
        unmapped is deliberate: a verdict translated on the way into the log
        can no longer be compared against the code that produced it.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    now_ts = now if now is not None else datetime.now(tz=timezone.utc).timestamp()
    record: dict[str, Any] = {
        "timestamp_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "event": event,
        "subsystem": subsystem,
        # Tri-state, always present: null == does not apply / could not tell.
        "subject": subject,
        "subject_kind": subject_kind,
        "verdict": verdict,
        "detail": detail,
        "host": _resolve_host(),
        "pid": os.getpid(),
    }
    if event not in KNOWN_EVENTS:
        record["event_known"] = False
    if extra:
        for key, val in extra.items():
            record.setdefault(key, val)

    target = Path(path) if path is not None else event_log_path()
    # stx-allow: fallback (reason: FAIL-OPEN is this rail's contract. Failing to
    # record an event must never abort the pass being observed — losing a log
    # line is bad, losing the reconcile that line described is worse. The
    # failure is printed loudly below, never swallowed.)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[sac-events] FAILED to record {event} for "
            f"{subsystem}/{subject or 'fleet'} at {target} — {exc}. The pass "
            f"itself was UNAFFECTED, but this decision is now unrecorded.",
            file=stream,
        )
        return False


def log_pass_completed(
    *,
    subsystem: str,
    mode: str,
    counts: dict[str, int] | None = None,
    detail: str = "",
    path: Path | None = None,
    now: float | None = None,
    err_stream: Any = None,
) -> bool:
    """Record that a scheduled pass RAN. Never raises.

    Called on EVERY pass — above all on a pass that found nothing to do. "0
    restarted, all healthy" is the most important record there is, because it
    is the only thing distinguishing a healthy fleet from a pass that stopped
    running months ago.

    ``mode`` carries whether this was an ``apply`` pass or a dry run, and it is
    load-bearing rather than cosmetic: a hand-run dry run also writes this
    record, so a reader who ignores ``mode`` can believe a scheduled timer is
    alive on the strength of somebody having run the command by hand.
    """
    return log_event(
        event=PASS_COMPLETED,
        subsystem=subsystem,
        detail=detail or f"{subsystem} pass completed ({mode})",
        verdict=mode,
        extra={"mode": mode, "counts": dict(counts or {})},
        path=path,
        now=now,
        err_stream=err_stream,
    )


@dataclass(frozen=True)
class SacEvent:
    """One parsed record. ``raw`` keeps every field, including unknown ones."""

    timestamp_utc: str | None
    event: str | None
    subsystem: str | None
    subject: str | None
    subject_kind: str | None
    verdict: str | None
    detail: str | None
    raw: dict[str, Any]


def _parse(line: str) -> SacEvent | None:
    # stx-allow: fallback (reason: a half-written or corrupt line must not blind
    # a reader to the thousands of good ones around it — an append-only log read
    # during an incident has to degrade, not refuse.)
    try:
        obj = json.loads(line)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    if not isinstance(obj, dict):
        return None
    return SacEvent(
        timestamp_utc=obj.get("timestamp_utc"),
        event=obj.get("event"),
        subsystem=obj.get("subsystem"),
        subject=obj.get("subject"),
        subject_kind=obj.get("subject_kind"),
        verdict=obj.get("verdict"),
        detail=obj.get("detail"),
        raw=obj,
    )


def read_events(
    path: Path | None = None,
    *,
    subsystem: str | None = None,
    event: str | None = None,
) -> list[SacEvent]:
    """Read the log, skipping unparseable lines. Returns ``[]`` if absent.

    A missing log is an empty reading, not an error — but note it is also NOT
    evidence that no pass ran. Absence of the rail is absence of evidence, and
    this function cannot tell those apart for you.
    """
    target = Path(path) if path is not None else event_log_path()
    # stx-allow: fallback (reason: reading is diagnostic; an unreadable log must
    # degrade to an empty reading rather than raise into an incident.)
    try:
        with target.open("r", encoding="utf-8") as handle:
            found: Iterable[SacEvent] = [
                parsed for parsed in (_parse(ln) for ln in handle) if parsed
            ]
    except FileNotFoundError:
        return []
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return []
    return [
        item
        for item in found
        if (subsystem is None or item.subsystem == subsystem)
        and (event is None or item.event == event)
    ]
