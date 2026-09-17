"""Deterministic Hermes session lifecycle outside model context."""

from __future__ import annotations

import json
import subprocess
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
    (tmp_path / "hermes-active-card.json").write_text(
        '{"card_id":"task-20260917"}', encoding="utf-8"
    )
    event = {
        "msg_id": "delivery-1",
        "kind": "card-event",
        "from_agent": "scitex-cards",
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
        "delivery_id": "delivery-1",
        "owner": "agent",
        "reason": "task-completed",
        "was_active": True,
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
        "from_agent": "scitex-cards",
        "extra": {
            "card_id": "foreign-card",
            "card_event_kind": "completed",
            "card_event_owner": "other-agent",
        },
    }
    # Act
    record_inbound_task_event(tmp_path, "agent", event)
    # Assert
    assert (
        (tmp_path / "hermes-fresh-next-task.json").exists(),
        (tmp_path / "hermes-active-card.json").exists(),
    ) == (False, False)


def test_untrusted_peer_cannot_forge_a_completion_boundary(tmp_path) -> None:
    # Arrange
    event = {
        "kind": "message",
        "from_agent": "peer-agent",
        "extra": {
            "card_id": "victim-card",
            "card_event_kind": "completed",
            "card_event_owner": "agent",
        },
    }
    # Act
    record_inbound_task_event(tmp_path, "agent", event)
    # Assert
    assert (
        (tmp_path / "hermes-fresh-next-task.json").exists(),
        (tmp_path / "hermes-active-card.json").exists(),
    ) == (False, False)


def test_new_assignment_invalidates_unconsumed_completion(tmp_path) -> None:
    # Arrange
    (tmp_path / "hermes-fresh-next-task.json").write_text(
        '{"card_id":"old","delivery_id":"delivery-old","owner":"agent",'
        '"reason":"task-completed",'
        '"was_active":true}',
        encoding="utf-8",
    )
    event = {
        "kind": "card-event",
        "from_agent": "scitex-cards",
        "extra": {
            "card_id": "new-card",
            "card_event_kind": "assigned",
            "card_event_owner": "agent",
        },
    }
    # Act
    record_inbound_task_event(tmp_path, "agent", event)
    # Assert
    assert (
        (tmp_path / "hermes-fresh-next-task.json").exists(),
        json.loads((tmp_path / "hermes-active-card.json").read_text()),
    ) == (False, {"card_id": "new-card"})


def test_compression_failure_canary_reaches_fresh_nonce_rotation(tmp_path) -> None:
    # Arrange
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (tmp_path / "hermes-active-card.json").write_text(
        '{"card_id":"card-1"}', encoding="utf-8"
    )
    stored_record = {
        "id": "stored-old",
        "started_at": 100.0,
        "compression_failure_error": "summary failed",
    }
    facts = [WorktreeFact(workdir, "abc123", "fix/task", ())]
    rotated = []

    def rotate(*_args, **kwargs):
        rotated.append(kwargs)
        kwargs["pre_close_check"]()
        return "stored-fresh"

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
        rotate_session=rotate,
        transition_guard=lambda _state, _session: None,
        nonce_factory=lambda: "nonce-123",
    )
    # Assert
    handoff = json.loads(rotated[0]["handoff_path"].read_text(encoding="utf-8"))
    assert (
        replacement,
        rotated[0]["old_session_id"],
        handoff["tests"],
    ) == ("stored-fresh", "live-old", "passed")


def test_task_completion_marker_closes_old_and_selects_fresh(tmp_path):
    # Arrange
    (tmp_path / "hermes-fresh-next-task.json").write_text(
        '{"card_id":"card-1","delivery_id":"delivery-1","owner":"agent",'
        '"reason":"task-completed",'
        '"was_active":true}',
        encoding="utf-8",
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
        transition_guard=lambda _state, _session: None,
        card_by_id_reader=lambda _card_id: {
            "id": "card-1",
            "status": "done",
            "owner": "agent",
        },
        handoff_facts_reader=lambda *_args, **_kwargs: ({}, []),
        worktree_reader=lambda *_args, **_kwargs: [
            WorktreeFact(tmp_path, "abc123", "fix/task", ())
        ],
    )
    # Assert
    assert (
        replacement,
        closed,
        (tmp_path / "hermes-fresh-next-task.json").exists(),
    ) == ("", ["live-old"], False)


def test_completion_marker_refuses_dirty_worktree_before_close(tmp_path) -> None:
    # Arrange
    (tmp_path / "hermes-fresh-next-task.json").write_text(
        '{"card_id":"card-1","delivery_id":"delivery-1","owner":"agent",'
        '"reason":"task-completed",'
        '"was_active":true}',
        encoding="utf-8",
    )
    closed = []

    def action():
        return context_gc.reconcile_context_lifecycle(
            state_dir=tmp_path,
            agent_name="agent",
            workdir=tmp_path,
            observed_session={"id": "live", "session_key": "stored", "status": "idle"},
            close_live=lambda _state, session_id: closed.append(session_id),
            transition_guard=lambda _state, _session: None,
            card_by_id_reader=lambda _card_id: {
                "id": "card-1",
                "status": "done",
                "owner": "agent",
            },
            worktree_reader=lambda *_args, **_kwargs: [
                WorktreeFact(tmp_path, "abc", "fix/task", (" M work.py",))
            ],
        )

    # Act
    try:
        action()
    except HermesContextGcRefused as exc:
        error = str(exc)
    else:
        error = ""
    # Assert
    assert ("dirty worktree" in error, closed) == (True, [])


