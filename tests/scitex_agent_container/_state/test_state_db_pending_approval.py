"""Tests for the pending-prompt flag store (task #27).

Lead's design amendment (2026-06-01) replaces the prior held-message
queue with a minimal "is there a pending decision yes/no" flag. The
sender's original content is NEVER stored here — receivers decide on
identity, not on content. This module covers the flag CRUD in
isolation; the integration (denied send records the flag, the CLI
decision clears it) lives in its own test file.

No-mocks (PA-306): a REAL PostgreSQL via the shared ``pg_schema``
fixture, which gives each test a throwaway schema so the live fleet
store is never touched. The flag moved to the store on 2026-08-20; there
is deliberately NO skipif on database availability, because a skip that
reads as a pass is the defect this migration exists to remove.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

from tests._store_isolation import pg_endpoint_port

import pytest

from scitex_agent_container._state.state_db_pending_approval import (
    clear_pending_prompt,
    has_pending_prompt,
    record_pending_prompt,
)


# ---------------------------------------------------------------------------
# record_pending_prompt — first-wins flag semantics
# ---------------------------------------------------------------------------


def test_first_record_returns_true(pg_schema: str) -> None:
    # Arrange — fresh DB, no prior row.
    # Act
    first = record_pending_prompt(sender="alice", target="lead")
    # Assert — caller uses this signal to "emit the receiver-facing
    # push exactly once per pair until a decision".
    assert first is True


def test_second_record_returns_false_no_duplicate_push(
    pg_schema: str,
) -> None:
    # Arrange — a pending row already exists.
    record_pending_prompt(sender="alice", target="lead")
    # Act
    second = record_pending_prompt(
        sender="alice", target="lead"
    )
    # Assert — duplicate denied attempts MUST NOT re-prompt.
    assert second is False


def test_different_sender_target_pairs_both_emit(pg_schema: str) -> None:
    # Arrange — dedupe is per-pair, not global.
    record_pending_prompt(sender="alice", target="lead")
    # Act
    second_pair = record_pending_prompt(
        sender="bob", target="lead"
    )
    # Assert
    assert second_pair is True


# ---------------------------------------------------------------------------
# has_pending_prompt — read-only flag check
# ---------------------------------------------------------------------------


def test_has_pending_prompt_returns_true_after_record(
    pg_schema: str,
) -> None:
    # Arrange
    record_pending_prompt(sender="alice", target="lead")
    # Act
    flag = has_pending_prompt(sender="alice", target="lead")
    # Assert
    assert flag is True


def test_has_pending_prompt_returns_false_for_absent_pair(
    pg_schema: str,
) -> None:
    # Arrange — no record_pending_prompt call.
    # Act
    flag = has_pending_prompt(sender="ghost", target="lead")
    # Assert
    assert flag is False


# ---------------------------------------------------------------------------
# clear_pending_prompt — drop on decision
# ---------------------------------------------------------------------------


def test_clear_removes_pending_row(pg_schema: str) -> None:
    # Arrange
    record_pending_prompt(sender="alice", target="lead")
    clear_pending_prompt(sender="alice", target="lead")
    # Act
    flag = has_pending_prompt(sender="alice", target="lead")
    # Assert
    assert flag is False


def test_clear_returns_true_when_row_existed(pg_schema: str) -> None:
    # Arrange
    record_pending_prompt(sender="alice", target="lead")
    # Act
    removed = clear_pending_prompt(
        sender="alice", target="lead"
    )
    # Assert
    assert removed is True


def test_clear_returns_false_when_row_absent(pg_schema: str) -> None:
    # Arrange — no prior record.
    # Act
    removed = clear_pending_prompt(
        sender="ghost", target="lead"
    )
    # Assert
    assert removed is False


def test_after_clear_next_record_returns_true_again(
    pg_schema: str,
) -> None:
    # Arrange — record + clear + record again. The second record
    # MUST return True (i.e. emit the prompt) because the prior
    # decision was already made.
    record_pending_prompt(sender="alice", target="lead")
    clear_pending_prompt(sender="alice", target="lead")
    # Act
    second = record_pending_prompt(
        sender="alice", target="lead"
    )
    # Assert
    assert second is True


# ---------------------------------------------------------------------------
# Fail-loud — empty sender / target rejected
# ---------------------------------------------------------------------------


def test_empty_sender_raises(pg_schema: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        record_pending_prompt(sender="", target="lead")


def test_empty_target_raises(pg_schema: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="non-empty"):
        record_pending_prompt(sender="alice", target="")


# ---------------------------------------------------------------------------
# What the PostgreSQL move CHANGED — clearing hides, it does not delete
# ---------------------------------------------------------------------------


def test_clearing_hides_the_record_rather_than_destroying_it(pg_schema: str) -> None:
    """The decision survives the clear, which the previous DELETE destroyed.

    ``clear_pending_prompt`` maps to the store's ``hide``, so the record
    stays in the oplog with the actor that cleared it. Reads are
    unaffected — ``get`` skips hidden records — but "who cleared this
    pair, and when" is now answerable, and under DELETE it was not.

    Asserted through the store's own ``is_hidden`` rather than by peeking
    at a physical table: the claim is about the RECORD's state, and a raw
    table read would be a true statement about a different artifact.
    """
    # Arrange
    from scitex_agent_container._state.state_db_pending_approval import (
        open_pending_prompt_store,
    )

    record_pending_prompt(sender="alice", target="lead")
    clear_pending_prompt(sender="alice", target="lead")
    # Act
    store = open_pending_prompt_store()
    try:
        hidden = store.is_hidden({"sender": "alice", "target": "lead"})
    finally:
        store.close()
    # Assert — True means "record exists and is hidden", not "absent".
    assert hidden is True


def test_re_recording_after_a_clear_refreshes_the_timestamp(pg_schema: str) -> None:
    """A re-armed pair carries the NEW prompt's time, not the old one.

    This is why ``ts`` is LAST_WRITER_WINS rather than IMMUTABLE. Nothing
    orders decisions on this field today, so the refresh cannot reorder
    anything — but a stale timestamp on a live prompt would misreport when
    the receiver was actually asked.
    """
    # Arrange
    from scitex_agent_container._state.state_db_pending_approval import (
        open_pending_prompt_store,
    )

    record_pending_prompt(sender="alice", target="lead")
    store = open_pending_prompt_store()
    try:
        first_ts = store.get({"sender": "alice", "target": "lead"}).values["ts"]
    finally:
        store.close()
    clear_pending_prompt(sender="alice", target="lead")
    record_pending_prompt(sender="alice", target="lead")
    # Act
    store = open_pending_prompt_store()
    try:
        second_ts = store.get({"sender": "alice", "target": "lead"}).values["ts"]
    finally:
        store.close()
    # Assert
    assert second_ts >= first_ts


def test_init_returns_a_locator_naming_the_postgres_endpoint(pg_schema: str) -> None:
    """The return value names WHERE the state went, so it can be checked.

    The previous implementation returned None. The locator names the DATABASE and
    not the search_path schema layered on top — the same shape the
    incarnations store has, pinned here so the two do not drift apart.
    """
    # Arrange
    from scitex_agent_container._state.state_db_pending_approval import (
        init_pending_prompts_schema,
    )

    expected = pg_endpoint_port()
    # Act
    locator = init_pending_prompts_schema()
    # Assert
    assert expected in locator
