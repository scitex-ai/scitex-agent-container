"""Tests for the dispatch ledger (2026-05-22).

Every outbound dispatch gets a stable ``dispatch_id`` minted at the
sender and persisted to ``state.db.dispatches`` so dispatches can be
filtered + recalled later.

Conventions (mirroring test_state_db_turns_errors_heartbeats.py):

  * One assertion per test (STX-TQ007); related invariants collapse
    into ``pytest.parametrize``.
  * AAA markers (Arrange / Act / Assert).
  * No mocks / monkeypatch (STX-NM); real sqlite under ``tmp_path``,
    isolated via the ``db_path`` env fixture.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env so callers pick it up.

    Explicit env save/restore (no monkeypatch fixture, PA-306).
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


# ---------------------------------------------------------------------------
# Schema — the dispatches table is created on first use.
# ---------------------------------------------------------------------------


def test_init_ledger_schema_creates_dispatches_table(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import init_ledger_schema

    # Act
    init_ledger_schema()
    with sqlite3.connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    # Assert
    assert "dispatches" in names


def test_record_dispatch_creates_table_lazily_without_explicit_init(db_path: Path):
    # Arrange — never call init_ledger_schema; record_dispatch must
    # ensure the table itself.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    # Act
    record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    rows = list_dispatches()
    # Assert
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Mint — new_dispatch_id is a 32-char uuid4 hex.
# ---------------------------------------------------------------------------


def test_new_dispatch_id_is_32_char_hex():
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import new_dispatch_id

    # Act
    did = new_dispatch_id()
    # Assert
    assert len(did) == 32 and all(c in "0123456789abcdef" for c in did)


def test_new_dispatch_id_is_unique_across_calls():
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import new_dispatch_id

    # Act
    ids = {new_dispatch_id() for _ in range(1000)}
    # Assert
    assert len(ids) == 1000


# ---------------------------------------------------------------------------
# Round-trip — record_dispatch writes a row SELECT can recover.
# ---------------------------------------------------------------------------


def test_record_dispatch_returns_the_minted_id(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    # Act
    did = record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    # Assert
    assert len(did) == 32


def test_record_dispatch_honours_supplied_id(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    # Act
    did = record_dispatch(
        from_agent="alice", to_agent="bob", text="hi", dispatch_id="deadbeef"
    )
    # Assert
    assert did == "deadbeef"


def test_record_dispatch_round_trips_to_agent(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["to_agent"] == "bob"


def test_record_dispatch_round_trips_conversation_id(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="hi", conversation_id="conv-7")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["conversation_id"] == "conv-7"


def test_record_dispatch_defaults_status_to_sent(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_SENT,
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == STATUS_SENT


def test_record_dispatch_allows_null_agents(db_path: Path):
    # Arrange — a script driving post_turn outside an agent has no
    # SAC_NAME; the ledger row must still record (from_agent=None).
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent=None, to_agent=None, text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["from_agent"] is None


# ---------------------------------------------------------------------------
# text_summary truncation — long bodies are clipped to the first 500 chars.
# ---------------------------------------------------------------------------


def test_record_dispatch_truncates_long_text_summary(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="x" * 5000)
    # Act
    rows = list_dispatches()
    # Assert
    assert len(rows[0]["text_summary"]) == 500


def test_record_dispatch_keeps_short_text_intact(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="short")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["text_summary"] == "short"


# ---------------------------------------------------------------------------
# Status validation — unknown statuses fail loudly (no silent write).
# ---------------------------------------------------------------------------


def test_record_dispatch_rejects_unknown_status(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    # Act
    ctx = pytest.raises(ValueError, match="unknown dispatch status")
    # Assert
    with ctx:
        record_dispatch(from_agent="a", to_agent="b", text="hi", status="bogus")


def test_update_dispatch_status_rejects_unknown_status(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    ctx = pytest.raises(ValueError, match="unknown dispatch status")
    # Assert
    with ctx:
        update_dispatch_status(did, "bogus")


# ---------------------------------------------------------------------------
# Status transition — sent -> delivered/timeout/failed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal",
    ["delivered", "timeout", "failed"],
)
def test_update_dispatch_status_transitions_to_terminal(db_path: Path, terminal: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    update_dispatch_status(did, terminal)
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == terminal


def test_update_dispatch_status_returns_true_on_match(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    matched = update_dispatch_status(did, "delivered")
    # Assert
    assert matched is True


def test_update_dispatch_status_returns_false_on_missing_row(db_path: Path):
    # Arrange — no row with this id exists.
    from scitex_agent_container._state.dispatch_ledger import update_dispatch_status

    # Act
    matched = update_dispatch_status("does-not-exist", "delivered")
    # Assert
    assert matched is False


# ---------------------------------------------------------------------------
# Query filters — from / to / status / conversation / since / limit.
# ---------------------------------------------------------------------------


def test_list_dispatches_filter_by_from_agent(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="alice", to_agent="x", text="1")
    record_dispatch(from_agent="bob", to_agent="x", text="2")
    record_dispatch(from_agent="alice", to_agent="y", text="3")
    # Act
    rows = list_dispatches(from_agent="alice")
    # Assert
    assert {r["text_summary"] for r in rows} == {"1", "3"}


def test_list_dispatches_filter_by_to_agent(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="bob", text="1")
    record_dispatch(from_agent="a", to_agent="carol", text="2")
    # Act
    rows = list_dispatches(to_agent="bob")
    # Assert
    assert [r["text_summary"] for r in rows] == ["1"]


def test_list_dispatches_filter_by_status(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="1", status="sent")
    record_dispatch(from_agent="a", to_agent="b", text="2", status="failed")
    # Act
    rows = list_dispatches(status="failed")
    # Assert
    assert [r["text_summary"] for r in rows] == ["2"]


def test_list_dispatches_filter_by_conversation_id(db_path: Path):
    # Arrange — "which dispatches belonged to this conversation?"
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="1", conversation_id="c1")
    record_dispatch(from_agent="a", to_agent="b", text="2", conversation_id="c2")
    record_dispatch(from_agent="a", to_agent="b", text="3", conversation_id="c1")
    # Act
    rows = list_dispatches(conversation_id="c1")
    # Assert
    assert {r["text_summary"] for r in rows} == {"1", "3"}


def test_list_dispatches_filter_by_since(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="old", ts=100.0)
    record_dispatch(from_agent="a", to_agent="b", text="new", ts=200.0)
    # Act
    rows = list_dispatches(since=150.0)
    # Assert
    assert [r["text_summary"] for r in rows] == ["new"]


def test_list_dispatches_combined_filters_are_anded(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="alice", to_agent="bob", text="hit", status="failed")
    record_dispatch(from_agent="alice", to_agent="bob", text="miss-status")
    record_dispatch(from_agent="zed", to_agent="bob", text="miss-from", status="failed")
    # Act
    rows = list_dispatches(from_agent="alice", to_agent="bob", status="failed")
    # Assert
    assert [r["text_summary"] for r in rows] == ["hit"]


def test_list_dispatches_orders_newest_first(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="first", ts=10.0)
    record_dispatch(from_agent="a", to_agent="b", text="second", ts=20.0)
    # Act
    rows = list_dispatches()
    # Assert
    assert [r["text_summary"] for r in rows] == ["second", "first"]


def test_list_dispatches_respects_limit(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    for i in range(5):
        record_dispatch(from_agent="a", to_agent="b", text=str(i), ts=float(i))
    # Act
    rows = list_dispatches(limit=2)
    # Assert
    assert len(rows) == 2


def test_list_dispatches_empty_when_no_match(db_path: Path):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="x")
    # Act
    rows = list_dispatches(from_agent="nobody")
    # Assert
    assert rows == []