def test_completion_marker_refuses_a_newer_active_card(tmp_path) -> None:
    # Arrange
    (tmp_path / "hermes-fresh-next-task.json").write_text(
        '{"card_id":"old","delivery_id":"delivery-old","owner":"agent",'
        '"reason":"task-completed",'
        '"was_active":true}',
        encoding="utf-8",
    )
    (tmp_path / "hermes-active-card.json").write_text(
        '{"card_id":"new"}', encoding="utf-8"
    )
    closed = []
    # Act
    try:
        context_gc.reconcile_context_lifecycle(
            state_dir=tmp_path,
            agent_name="agent",
            workdir=tmp_path,
            observed_session={"id": "live", "session_key": "stored", "status": "idle"},
            close_live=lambda _state, session_id: closed.append(session_id),
        )
    except HermesContextGcRefused as exc:
        error = str(exc)
    else:
        error = ""
    # Assert
    assert ("newer active card" in error, closed) == (True, [])


def test_replayed_completion_bound_to_old_session_cannot_close_new(tmp_path) -> None:
    # Arrange
    marker = {
        "card_id": "card-1",
        "delivery_id": "delivery-1",
        "owner": "agent",
        "reason": "task-completed",
        "session_id": "old-live",
        "was_active": True,
    }
    (tmp_path / "hermes-fresh-next-task.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    closed = []
    # Act
    replacement = context_gc.reconcile_context_lifecycle(
        state_dir=tmp_path,
        agent_name="agent",
        workdir=tmp_path,
        observed_session={"id": "new-live", "session_key": "new-stored", "status": "idle"},
        close_live=lambda _state, session_id: closed.append(session_id),
    )
    consumed = json.loads(
        (tmp_path / "hermes-consumed-completions.json").read_text(encoding="utf-8")
    )
    # Assert
    assert (
        replacement,
        closed,
        (tmp_path / "hermes-fresh-next-task.json").exists(),
        consumed,
    ) == (None, [], False, {"delivery_ids": ["delivery-1"]})


def test_preclose_refuses_sha_drift_during_nonce_proof(tmp_path) -> None:
    # Arrange
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (tmp_path / "hermes-active-card.json").write_text(
        '{"card_id":"card-1"}', encoding="utf-8"
    )
    facts = iter(
        (
            [WorktreeFact(workdir, "sha-before", "fix/task", ())],
            [WorktreeFact(workdir, "sha-after", "fix/task", ())],
        )
    )

    def action():
        return context_gc.reconcile_context_lifecycle(
            state_dir=tmp_path,
            agent_name="agent",
            workdir=workdir,
            observed_session={"id": "live", "session_key": "stored", "status": "idle"},
            stored_record_reader=lambda *_args: {
                "id": "stored",
                "started_at": 100.0,
                "compression_failure_error": "failed",
            },
            handoff_facts_reader=lambda *_args, **_kwargs: ({"status": "passed"}, []),
            card_reader=lambda _state: {"id": "card-1", "title": "continue"},
            worktree_reader=lambda *_args, **_kwargs: next(facts),
            transition_guard=lambda _state, _session: None,
            rotate_session=lambda *_args, **kwargs: (
                kwargs["pre_close_check"]() or "fresh"
            ),
            nonce_factory=lambda: "nonce",
        )

    # Act
    try:
        action()
    except HermesContextGcRefused as exc:
        error = str(exc)
    else:
        error = ""
    # Assert
    assert "path/SHA set changed" in error


def test_clean_unpushed_subagent_commit_is_unaccounted(tmp_path) -> None:
    # Arrange
    workdir = tmp_path / "repo"
    facts = [
        WorktreeFact(workdir, "current", "fix/task", ()),
        WorktreeFact(
            tmp_path / "subagent-1",
            "child",
            "hermes-subagent/child",
            (),
            accounted=False,
        ),
    ]

    def action():
        return handle_compression_failure(
            state_dir=tmp_path,
            agent_name="agent",
            workdir=workdir,
            session_record={"id": "old", "compression_failure_error": "failed"},
            card={"id": "card", "title": "continue"},
            worktrees=facts,
            verification={"tests": "passed"},
            next_nonce=lambda: "nonce",
            rotate=lambda *_args: "fresh",
        )

    # Act
    try:
        action()
    except HermesContextGcRefused as exc:
        error = str(exc)
    else:
        error = ""
    # Assert
    assert "not preserved on a remote ref" in error


def test_current_subagent_worktree_requires_remote_preservation(tmp_path) -> None:
    # Arrange: the current tree itself is a detached Hermes child checkout.
    workdir = tmp_path / "subagent-current"
    workdir.mkdir()
    for args in (
        ("init",),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True)
    (workdir / "work.txt").write_text("preserve me", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workdir), "add", "work.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(workdir), "commit", "-m", "child work"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(workdir), "branch", "-M", "hermes-subagent/test"],
        check=True,
        capture_output=True,
    )
    # Act
    facts = context_gc.collect_worktree_facts(workdir, session_started_at=0)
    # Assert
    assert (len(facts), facts[0].accounted) == (1, False)
