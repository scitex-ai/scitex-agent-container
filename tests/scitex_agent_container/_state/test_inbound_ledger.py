"""Tests for the receiver-side inbound dispatch ledger
(``_state.inbound_ledger``) — the bridge that carries a TUI wake's requester
identity from the host-side turn-bridge to the in-container Stop hook that
reports completion.

Real PostgreSQL via ``scitex_dev.store``, isolated per test by the
``pg_schema`` fixture (no mocks). Covers the pending→reporting→reported
lifecycle, FIFO claim order, the atomic single-claim guarantee, the identity
collision the old autoincrement used to absorb, and fail-loud validation.
STX-TQ002 AAA markers + STX-TQ007 one assert + STX-TQ003 descriptive names.

``db_path`` IS GONE from every call. It named a file and there is no
file; every test that used to thread ``tmp_path / "state.db"`` now takes
``pg_schema`` instead, which is the seam that keeps one test's rows out of
another's — and out of the live fleet store.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state import inbound_ledger as ledger


def test_record_inbound_inserts_a_pending_row(pg_schema: str) -> None:
    # Arrange
    # Act
    ledger.record_inbound(agent="figrecipe", from_agent="lead", dispatch_id="d1")
    # Assert
    rows = ledger.list_inbound(agent="figrecipe")
    assert len(rows) == 1 and rows[0]["status"] == ledger.STATUS_PENDING


def test_record_inbound_rejects_empty_from_agent(pg_schema: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        ledger.record_inbound(agent="figrecipe", from_agent="")


def test_record_inbound_returns_the_identity_not_a_counter(pg_schema: str) -> None:
    """The autoincrement id is gone; the handle is the natural key.

    Pins the SHAPE rather than a value, because the whole point of the port is
    that nothing downstream may depend on the handle being a number.
    """
    # Arrange
    # Act
    handle = ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="d1")
    # Assert
    assert set(handle) == set(ledger.IDENTITY_FIELDS)


def test_claim_oldest_pending_returns_the_fifo_oldest(pg_schema: str) -> None:
    # Arrange — two dispatches, oldest first by ts.
    ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="old", ts=100.0)
    ledger.record_inbound(agent="a", from_agent="peer", dispatch_id="new", ts=200.0)
    # Act
    claimed = ledger.claim_oldest_pending(agent="a")
    # Assert
    assert claimed is not None and claimed["dispatch_id"] == "old"


def test_claim_oldest_pending_flips_row_to_reporting(pg_schema: str) -> None:
    # Arrange
    ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="d1")
    # Act
    claimed = ledger.claim_oldest_pending(agent="a")
    # Assert
    assert claimed is not None and claimed["status"] == ledger.STATUS_REPORTING


def test_claim_oldest_pending_none_when_queue_empty(pg_schema: str) -> None:
    # Arrange
    # Act
    claimed = ledger.claim_oldest_pending(agent="a")
    # Assert
    assert claimed is None


def test_claim_oldest_pending_ignores_another_agents_row(pg_schema: str) -> None:
    """CONTROL — the claim must be scoped, or one agent settles another's work.

    Without this, a claim that ignored ``agent`` would satisfy every other
    test in this file: they each use a single agent.
    """
    # Arrange
    ledger.record_inbound(agent="other", from_agent="lead", dispatch_id="d1")
    # Act
    claimed = ledger.claim_oldest_pending(agent="a")
    # Assert
    assert claimed is None


def test_claim_is_atomic_so_one_row_is_claimed_once(pg_schema: str) -> None:
    # Arrange — a single pending row, claimed once already.
    ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="d1")
    ledger.claim_oldest_pending(agent="a")
    # Act — a second claim finds nothing pending (no double-report).
    second = ledger.claim_oldest_pending(agent="a")
    # Assert
    assert second is None


def test_two_wakes_in_the_same_instant_are_both_kept(pg_schema: str) -> None:
    """The collision the old autoincrement used to absorb for free.

    Identity is ``(agent, from_agent, dispatch_id, ts)``. Two requester-bearing
    wakes with no dispatch id, from the same peer, at the SAME float instant
    name the same record — so without the microsecond retry the second would
    silently overwrite the first and one completion report would never be sent.

    Not credible in production (``time.time()`` is microsecond-resolution and a
    wake is a bus event) — which is exactly why it is pinned here rather than
    reasoned about: the old counter made duplicates free and the port must
    not quietly withdraw that.
    """
    # Arrange — same everything, including ts.
    ledger.record_inbound(agent="a", from_agent="lead", ts=100.0)
    ledger.record_inbound(agent="a", from_agent="lead", ts=100.0)
    # Act
    rows = ledger.list_inbound(agent="a")
    # Assert
    assert len(rows) == 2


def test_mark_reported_settles_a_claimed_row(pg_schema: str) -> None:
    # Arrange
    handle = ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="d1")
    ledger.claim_oldest_pending(agent="a")
    # Act
    settled = ledger.mark_reported(handle, status=ledger.STATUS_REPORTED)
    # Assert
    assert settled is True


def test_mark_reported_accepts_the_claimed_row_itself(pg_schema: str) -> None:
    """Production hands back the CLAIM, not the record. Both must work.

    ``flush_one_completion`` never sees ``record_inbound``'s return value — it
    settles whatever ``claim_oldest_pending`` gave it, which carries the data
    fields too. If the handle had to be exactly the identity mapping, the real
    call path would be the one that breaks.
    """
    # Arrange
    ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="d1")
    claimed = ledger.claim_oldest_pending(agent="a")
    # Act
    settled = ledger.mark_reported(claimed, status=ledger.STATUS_REPORTED)
    # Assert
    assert settled is True


def test_mark_reported_is_false_for_an_unknown_row(pg_schema: str) -> None:
    """CONTROL — settling must report whether it matched, not always True."""
    # Arrange — a well-formed handle for a record that was never written.
    handle = {"agent": "a", "from_agent": "lead", "dispatch_id": "ghost", "ts": 1.0}
    # Act
    settled = ledger.mark_reported(handle, status=ledger.STATUS_REPORTED)
    # Assert
    assert settled is False


def test_mark_reported_rejects_a_non_terminal_status(pg_schema: str) -> None:
    # Arrange
    handle = ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="d1")
    # Act
    # Assert
    with pytest.raises(ValueError):
        ledger.mark_reported(handle, status="pending")
