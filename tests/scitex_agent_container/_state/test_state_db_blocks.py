"""Tests for the ``comms_blocks`` table (task #27).

Lead's design amendment (2026-06-01): UNBLOCK is the existing
``grant_send`` (write ``comms_grants``); BLOCK is this module's
``block_send`` (write ``comms_blocks``) — symmetric helpers, same
shape as ``state_db_nodes`` grant/revoke/has helpers.

No-mocks (PA-306): real on-disk state.db. AAA markers, one
assertion per test.
"""

from __future__ import annotations

import time


import pytest

from scitex_agent_container._state.state_db_blocks import (
    block_send,
    ensure_comms_blocks_table,
    has_block,
    open_blocks_store,
    unblock_send,
)


# ---------------------------------------------------------------------------
# block_send + has_block — basic persistence
# ---------------------------------------------------------------------------


def test_block_then_has_block_returns_true(pg_schema: str) -> None:
    # Arrange
    block_send(sender="alice", target="lead")
    # Act
    flag = has_block(sender="alice", target="lead")
    # Assert
    assert flag is True


def test_has_block_returns_false_for_absent_pair(pg_schema: str) -> None:
    # Arrange — no block_send.
    # Act
    flag = has_block(sender="ghost", target="lead")
    # Assert
    assert flag is False


def test_block_is_directional(pg_schema: str) -> None:
    # Arrange — block alice → lead. The REVERSE direction
    # (lead → alice) must be unaffected.
    block_send(sender="alice", target="lead")
    # Act
    reverse_flag = has_block(sender="lead", target="alice")
    # Assert
    assert reverse_flag is False


def test_block_is_idempotent(pg_schema: str) -> None:
    # Arrange
    block_send(sender="alice", target="lead", note="first")
    # Act
    block_send(sender="alice", target="lead", note="second")
    # Assert — still blocked. The TIMESTAMP half of idempotence, which this
    # test's comment used to claim and never checked, is pinned separately
    # below where it can actually fail.
    assert has_block(sender="alice", target="lead")


# ---------------------------------------------------------------------------
# unblock_send — remove the row
# ---------------------------------------------------------------------------


def test_unblock_removes_the_block_row(pg_schema: str) -> None:
    # Arrange
    block_send(sender="alice", target="lead")
    unblock_send(sender="alice", target="lead")
    # Act
    flag = has_block(sender="alice", target="lead")
    # Assert
    assert flag is False


def test_unblock_returns_true_when_row_existed(pg_schema: str) -> None:
    # Arrange
    block_send(sender="alice", target="lead")
    # Act
    removed = unblock_send(sender="alice", target="lead")
    # Assert
    assert removed is True


def test_unblock_returns_false_when_row_absent(pg_schema: str) -> None:
    # Arrange — no prior block.
    # Act
    removed = unblock_send(sender="ghost", target="lead")
    # Assert
    assert removed is False


# ---------------------------------------------------------------------------
# Fail-loud — empty inputs rejected
# ---------------------------------------------------------------------------


def test_block_empty_sender_raises(pg_schema: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        block_send(sender="", target="lead")


def test_block_empty_target_raises(pg_schema: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        block_send(sender="alice", target="")


# ---------------------------------------------------------------------------
# PostgreSQL semantics (2026-08-20): the store hides, it does not delete
# ---------------------------------------------------------------------------
#
# `unblock_send` used to DELETE. The store has no delete — it has `hide`, which
# is the better primitive here (who unblocked whom, and when, survives) but
# introduces a state a plain DELETE did not have: a cleared pair is HIDDEN, not ABSENT.
# These pin the three-value branch that distinguishes them.


def _created_at(sender: str, target: str) -> float:
    """The stored timestamp, read back through the store the module writes to."""
    store = open_blocks_store()
    try:
        record = store.get({"sender_name": sender, "target_name": target})
        return float(record.values["created_at"])
    finally:
        store.close()


def test_reblocking_does_not_bump_the_timestamp(pg_schema: str) -> None:
    """The documented idempotence: "re-blocking leaves the row untouched".

    Falsifiable, and it is the one the old test only claimed: make the visible
    branch write instead of returning early and this goes RED.
    """
    # Arrange
    block_send(sender="alice", target="lead")
    first = _created_at("alice", "lead")
    time.sleep(0.01)
    # Act
    block_send(sender="alice", target="lead")
    # Assert
    assert _created_at("alice", "lead") == first


def test_reblocking_after_an_unblock_does_bump_the_timestamp(pg_schema: str) -> None:
    """An unblock followed by a block is a NEW decision, not a repeat.

    Dating it to the superseded block would misreport when the receiver made
    it. This is the case that separates "hidden" from "visible": collapse the
    two and one of these two tests must fail.
    """
    # Arrange
    block_send(sender="alice", target="lead")
    first = _created_at("alice", "lead")
    unblock_send(sender="alice", target="lead")
    time.sleep(0.01)
    # Act
    block_send(sender="alice", target="lead")
    # Assert
    assert _created_at("alice", "lead") > first


def test_unblocking_hides_the_record_rather_than_destroying_it(pg_schema: str) -> None:
    """The audit trail a DELETE destroyed."""
    # Arrange
    block_send(sender="alice", target="lead")
    unblock_send(sender="alice", target="lead")
    # Act
    store = open_blocks_store()
    try:
        hidden = store.is_hidden({"sender_name": "alice", "target_name": "lead"})
    finally:
        store.close()
    # Assert — True means "present but hidden"; None would mean it was erased.
    assert hidden is True


def test_init_returns_a_locator_naming_the_postgres_endpoint(pg_schema: str) -> None:
    """The previous implementation returned None. Naming where the state went is more
    useful: an operator can check it instead of assuming it."""
    # Arrange
    expected_scheme = "postgres"
    # Act
    locator = ensure_comms_blocks_table()
    # Assert
    assert expected_scheme in locator
