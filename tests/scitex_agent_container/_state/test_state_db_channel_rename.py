"""``rename_channel_events`` — a renamed agent keeps its message history.

This is the half of ``sac agents rename`` that ``_rename_db.NAME_COLUMNS``
used to carry, until ``channel_events`` became the last table to leave
SQLite (ADR-0023). ``rename_rows`` SKIPS a table absent from
``sqlite_master``, so leaving the two pairs behind would have made the
rename report success while every message the agent ever sent or received
stayed filed under the old name — the quietest of the three silent no-ops
that move produced, because nobody greps a history they were told had moved.

Real PostgreSQL through the ``pg_schema`` fixture; no mocks.
"""

from __future__ import annotations

from typing import Any

import pytest

from scitex_agent_container._state.state_db_channel import (
    list_since_id,
    persist_event,
    rename_channel_events,
    undo_rename_channel_events,
)
from scitex_agent_container._state.state_db_channel_store import (
    new_channel_connection,
    reset_channel_connection,
)


@pytest.fixture(autouse=True)
def _drop_cached_connection():
    """Close the process-wide handle around every test in this module."""
    reset_channel_connection()
    yield
    reset_channel_connection()


def _event(content: str) -> dict[str, Any]:
    return {
        "msg_id": "m-" + content,
        "from_agent": "alice",
        "content": content,
        "ts": 1_700_000_000.0,
    }


def _contents(target: str) -> list[str]:
    return [r["event"]["content"] for r in list_since_id(target=target, since_id=0)]


def test_history_follows_the_new_name(pg_schema: str) -> None:
    """Every row addressed to the old name is readable under the new one."""
    # Arrange
    for n in range(3):
        persist_event(target="old-name", event=_event(f"m{n}"))
    # Act
    rename_channel_events(old="old-name", new="new-name")
    # Assert
    assert _contents("new-name") == ["m0", "m1", "m2"]


def test_ids_are_preserved_when_the_new_name_is_free(pg_schema: str) -> None:
    """The ordinary case shifts NOTHING, which is what keeps a cursor valid.

    A consumer that dropped holding ``Last-Event-ID: 2`` reconnects under the
    new name asking for everything after 2. Renumbering would hand it a
    replay or a gap.
    """
    # Arrange
    for n in range(3):
        persist_event(target="old-name", event=_event(f"m{n}"))
    # Act
    rename_channel_events(old="old-name", new="new-name")
    # Assert
    assert [r["id"] for r in list_since_id(target="new-name", since_id=0)] == [1, 2, 3]


def test_the_sender_column_follows_the_rename_too(pg_schema: str) -> None:
    """Rows SENT BY the old name are re-attributed, not only rows sent TO it."""
    # Arrange
    persist_event(target="someone-else", event={"from_agent": "old-name", "ts": 1.0})
    # Act
    rename_channel_events(old="old-name", new="new-name")
    conn = new_channel_connection()
    try:
        sources = [
            r[0]
            for r in conn.execute(
                "SELECT source FROM sac_channel_events WHERE target = %s",
                ("someone-else",),
            ).fetchall()
        ]
    finally:
        conn.close()
    # Assert
    assert sources == ["new-name"]


def test_a_colliding_destination_shifts_the_incoming_ids(pg_schema: str) -> None:
    """Leftovers under the new name are not overwritten — ours go above them.

    ``(target, id)`` is the primary key, so two id spaces cannot merge. The
    previously-deleted agent's rows keep 1 and 2; the migrated ones become 3
    and 4.
    """
    # Arrange
    for n in range(2):
        persist_event(target="new-name", event=_event(f"stranger{n}"))
    for n in range(2):
        persist_event(target="old-name", event=_event(f"mine{n}"))
    # Act
    rename_channel_events(old="old-name", new="new-name")
    # Assert
    assert _contents("new-name") == ["stranger0", "stranger1", "mine0", "mine1"]


def test_the_cursor_carries_so_the_next_id_is_unused(pg_schema: str) -> None:
    """After a rename the next event under the new name gets a FRESH id."""
    # Arrange
    for n in range(3):
        persist_event(target="old-name", event=_event(f"m{n}"))
    rename_channel_events(old="old-name", new="new-name")
    # Act
    minted = persist_event(target="new-name", event=_event("after"))
    # Assert
    assert minted == 4


def test_undo_restores_the_old_name(pg_schema: str) -> None:
    """The rollback step puts every migrated row back where it was."""
    # Arrange
    for n in range(3):
        persist_event(target="old-name", event=_event(f"m{n}"))
    undo = rename_channel_events(old="old-name", new="new-name")
    # Act
    undo_rename_channel_events(undo)
    # Assert
    assert _contents("old-name") == ["m0", "m1", "m2"]


def test_undo_does_not_drag_a_stranger_row_along(pg_schema: str) -> None:
    """The trap a ``WHERE target = new`` undo would fall into.

    A previously deleted agent by the destination name can have left history
    behind. Rolling the rename back must not move that stranger's rows to the
    old name — which is why the undo is id-scoped, exactly as ``_rename_db``'s
    rowid capture is for the SQLite half.
    """
    # Arrange
    persist_event(target="new-name", event=_event("stranger"))
    persist_event(target="old-name", event=_event("mine"))
    undo = rename_channel_events(old="old-name", new="new-name")
    # Act
    undo_rename_channel_events(undo)
    # Assert
    assert _contents("new-name") == ["stranger"]


def test_nothing_to_rename_returns_none(pg_schema: str) -> None:
    """A name with no history yields no undo entry to push."""
    # Arrange
    persist_event(target="unrelated", event=_event("m0"))
    # Act
    result = rename_channel_events(old="never-existed", new="new-name")
    # Assert
    assert result is None
