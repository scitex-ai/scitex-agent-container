"""Nonce-bound, model-authored A2A acknowledgement and progress state tests."""

from __future__ import annotations


def test_agentic_ack_status_is_registered() -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_AGENTIC_ACKED,
        VALID_STATUSES,
    )

    # Act
    registered = STATUS_AGENTIC_ACKED in VALID_STATUSES
    # Assert
    assert registered is True


def test_record_agentic_ack_api_is_available() -> None:
    # Arrange
    from scitex_agent_container._state import dispatch_feedback

    # Act
    api = getattr(dispatch_feedback, "record_agentic_ack", None)
    # Assert
    assert callable(api)


def test_record_agentic_ack_marks_exact_dispatch(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import record_agentic_ack
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_AGENTIC_ACKED,
        list_dispatches,
        record_dispatch,
    )

    did = record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    # Act
    record_agentic_ack(
        did,
        from_agent="bob",
        understood="Implement the nonce-bound protocol.",
        owner="bob",
        next_checkpoint="Tests green and branch pushed.",
        agent="alice",
    )
    row = list_dispatches(agent="alice")[0]
    # Assert
    assert row["status"] == STATUS_AGENTIC_ACKED


def test_wrong_nonce_cannot_mark_dispatch(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import record_agentic_ack
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    # Act
    result = record_agentic_ack(
        "wrong-nonce",
        from_agent="bob",
        understood="Do work.",
        owner="bob",
        next_checkpoint="Report.",
        agent="alice",
    )
    status = list_dispatches(agent="alice")[0]["status"]
    # Assert
    assert result is None and status == "sent"


def test_unexpected_peer_cannot_claim_nonce(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import record_agentic_ack
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    did = record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    # Act
    result = record_agentic_ack(
        did,
        from_agent="mallory",
        understood="Claim work.",
        owner="mallory",
        next_checkpoint="Never.",
        agent="alice",
    )
    status = list_dispatches(agent="alice")[0]["status"]
    # Assert
    assert result is None and status == "sent"


def test_exact_ack_replay_is_idempotent(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import record_agentic_ack
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    did = record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    kwargs = {
        "from_agent": "bob",
        "understood": "Do work.",
        "owner": "bob",
        "next_checkpoint": "Report.",
        "agent": "alice",
    }
    first = record_agentic_ack(did, **kwargs)
    # Act
    replay = record_agentic_ack(did, **kwargs)
    # Assert
    assert replay == first


def test_progress_before_agentic_ack_is_refused(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import record_progress
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    did = record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    # Act
    result = record_progress(
        did,
        from_agent="bob",
        status="in_progress",
        summary="Started.",
        agent="alice",
    )
    # Assert
    assert result is None


def test_progress_updates_sender_status(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import (
        record_agentic_ack,
        record_progress,
    )
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    did = record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    record_agentic_ack(
        did,
        from_agent="bob",
        understood="Do work.",
        owner="bob",
        next_checkpoint="Report.",
        agent="alice",
    )
    # Act
    record_progress(
        did,
        from_agent="bob",
        status="in_progress",
        summary="Tests running.",
        agent="alice",
    )
    # Assert
    assert list_dispatches(agent="alice")[0]["status"] == "in_progress"


def test_timeout_query_escalates_missing_agentic_ack(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import dispatch_status
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    did = record_dispatch(
        agent="alice", from_agent="alice", to_agent="bob", text="work", ts=100.0
    )
    # Act
    status = dispatch_status(
        did,
        agent="alice",
        agentic_ack_timeout_s=120.0,
        now=221.0,
    )
    # Assert
    assert status is not None and status["escalation"] == "missing_agentic_ack"


def test_late_mechanical_reaction_cannot_regress_agentic_ack(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.dispatch_feedback import record_agentic_ack
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        mark_dispatch_reacted,
        record_dispatch,
    )

    did = record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    record_agentic_ack(
        did,
        from_agent="bob",
        understood="Do work.",
        owner="bob",
        next_checkpoint="Report.",
        agent="alice",
    )
    # Act
    mark_dispatch_reacted(did, agent="alice")
    # Assert
    assert list_dispatches(agent="alice")[0]["status"] == "agentic_acked"
