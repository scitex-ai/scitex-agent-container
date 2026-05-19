"""WI-1 — channel-event durability + replay (handoff §4).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-1 "Durability /
replay-on-reconnect"): every channel event must be persisted to
state.db so an event POSTed while no SSE subscriber is connected is
delivered on reconnect, and a kill+reconnect replays exactly the
missed events.

These tests drive the persistence primitives directly against a real
SQLite file under ``tmp_path`` — no mocks, per handoff §0 Hard rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_channel import (
    list_undelivered,
    list_since_id,
    mark_delivered,
    persist_event,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Initialise state.db under ``tmp_path`` and return its path."""
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


# ---------------------------------------------------------------------------
# Schema — channel_events table exists with the expected columns
# ---------------------------------------------------------------------------


def test_channel_events_table_exists(db_path: Path) -> None:
    # Arrange / Act
    with state_db.open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='channel_events'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


@pytest.mark.parametrize(
    "column",
    ["id", "target", "source", "kind", "content", "meta_json", "ts", "delivered_at"],
)
def test_channel_events_has_column(db_path: Path, column: str) -> None:
    # Arrange / Act
    with state_db.open_db(db_path) as conn:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(channel_events)").fetchall()
        }
    # Assert
    assert column in cols


# ---------------------------------------------------------------------------
# persist_event — minted row id, NULL delivered_at, full envelope
# ---------------------------------------------------------------------------


def _event(content: str = "hi", from_agent: str = "alice") -> dict:
    return {
        "msg_id": "m-fixed-1",
        "to_agent": "bob",
        "from_agent": from_agent,
        "ts": 1.0,
        "content": content,
        "priority": "normal",
        "requires_reply": False,
    }


def test_persist_event_returns_positive_row_id(db_path: Path) -> None:
    # Arrange
    event = _event()
    # Act
    row_id = persist_event(target="bob", event=event, db_path=db_path)
    # Assert
    assert isinstance(row_id, int) and row_id > 0


def test_persist_event_records_target(db_path: Path) -> None:
    # Arrange
    row_id = persist_event(target="bob", event=_event(), db_path=db_path)
    # Act
    with state_db.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT target FROM channel_events WHERE id=?", (row_id,)
        ).fetchone()
    # Assert
    assert row["target"] == "bob"


def test_persist_event_leaves_delivered_at_null(db_path: Path) -> None:
    # Arrange
    row_id = persist_event(target="bob", event=_event(), db_path=db_path)
    # Act
    with state_db.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT delivered_at FROM channel_events WHERE id=?", (row_id,)
        ).fetchone()
    # Assert
    assert row["delivered_at"] is None


def test_persist_event_stores_full_envelope_as_meta_json(db_path: Path) -> None:
    # Arrange
    import json

    event = _event()
    row_id = persist_event(target="bob", event=event, db_path=db_path)
    # Act
    with state_db.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT meta_json FROM channel_events WHERE id=?", (row_id,)
        ).fetchone()
    # Assert — round-trip preserves every field
    assert json.loads(row["meta_json"]) == event


# ---------------------------------------------------------------------------
# list_undelivered — fresh subscriber sees every event delivered_at IS NULL
# ---------------------------------------------------------------------------


def test_list_undelivered_returns_empty_when_nothing_persisted(db_path: Path) -> None:
    # Arrange / Act
    rows = list_undelivered(target="bob", db_path=db_path)
    # Assert
    assert rows == []


def test_list_undelivered_returns_one_row_after_one_publish(db_path: Path) -> None:
    # Arrange
    persist_event(target="bob", event=_event(), db_path=db_path)
    # Act
    rows = list_undelivered(target="bob", db_path=db_path)
    # Assert
    assert len(rows) == 1


def test_list_undelivered_orders_ascending_by_id(db_path: Path) -> None:
    # Arrange
    persist_event(target="bob", event=_event("first"), db_path=db_path)
    persist_event(target="bob", event=_event("second"), db_path=db_path)
    # Act
    rows = list_undelivered(target="bob", db_path=db_path)
    # Assert
    contents = [r["event"]["content"] for r in rows]
    assert contents == ["first", "second"]


def test_list_undelivered_filters_by_target(db_path: Path) -> None:
    # Arrange — two targets, only one subscriber
    persist_event(target="bob", event=_event("for-bob"), db_path=db_path)
    persist_event(target="alice", event=_event("for-alice"), db_path=db_path)
    # Act
    rows = list_undelivered(target="bob", db_path=db_path)
    # Assert
    assert len(rows) == 1 and rows[0]["event"]["content"] == "for-bob"


# ---------------------------------------------------------------------------
# mark_delivered — removes the row from the undelivered window
# ---------------------------------------------------------------------------


def test_mark_delivered_sets_delivered_at_to_a_float(db_path: Path) -> None:
    # Arrange
    row_id = persist_event(target="bob", event=_event(), db_path=db_path)
    # Act
    mark_delivered([row_id], db_path=db_path)
    with state_db.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT delivered_at FROM channel_events WHERE id=?", (row_id,)
        ).fetchone()
    # Assert
    assert isinstance(row["delivered_at"], float)


def test_mark_delivered_makes_event_disappear_from_undelivered(db_path: Path) -> None:
    # Arrange
    row_id = persist_event(target="bob", event=_event(), db_path=db_path)
    # Act
    mark_delivered([row_id], db_path=db_path)
    rows = list_undelivered(target="bob", db_path=db_path)
    # Assert
    assert rows == []


def test_mark_delivered_is_idempotent(db_path: Path) -> None:
    # Arrange
    row_id = persist_event(target="bob", event=_event(), db_path=db_path)
    # Act
    mark_delivered([row_id], db_path=db_path)
    mark_delivered([row_id], db_path=db_path)  # second call is a no-op
    rows = list_undelivered(target="bob", db_path=db_path)
    # Assert
    assert rows == []


def test_mark_delivered_only_affects_passed_ids(db_path: Path) -> None:
    # Arrange — two events, mark only one
    r1 = persist_event(target="bob", event=_event("a"), db_path=db_path)
    persist_event(target="bob", event=_event("b"), db_path=db_path)
    # Act
    mark_delivered([r1], db_path=db_path)
    rows = list_undelivered(target="bob", db_path=db_path)
    # Assert
    assert len(rows) == 1 and rows[0]["event"]["content"] == "b"


# ---------------------------------------------------------------------------
# list_since_id — Last-Event-ID replay path
# ---------------------------------------------------------------------------


def test_list_since_id_returns_events_strictly_after_cursor(db_path: Path) -> None:
    # Arrange — persist three events, mark them all delivered, then replay
    r1 = persist_event(target="bob", event=_event("a"), db_path=db_path)
    r2 = persist_event(target="bob", event=_event("b"), db_path=db_path)
    r3 = persist_event(target="bob", event=_event("c"), db_path=db_path)
    mark_delivered([r1, r2, r3], db_path=db_path)
    # Act
    rows = list_since_id(target="bob", since_id=r1, db_path=db_path)
    # Assert — only r2 and r3 returned (strictly > r1)
    ids = [r["id"] for r in rows]
    assert ids == [r2, r3]


def test_list_since_id_returns_all_when_cursor_is_zero(db_path: Path) -> None:
    # Arrange
    r1 = persist_event(target="bob", event=_event("a"), db_path=db_path)
    r2 = persist_event(target="bob", event=_event("b"), db_path=db_path)
    mark_delivered([r1, r2], db_path=db_path)
    # Act
    rows = list_since_id(target="bob", since_id=0, db_path=db_path)
    # Assert
    ids = [r["id"] for r in rows]
    assert ids == [r1, r2]
