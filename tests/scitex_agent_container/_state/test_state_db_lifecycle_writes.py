"""``record_instance_start`` / ``_stop`` and the lifecycle event log.

Extracted from ``test_state_db.py`` on 2026-08-28, when ``instances`` and
``events`` moved to per-host PostgreSQL. The split is along the fixture
seam rather than an arbitrary one: everything here needs ``pg_schema``
because every one of these calls now reaches the store, while what stays
in ``test_state_db.py`` is pure SQLite. The heartbeat cache and the GC
sweep — the other half of that extraction — live in
``test_state_db_gc_and_heartbeats.py``, which needs BOTH engines.

WHAT THE MIGRATION KILLED, and where it went:
``test_record_instance_start_writes_a_start_kind_event_row`` read the
event log with a raw ``SELECT * FROM events``. There is no such table; it
reads through ``instance_events()`` instead, which is the accessor
production reads through. Same property, one dialect.

PA-306: no mocks. Isolation is a throwaway PostgreSQL schema.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# record_instance_start / _stop
# ---------------------------------------------------------------------------


def test_record_instance_start_returns_uuid_like_string(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    # Act
    iid = record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    # Assert
    assert iid and len(iid) >= 32


def test_record_instance_start_inserts_single_active_instance_record(
    pg_schema: str,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Act
    record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    rows = list_active_instances()
    # Assert
    assert len(rows) == 1


@pytest.mark.parametrize(
    "field, expected",
    [
        ("name", "diag-test"),
        ("pid", 1234),
        ("host", "ywata-note-win"),
        ("ended_at", None),
    ],
)
def test_record_instance_start_record_carries_field_from_constructor_args(
    pg_schema: str, field: str, expected
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Act
    record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    row = list_active_instances()[0]
    # Assert
    assert row[field] == expected


def test_record_instance_start_returned_id_matches_the_active_record(
    pg_schema: str,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Act
    iid = record_instance_start(
        "diag-test", pid=1234, screen="diag-test", host="ywata-note-win"
    )
    # Assert
    assert list_active_instances()[0]["id"] == iid


def test_record_instance_start_appends_a_start_kind_event(pg_schema: str):
    # Arrange — read through the accessor, not through a table name. The
    # raw ``SELECT * FROM events`` this replaces would keep returning zero
    # rows against a SQLite table that no longer exists.
    from scitex_agent_container._state.state_db import (
        instance_events,
        record_instance_start,
    )

    # Act
    iid = record_instance_start("x", host="h")
    # Assert
    assert [e["kind"] for e in instance_events(iid)] == ["start"]


def test_record_instance_stop_appends_a_stop_kind_event(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import (
        instance_events,
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    # Act
    record_instance_stop(iid, exit_reason="stopped")
    # Assert — oldest first, so the stop is the SECOND entry.
    assert [e["kind"] for e in instance_events(iid)] == ["start", "stop"]


def test_a_stop_event_carries_its_exit_reason_in_the_payload(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import (
        instance_events,
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    # Act
    record_instance_stop(iid, exit_reason="superseded")
    # Assert
    stop = instance_events(iid)[-1]
    assert json.loads(stop["payload_json"])["exit_reason"] == "superseded"


def test_record_instance_stop_clears_the_active_instance_list(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    record_instance_stop(iid, exit_reason="stopped")
    # Act
    active_after = list_active_instances()
    # Assert
    assert active_after == []


def test_record_instance_stop_returns_true_on_first_call(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import (
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    # Act
    first = record_instance_stop(iid, exit_reason="stopped")
    # Assert
    assert first is True


def test_record_instance_stop_returns_false_on_second_call_for_idempotency(
    pg_schema: str,
):
    # Arrange
    from scitex_agent_container._state.state_db import (
        record_instance_start,
        record_instance_stop,
    )

    iid = record_instance_start("x", host="h")
    record_instance_stop(iid, exit_reason="stopped")
    # Act
    second = record_instance_stop(iid)
    # Assert
    assert second is False


