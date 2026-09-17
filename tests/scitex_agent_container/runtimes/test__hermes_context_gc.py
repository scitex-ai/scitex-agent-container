"""Deterministic Hermes session lifecycle outside model context."""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container.runtimes import _hermes_context_gc as context_gc
from scitex_agent_container.runtimes._hermes_context_gc import (
    DEFAULT_MAX_SESSION_AGE_MINUTES,
    HermesContextGcRefused,
    WorktreeFact,
    continuation_is_fresh_enough,
    handle_compression_failure,
    record_inbound_task_event,
)


def test_session_at_three_day_boundary_must_start_fresh() -> None:
    # Arrange
    now = 2_000_000.0
    started_at = now - DEFAULT_MAX_SESSION_AGE_MINUTES * 60
    # Act
    resumable = continuation_is_fresh_enough(started_at, now=now)
    # Assert
    assert resumable is False


def test_session_one_second_inside_three_days_may_continue() -> None:
    # Arrange
    now = 2_000_000.0
    started_at = now - DEFAULT_MAX_SESSION_AGE_MINUTES * 60 + 1
    # Act
    resumable = continuation_is_fresh_enough(started_at, now=now)
    # Assert
    assert resumable is True


def test_missing_started_at_fails_closed_to_fresh() -> None:
    # Arrange
    now = 2_000_000.0
    started_at = None
    # Act
    resumable = continuation_is_fresh_enough(started_at, now=now)
    # Assert
    assert resumable is False


def test_explicit_compression_failure_writes_minimal_handoff_before_rotation(
    tmp_path,
) -> None:
    # Arrange
    rotated = []
    card = {
        "id": "card-20260917",
        "title": "finish context lifecycle",
        "blocker": "none",
    }
    facts = [
        WorktreeFact(
            path=Path("/repo/.worktrees/task"),
            sha="abc123",
            branch="fix/task",
            dirty=(),
        )
    ]

    def rotate(path, nonce, old_session_id):
        rotated.append((path, nonce, old_session_id))
        return "fresh-session-2"

    # Act
    transition = handle_compression_failure(
        state_dir=tmp_path,
        agent_name="agent",
        workdir=Path("/repo/.worktrees/task"),
        session_record={
            "id": "old-session-1",
            "compression_failure_error": "summary backend timed out",
        },
        card=card,
        worktrees=facts,
        verification={"tests": "191 passed"},
        next_nonce=lambda: "nonce-123",
        rotate=rotate,
    )
    payload = json.loads(transition.handoff_path.read_text(encoding="utf-8"))
    # Assert
    assert (payload, transition.session_id, rotated) == (
        {
            "task_id": "card-20260917",
            "repo_worktree": "/repo/.worktrees/task",
            "exact_sha": "abc123",
            "tests": "191 passed",
            "next_action": "finish context lifecycle",
            "blocker": "none",
            "compression_failure": "summary backend timed out",
        },
        "fresh-session-2",
        [(transition.handoff_path, "nonce-123", "old-session-1")],
    )


def test_dirty_subagent_worktree_refuses_handoff_and_session_mutation(tmp_path) -> None:
    # Arrange
    rotated = []
    workdir = Path("/repo/.worktrees/task")
    facts = [
        WorktreeFact(
            path=workdir,
            sha="abc123",
            branch="hermes-subagent/task",
            dirty=(" M src/work.py",),
        )
    ]

    def action():
        return handle_compression_failure(
            state_dir=tmp_path,
            agent_name="agent",
            workdir=workdir,
            session_record={"id": "old", "compression_failure_error": "failed"},
            card={"id": "card", "task": "continue"},
            worktrees=facts,
            verification={"tests": "unknown"},
            next_nonce=lambda: "nonce",
            rotate=lambda *args: rotated.append(args) or "fresh",
        )

    # Act
    try:
        action()
    except HermesContextGcRefused as exc:
        error = str(exc)
    else:
        error = ""
    # Assert
    assert ("dirty worktree" in error, rotated) == (True, [])


def test_completed_owned_card_requests_fresh_next_task(tmp_path) -> None:
    # Arrange
    event = {
        "kind": "card-event",
        "extra": {
            "card_id": "task-20260917",
            "card_event_kind": "completed",
            "card_event_owner": "agent",
        },
    }
    # Act
    record_inbound_task_event(tmp_path, "agent", event)
    # Assert
    assert json.loads((tmp_path / "hermes-fresh-next-task.json").read_text()) == {
        "card_id": "task-20260917",
        "reason": "task-completed",
    }


def test_direct_assignment_tracks_explicit_parent_card_id(tmp_path) -> None:
    # Arrange
    event = {
        "kind": "message",
        "content": (
            "Implement context GC. This is part of "
            "fleet-integration-pipeline-backpressure-20260917; owner remains hub."
        ),
    }
    # Act
    record_inbound_task_event(tmp_path, "agent", event)
    # Assert
    assert json.loads((tmp_path / "hermes-active-card.json").read_text()) == {
        "card_id": "fleet-integration-pipeline-backpressure-20260917"
    }


def test_foreign_completion_never_becomes_active_card(tmp_path) -> None:
    # Arrange
    event = {
        "kind": "card-event",
        "extra": {
            "card_id": "foreign-card",
            "card_event_kind": "completed",
            "card_event_owner": "other-agent",
        },
    }
    # Act
    record_inbound_task_event(tmp_path, "agent", event)
    # Assert
    assert list(tmp_path.iterdir()) == []


def test_compression_failure_canary_reaches_fresh_nonce_rotation(tmp_path) -> None:
    # Arrange
    workdir = tmp_path / "repo"
    workdir.mkdir()
    stored_record = {
        "id": "stored-old",
        "started_at": 100.0,
        "compression_failure_error": "summary failed",
    }
    facts = [WorktreeFact(workdir, "abc123", "fix/task", ())]
    # Act
    replacement = context_gc.reconcile_context_lifecycle(
        state_dir=tmp_path,
        agent_name="agent",
        workdir=workdir,
        observed_session={
            "id": "live-old",
            "session_key": "stored-old",
            "status": "idle",
        },
        stored_record_reader=lambda *_args: stored_record,
        handoff_facts_reader=lambda *_args, **_kwargs: ({"status": "passed"}, []),
        card_reader=lambda _state: {"id": "card-1", "task": "continue safely"},
        worktree_reader=lambda *_args, **_kwargs: facts,
        rotate_session=lambda *_args, **_kwargs: "stored-fresh",
        nonce_factory=lambda: "nonce-123",
    )
    # Assert
    assert replacement == "stored-fresh"


def test_task_completion_marker_closes_old_and_selects_fresh(tmp_path):
    # Arrange
    (tmp_path / "hermes-fresh-next-task.json").write_text(
        '{"card_id":"card-1","reason":"task-completed"}', encoding="utf-8"
    )
    closed = []
    # Act
    replacement = context_gc.reconcile_context_lifecycle(
        state_dir=tmp_path,
        agent_name="agent",
        workdir=tmp_path,
        observed_session={
            "id": "live-old",
            "session_key": "stored-old",
            "status": "idle",
        },
        close_live=lambda _state, session_id: closed.append(session_id),
    )
    # Assert
    assert (
        replacement,
        closed,
        (tmp_path / "hermes-fresh-next-task.json").exists(),
    ) == ("", ["live-old"], False)
