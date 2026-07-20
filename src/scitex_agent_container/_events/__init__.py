"""sac's own operational event log.

sac's unattended passes decide things about the fleet every few minutes,
forever, with nobody watching. This package is where sac records what it
observed and what it decided, so that sac's own behaviour is auditable
afterwards without depending on any software sac does not own.

See :mod:`._log` for the record and the event vocabulary, and
:mod:`._verdicts` for the shared per-subject routing the scheduled passes use.
"""

from __future__ import annotations

from ._log import (
    EVENT_LOG_ENV,
    EVENT_LOG_FILENAME,
    KNOWN_EVENTS,
    PASS_COMPLETED,
    SELF_IMPAIRED,
    SELF_RECOVERED,
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    SUBJECT_UNKNOWN,
    SacEvent,
    event_log_path,
    log_event,
    log_pass_completed,
    read_events,
)
from ._verdicts import (
    EmitOutcome,
    SubjectState,
    SubjectVerdict,
    degraded_state_path,
    emit_self_state,
    emit_subject_verdicts,
    recover_absent_subjects,
    self_state_path,
)

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
    "EmitOutcome",
    "SacEvent",
    "SubjectState",
    "SubjectVerdict",
    "degraded_state_path",
    "emit_self_state",
    "emit_subject_verdicts",
    "event_log_path",
    "log_event",
    "log_pass_completed",
    "read_events",
    "recover_absent_subjects",
    "self_state_path",
]
