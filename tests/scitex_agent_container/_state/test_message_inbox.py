"""Durable harness-neutral message inbox tests."""

from __future__ import annotations

import pytest

from scitex_agent_container._state import message_inbox as inbox


def test_delivery_poll_uses_two_targeted_queries_and_never_rows() -> None:
    # Arrange
    class SearchOnlyStore:
        def __init__(self) -> None:
            self.queries = []

        def search(self, query):
            self.queries.append(query)
            return []

        def rows(self):
            raise AssertionError("whole-store scan must not be used")

    store = SearchOnlyStore()
    # Act
    result = inbox._deliverable_rows(store, target_agent="scholar", stale_before=10.0)
    # Assert
    assert (result, len(store.queries), store.queries[0].predicates[0].value) == (
        [],
        2,
        "scholar",
    )


def test_accepted_message_is_readable_before_delivery(pg_schema: str) -> None:
    # Arrange
    target = "scholar"
    # Act
    receipt = inbox.accept_message(target_agent=target, payload="check inbox")

    rows = inbox.list_messages(target_agent=target)

    # Assert
    assert rows[0]["message_id"] == receipt["message_id"]


def test_dispatch_id_is_an_idempotency_key(pg_schema: str) -> None:
    # Arrange
    message_id = "dispatch-1"
    # Act
    first = inbox.accept_message(
        target_agent="scholar", payload="same", message_id=message_id
    )

    second = inbox.accept_message(
        target_agent="scholar", payload="same", message_id=message_id
    )

    # Assert
    assert (
        first["message_id"] == second["message_id"] and len(inbox.list_messages()) == 1
    )


def test_reusing_id_for_different_content_is_refused(pg_schema: str) -> None:
    # Arrange
    inbox.accept_message(target_agent="scholar", payload="first", message_id="d1")

    # Act
    # Assert
    with pytest.raises(ValueError):
        inbox.accept_message(target_agent="scholar", payload="second", message_id="d1")


def test_claim_is_fifo_and_scoped_to_target(pg_schema: str) -> None:
    # Arrange
    inbox.accept_message(
        target_agent="scholar", payload="new", message_id="new", accepted_at=20.0
    )
    inbox.accept_message(
        target_agent="writer", payload="other", message_id="other", accepted_at=1.0
    )
    inbox.accept_message(
        target_agent="scholar", payload="old", message_id="old", accepted_at=10.0
    )

    # Act
    claimed = inbox.claim_next_message(target_agent="scholar", now=30.0)

    # Assert
    assert claimed is not None and claimed["message_id"] == "old"


def test_release_keeps_a_busy_message_retryable(pg_schema: str) -> None:
    # Arrange
    inbox.accept_message(target_agent="scholar", payload="later", message_id="d1")
    inbox.claim_next_message(target_agent="scholar", now=10.0, lease_owner="bridge-1")

    # Act
    inbox.release_message("d1", lease_owner="bridge-1", error="busy")

    # Assert
    assert inbox.claim_next_message(target_agent="scholar", now=11.0) is not None


def test_stale_delivery_claim_is_recovered_after_bridge_restart(pg_schema: str) -> None:
    # Arrange
    inbox.accept_message(target_agent="scholar", payload="survive", message_id="d1")
    inbox.claim_next_message(target_agent="scholar", now=10.0, lease_owner="old")

    # Act
    recovered = inbox.claim_next_message(
        target_agent="scholar", now=131.0, stale_after_seconds=120.0
    )

    # Assert
    assert recovered is not None and recovered["attempts"] == 2


def test_delivery_ack_records_incarnation(pg_schema: str) -> None:
    # Arrange
    inbox.accept_message(target_agent="scholar", payload="work", message_id="d1")
    inbox.claim_next_message(target_agent="scholar", lease_owner="bridge-1")

    # Act
    inbox.mark_delivered(
        "d1", lease_owner="bridge-1", incarnation_id="inc-7", delivered_at=50.0
    )

    # Assert
    assert inbox.list_messages()[0]["incarnation_id"] == "inc-7"


def test_stale_lease_owner_cannot_ack_a_reclaimed_message(pg_schema: str) -> None:
    # Arrange
    inbox.accept_message(target_agent="scholar", payload="work", message_id="d1")
    inbox.claim_next_message(target_agent="scholar", now=10.0, lease_owner="old")
    inbox.claim_next_message(
        target_agent="scholar",
        now=131.0,
        stale_after_seconds=120.0,
        lease_owner="new",
    )
    # Act
    acknowledged = inbox.mark_delivered("d1", lease_owner="old")
    # Assert
    assert acknowledged is False
