"""Background-subagent task observation (autonomy C2): capture + expose.

``_session_tasks`` resolves the SDK's task-lifecycle classes, captures
each task message into ``session.jsonl``, and accumulates terminal
completions on a conversation-lifetime :class:`TaskObservations` holder
so a later turn can read which background subagents finished + what they
produced.

No mocks: every fake here is a real dataclass-shaped object or a real
``types.ModuleType`` carrying real classes — the same hand-rolled-fake
discipline the sibling conversation tests use. The capture path writes a
real ``session.jsonl`` under ``tmp_path`` and reads it back.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from pathlib import Path

from scitex_agent_container._runners._session_state import append_session_message
from scitex_agent_container._runners._session_tasks import (
    TaskObservations,
    handle_task_message,
    is_task_message,
    resolve_task_types,
)

# ---------------------------------------------------------------------------
# Real dataclass-shaped fakes for the three SDK task-message classes
# ---------------------------------------------------------------------------


@dataclass
class _FakeTaskStarted:
    task_id: str
    session_id: str
    description: str
    task_type: str = "local_agent"


@dataclass
class _FakeTaskProgress:
    task_id: str
    session_id: str
    description: str


@dataclass
class _FakeTaskNotification:
    task_id: str
    session_id: str
    status: str
    summary: str
    output_file: str


def _fake_sdk_with_task_types() -> types.ModuleType:
    """An SDK module that exposes all three task-message classes."""
    mod = types.ModuleType("fake_sdk_tasks")
    mod.TaskStartedMessage = _FakeTaskStarted
    mod.TaskProgressMessage = _FakeTaskProgress
    mod.TaskNotificationMessage = _FakeTaskNotification
    return mod


def _read_jsonl(state_dir: Path) -> list[dict]:
    """Read every record from the state dir's ``session.jsonl``."""
    text = (state_dir / "session.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# resolve_task_types — maps the SDK classes to their event-type strings
# ---------------------------------------------------------------------------


def test_resolve_task_types_maps_notification_to_event_type() -> None:
    # Arrange
    sdk = _fake_sdk_with_task_types()
    # Act
    resolved = resolve_task_types(sdk)
    # Assert
    assert resolved[_FakeTaskNotification] == "task_notification"


def test_resolve_task_types_empty_when_sdk_lacks_classes() -> None:
    # Arrange — an SDK predating background-subagent observation.
    sdk = types.ModuleType("fake_sdk_no_tasks")
    # Act
    resolved = resolve_task_types(sdk)
    # Assert
    assert resolved == {}


# ---------------------------------------------------------------------------
# is_task_message — recognises resolved classes, no-ops on empty mapping
# ---------------------------------------------------------------------------


def test_is_task_message_true_for_resolved_notification() -> None:
    # Arrange
    sdk = _fake_sdk_with_task_types()
    task_types = resolve_task_types(sdk)
    msg = _FakeTaskNotification("t1", "s1", "completed", "did the thing", "/out")
    # Act
    matched = is_task_message(msg, task_types)
    # Assert
    assert matched is True


def test_is_task_message_false_when_task_types_empty() -> None:
    # Arrange — no resolved task classes → nothing should ever match.
    msg = _FakeTaskNotification("t1", "s1", "completed", "x", "/out")
    # Act
    matched = is_task_message(msg, {})
    # Assert
    assert matched is False


# ---------------------------------------------------------------------------
# TaskObservations — files events into lifecycle buckets, drains completions
# ---------------------------------------------------------------------------


def test_observations_record_files_notification_into_completions() -> None:
    # Arrange
    obs = TaskObservations()
    # Act
    obs.record({"type": "task_notification", "task_id": "t1", "status": "completed"})
    # Assert
    assert obs.completions == [
        {"type": "task_notification", "task_id": "t1", "status": "completed"}
    ]


def test_observations_record_files_started_into_started_bucket() -> None:
    # Arrange
    obs = TaskObservations()
    # Act
    obs.record({"type": "task_started", "task_id": "t1"})
    # Assert
    assert obs.started == [{"type": "task_started", "task_id": "t1"}]


def test_observations_drain_completions_returns_accumulated() -> None:
    # Arrange
    obs = TaskObservations()
    obs.record({"type": "task_notification", "task_id": "t1", "status": "completed"})
    # Act
    drained = obs.drain_completions()
    # Assert
    assert drained == [
        {"type": "task_notification", "task_id": "t1", "status": "completed"}
    ]


def test_observations_drain_completions_clears_after_draining() -> None:
    # Arrange
    obs = TaskObservations()
    obs.record({"type": "task_notification", "task_id": "t1", "status": "completed"})
    obs.drain_completions()
    # Act
    second = obs.drain_completions()
    # Assert — a consumed completion is not re-delivered on the next drain.
    assert second == []


# ---------------------------------------------------------------------------
# handle_task_message — persists to session.jsonl AND accumulates on the holder
# ---------------------------------------------------------------------------


def test_handle_task_message_persists_notification_to_jsonl(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    msg = _FakeTaskNotification("t1", "s1", "completed", "shipped the fix", "/tmp/out")
    # Act
    handle_task_message(
        msg,
        "task_notification",
        observations=obs,
        append_fn=append_session_message,
        state_dir=state_dir,
    )
    # Assert — the structured record reached session.jsonl with the summary.
    records = _read_jsonl(state_dir)
    assert records[0]["summary"] == "shipped the fix"


def test_handle_task_message_records_status_field_in_jsonl(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    msg = _FakeTaskNotification("t1", "s1", "failed", "broke", "/tmp/out")
    # Act
    handle_task_message(
        msg,
        "task_notification",
        observations=obs,
        append_fn=append_session_message,
        state_dir=state_dir,
    )
    # Assert
    assert _read_jsonl(state_dir)[0]["status"] == "failed"


def test_handle_task_message_accumulates_completion_on_holder(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    msg = _FakeTaskNotification("t9", "s1", "completed", "done", "/tmp/out")
    # Act
    handle_task_message(
        msg,
        "task_notification",
        observations=obs,
        append_fn=append_session_message,
        state_dir=state_dir,
    )
    # Assert — a later turn can read the completion off the holder.
    assert obs.completions[0]["task_id"] == "t9"


def test_handle_task_message_progress_has_no_status_field(tmp_path: Path) -> None:
    # Arrange — TaskProgressMessage carries no status; the record omits it.
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    msg = _FakeTaskProgress("t1", "s1", "halfway")
    # Act
    handle_task_message(
        msg,
        "task_progress",
        observations=obs,
        append_fn=append_session_message,
        state_dir=state_dir,
    )
    # Assert
    assert "status" not in _read_jsonl(state_dir)[0]
