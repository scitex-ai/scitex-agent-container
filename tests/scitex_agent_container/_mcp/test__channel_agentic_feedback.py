"""Sender-side absorption tests for semantic A2A feedback envelopes."""

from __future__ import annotations


def test_non_feedback_event_is_not_absorbed() -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_agentic_feedback import (
        absorb_agentic_feedback,
    )

    event = {"kind": "reaction", "extra": {"dispatch_id": "nonce"}}
    # Act
    absorbed = absorb_agentic_feedback(event, agent="alice")
    # Assert
    assert absorbed is False


def test_agentic_ack_envelope_marks_exact_nonce(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_agentic_feedback import (
        absorb_agentic_feedback,
    )
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    did = record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    event = {
        "kind": "agentic_ack",
        "from_agent": "bob",
        "extra": {
            "dispatch_id": did,
            "understood": "Implement the protocol.",
            "owner": "bob",
            "next_checkpoint": "Tests green.",
        },
    }
    # Act
    absorbed = absorb_agentic_feedback(event, agent="alice")
    status = list_dispatches(agent="alice")[0]["status"]
    # Assert
    assert absorbed is True and status == "agentic_acked"


def test_wrong_nonce_feedback_does_not_mutate_sender_ledger(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_agentic_feedback import (
        absorb_agentic_feedback,
    )
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(agent="alice", from_agent="alice", to_agent="bob", text="work")
    event = {
        "kind": "agentic_ack",
        "from_agent": "bob",
        "extra": {
            "dispatch_id": "stale-nonce",
            "understood": "Claim work.",
            "owner": "bob",
            "next_checkpoint": "Never.",
        },
    }
    # Act
    absorbed = absorb_agentic_feedback(event, agent="alice")
    status = list_dispatches(agent="alice")[0]["status"]
    # Assert
    assert absorbed is False and status == "sent"
