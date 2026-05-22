"""Observe background-subagent task lifecycle messages (autonomy C2).

The claude-agent-sdk surfaces background subagents (``task_type ==
"local_agent"``) through three message classes that interleave with the
normal assistant stream during a turn:

* ``TaskStartedMessage`` — a background subagent began.
* ``TaskProgressMessage`` — incremental progress.
* ``TaskNotificationMessage`` — terminal signal carrying
  ``status`` (``completed`` / ``failed`` / ``stopped``) + ``summary``.

The per-turn receive loop used to drop these on the floor, so a
background subagent's result reached *nobody* — the whole point of a
background subagent (fan out work, react to it later) was unusable.
This module is the C2 fix: it CAPTURES each task message into
``session.jsonl`` as a structured event and ACCUMULATES it on a
:class:`TaskObservations` holder so a LATER turn (or the autonomous
loop — C3, separate item) can read which background subagents
completed and what they produced.

Honesty / non-breaking contract
--------------------------------
The three message classes are a recent SDK addition. :func:`resolve_task_types`
detects whether the *installed* SDK exposes them via ``hasattr`` and
returns an empty mapping when it does not — :func:`is_task_message` then
never matches and the receive loop is unchanged. The runner logs the
unavailability ONCE at startup so the gap is observable, never silently
swallowed. This is the honest "SDK too old → no-op + loud log" case, not
a masked failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# The SDK symbol names this module observes, mapped to the
# ``session.jsonl`` event ``type`` we record them under.
_TASK_SYMBOL_EVENT = {
    "TaskStartedMessage": "task_started",
    "TaskProgressMessage": "task_progress",
    "TaskNotificationMessage": "task_notification",
}


def resolve_task_types(sdk_module: Any) -> dict[type, str]:
    """Map the SDK's task-message classes to their event ``type`` string.

    Returns ``{TaskStartedMessage: "task_started", ...}`` for whichever of
    the three classes the installed SDK actually exposes. An SDK that
    predates background-subagent observation exposes none of them →
    returns ``{}`` and the caller no-ops (see module docstring). The gap
    is logged LOUD at WARNING by :func:`log_observability_status` so it is
    never a silent swallow.
    """
    resolved: dict[type, str] = {}
    for symbol, event_type in _TASK_SYMBOL_EVENT.items():
        cls = getattr(sdk_module, symbol, None)
        if isinstance(cls, type):
            resolved[cls] = event_type
    return resolved


def log_observability_status(task_types: dict[type, str], *, name: str) -> None:
    """Log whether background-task observation is available for this run.

    Called once at conversation startup. When ``task_types`` is empty the
    installed SDK is too old to surface task lifecycle messages, so the
    runner cannot react to background subagents — that is a real
    limitation the operator must be able to see, logged at WARNING. When
    available, logged at INFO so the capability is confirmable in the log.
    """
    if task_types:
        logger.info(
            "background-task observation enabled for %s (%d message types)",
            name,
            len(task_types),
        )
    else:
        logger.warning(
            "background-task observation UNAVAILABLE for %s: installed "
            "claude-agent-sdk exposes none of %s — background subagent "
            "completions will not be captured (SDK too old)",
            name,
            ", ".join(_TASK_SYMBOL_EVENT),
        )


def is_task_message(msg: Any, task_types: dict[type, str]) -> bool:
    """True when ``msg`` is one of the resolved task-lifecycle classes."""
    return isinstance(msg, tuple(task_types)) if task_types else False


def _task_event(msg: Any, event_type: str) -> dict:
    """Build the structured ``session.jsonl`` record for a task message.

    Pulls the fields common to all three classes (``task_id``,
    ``session_id``), plus the terminal-signal fields (``status``,
    ``summary``, ``output_file``) and the descriptive ``description`` when
    present. ``getattr`` defaults keep this robust across the three shapes
    (only ``TaskNotificationMessage`` has ``status`` / ``summary`` /
    ``output_file``; only Started/Progress have ``description``).
    """
    record: dict[str, Any] = {
        "type": event_type,
        "task_id": getattr(msg, "task_id", None),
        "session_id": getattr(msg, "session_id", None),
    }
    status = getattr(msg, "status", None)
    if status is not None:
        record["status"] = status
    summary = getattr(msg, "summary", None)
    if summary is not None:
        record["summary"] = summary
    output_file = getattr(msg, "output_file", None)
    if output_file is not None:
        record["output_file"] = output_file
    description = getattr(msg, "description", None)
    if description is not None:
        record["description"] = description
    task_type = getattr(msg, "task_type", None)
    if task_type is not None:
        record["task_type"] = task_type
    return record


@dataclass
class TaskObservations:
    """Per-conversation accumulator of observed background-subagent events.

    Turns are serial, so a single mutable holder is race-free (same model
    as :class:`._session_hooks.TurnContext`). ``completions`` records the
    terminal ``task_notification`` signals (the results a later turn /
    the autonomous loop reacts to — C3 consumes this). ``started`` /
    ``progress`` are kept for completeness so the full lifecycle is
    inspectable, but the load-bearing list for "which background subagents
    finished + what they produced" is ``completions``.
    """

    completions: list[dict] = field(default_factory=list)
    started: list[dict] = field(default_factory=list)
    progress: list[dict] = field(default_factory=list)

    def record(self, event: dict) -> None:
        """File a task event into the matching lifecycle bucket."""
        event_type = event.get("type")
        if event_type == "task_notification":
            self.completions.append(event)
        elif event_type == "task_started":
            self.started.append(event)
        elif event_type == "task_progress":
            self.progress.append(event)

    def drain_completions(self) -> list[dict]:
        """Return the accumulated completions and clear them.

        The autonomous loop (C3) calls this to consume the background
        subagent results since the last drain exactly once — clearing
        avoids re-reacting to the same completion on every later turn.
        """
        drained = self.completions
        self.completions = []
        return drained


def handle_task_message(
    msg: Any,
    event_type: str,
    *,
    observations: TaskObservations,
    append_fn: Any,
    state_dir: Any,
) -> None:
    """Capture one task-lifecycle message: persist + accumulate.

    Writes the structured record to ``session.jsonl`` via ``append_fn``
    (the runner injects ``append_session_message``) AND files it on the
    ``observations`` holder so a later turn can read it. Does NOT break
    the turn — task messages interleave with the assistant stream.
    """
    record = _task_event(msg, event_type)
    append_fn(state_dir, record)
    observations.record(record)


__all__ = [
    "TaskObservations",
    "handle_task_message",
    "is_task_message",
    "log_observability_status",
    "resolve_task_types",
]
