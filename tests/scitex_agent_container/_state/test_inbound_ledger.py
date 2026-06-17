"""Tests for the receiver-side inbound dispatch ledger
(``_state.inbound_ledger``) — the DB-backed bridge that carries a TUI
wake's requester identity from the host-side turn-bridge to the
in-container Stop hook that reports completion.

Real SQLite (a tmp ``state.db`` path, no mocks). Covers the
pending→reporting→reported lifecycle, FIFO claim order, the atomic
single-claim guarantee, and fail-loud validation. STX-TQ002 AAA markers
+ STX-TQ007 one assert + STX-TQ003 descriptive names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import inbound_ledger as ledger


def test_record_inbound_inserts_a_pending_row(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    # Act
    ledger.record_inbound(
        db_path=db, agent="figrecipe", from_agent="lead", dispatch_id="d1"
    )
    # Assert
    rows = ledger.list_inbound(agent="figrecipe", db_path=db)
    assert len(rows) == 1 and rows[0]["status"] == ledger.STATUS_PENDING


def test_record_inbound_rejects_empty_from_agent(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    # Act
    # Assert
    with pytest.raises(ValueError):
        ledger.record_inbound(db_path=db, agent="figrecipe", from_agent="")


def test_claim_oldest_pending_returns_the_fifo_oldest(tmp_path: Path) -> None:
    # Arrange — two dispatches, oldest first by ts.
    db = tmp_path / "state.db"
    ledger.record_inbound(
        db_path=db, agent="a", from_agent="lead", dispatch_id="old", ts=100.0
    )
    ledger.record_inbound(
        db_path=db, agent="a", from_agent="peer", dispatch_id="new", ts=200.0
    )
    # Act
    claimed = ledger.claim_oldest_pending(agent="a", db_path=db)
    # Assert
    assert claimed is not None and claimed["dispatch_id"] == "old"


def test_claim_oldest_pending_flips_row_to_reporting(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    ledger.record_inbound(db_path=db, agent="a", from_agent="lead", dispatch_id="d1")
    # Act
    claimed = ledger.claim_oldest_pending(agent="a", db_path=db)
    # Assert
    assert claimed is not None and claimed["status"] == ledger.STATUS_REPORTING


def test_claim_oldest_pending_none_when_queue_empty(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    # Act
    claimed = ledger.claim_oldest_pending(agent="a", db_path=db)
    # Assert
    assert claimed is None


def test_claim_is_atomic_so_one_row_is_claimed_once(tmp_path: Path) -> None:
    # Arrange — a single pending row, claimed once already.
    db = tmp_path / "state.db"
    ledger.record_inbound(db_path=db, agent="a", from_agent="lead", dispatch_id="d1")
    ledger.claim_oldest_pending(agent="a", db_path=db)
    # Act — a second claim finds nothing pending (no double-report).
    second = ledger.claim_oldest_pending(agent="a", db_path=db)
    # Assert
    assert second is None


def test_mark_reported_settles_a_claimed_row(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    row_id = ledger.record_inbound(
        db_path=db, agent="a", from_agent="lead", dispatch_id="d1"
    )
    ledger.claim_oldest_pending(agent="a", db_path=db)
    # Act
    settled = ledger.mark_reported(row_id, status=ledger.STATUS_REPORTED, db_path=db)
    # Assert
    assert settled is True


def test_mark_reported_rejects_a_non_terminal_status(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "state.db"
    row_id = ledger.record_inbound(
        db_path=db, agent="a", from_agent="lead", dispatch_id="d1"
    )
    # Act
    # Assert
    with pytest.raises(ValueError):
        ledger.mark_reported(row_id, status="pending", db_path=db)
