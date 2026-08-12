"""Attempts accumulate; a lease that cannot present a token is not a lease.

Two properties of the durable half, both learned the same way: a journal keyed on
the agent alone erased the attempt whose failure prompted the retry, and a lease
row with no token column could not be rebuilt into a ``Lease`` at all — the
persistence layer for a carefully pure decision module was effectively
write-only.
"""

from __future__ import annotations

import time

import pytest

from scitex_agent_container._lifecycle._relocate_lease import Lease
from scitex_agent_container._lifecycle._relocate_phases import begin
from scitex_agent_container._state.state_db_relocation import (
    init_relocation_schema,
    load_journal,
    load_journal_attempts,
    load_lease,
    save_journal,
    save_lease,
)


@pytest.fixture()
def db(tmp_path):
    """A real sqlite file, addressed explicitly rather than via the environment."""
    path = tmp_path / "state.db"
    init_relocation_schema(path)
    return path


def _attempt(agent: str, *, at: float):
    return begin(agent=agent, from_host="src", to_host="tgt", now=at)


def test_the_first_save_opens_attempt_one(db) -> None:
    # Arrange
    first = _attempt("mover", at=1000.0)
    # Act
    attempt = save_journal(first, db_path=db)
    # Assert
    assert attempt == 1


def test_re_saving_the_same_relocation_stays_on_its_own_attempt(db) -> None:
    # Arrange: a resumed journal carries the opening moment its first run
    # stamped, so it must update the row it already owns rather than opening a
    # second attempt on every phase transition.
    first = _attempt("mover", at=1000.0)
    save_journal(first, db_path=db)
    # Act
    again = save_journal(first, db_path=db)
    # Assert
    assert again == 1


def test_a_relocation_opened_afresh_becomes_the_next_attempt(db) -> None:
    # Arrange: THE bug. A re-run after an abort used to overwrite the row that
    # recorded the failure prompting it.
    save_journal(_attempt("mover", at=1000.0), db_path=db)
    # Act
    second = save_journal(_attempt("mover", at=2000.0), db_path=db)
    # Assert
    assert second == 2


def test_the_earlier_attempt_survives_the_retry(db) -> None:
    # Arrange
    save_journal(_attempt("mover", at=1000.0), db_path=db)
    save_journal(_attempt("mover", at=2000.0), db_path=db)
    # Act
    history = load_journal_attempts("mover", db_path=db)
    # Assert
    assert [n for n, _ in history] == [1, 2]


def test_the_attempts_are_returned_oldest_first(db) -> None:
    # Arrange
    save_journal(_attempt("mover", at=1000.0), db_path=db)
    save_journal(_attempt("mover", at=2000.0), db_path=db)
    # Act
    history = load_journal_attempts("mover", db_path=db)
    # Assert
    assert [r.started_at for _, r in history] == [1000.0, 2000.0]


def test_the_resume_read_returns_the_latest_attempt(db) -> None:
    # Arrange: a re-run continues the attempt that stopped, never an older one.
    save_journal(_attempt("mover", at=1000.0), db_path=db)
    save_journal(_attempt("mover", at=2000.0), db_path=db)
    # Act
    latest = load_journal("mover", db_path=db)
    # Assert
    assert latest.started_at == 2000.0


def test_another_agents_attempts_are_counted_separately(db) -> None:
    # Arrange
    save_journal(_attempt("mover", at=1000.0), db_path=db)
    save_journal(_attempt("mover", at=2000.0), db_path=db)
    # Act
    other = save_journal(_attempt("other", at=3000.0), db_path=db)
    # Assert
    assert other == 1


def test_a_lease_is_read_back_with_the_token_it_was_saved_with(db) -> None:
    # Arrange: every lease verb except claim requires the caller to PRESENT the
    # token, so a token that does not survive the round trip makes the whole
    # decision module unreachable.
    lease = Lease(
        agent="mover",
        holder="src",
        token="tok-abc",
        expires_at=time.time() + 60,
        fence=3,
    )
    save_lease(lease, db_path=db)
    # Act
    loaded = load_lease("mover", db_path=db)
    # Assert
    assert loaded.token == "tok-abc"


def test_the_fence_survives_the_round_trip(db) -> None:
    # Arrange: the fence is what actually fences — a holder that comes back reads
    # this row, sees a fence above its own, and knows it is out.
    lease = Lease(
        agent="mover",
        holder="src",
        token="tok-abc",
        expires_at=time.time() + 60,
        fence=3,
    )
    save_lease(lease, db_path=db)
    # Act
    loaded = load_lease("mover", db_path=db)
    # Assert
    assert loaded.fence == 3


def test_a_second_save_replaces_rather_than_appends(db) -> None:
    # Arrange: there is exactly one answer to "who holds it"; a history of
    # holders would invite reading the wrong one.
    save_lease(
        Lease(agent="mover", holder="src", token="t1", expires_at=1.0, fence=0),
        db_path=db,
    )
    save_lease(
        Lease(agent="mover", holder="tgt", token="t2", expires_at=2.0, fence=1),
        db_path=db,
    )
    # Act
    loaded = load_lease("mover", db_path=db)
    # Assert
    assert loaded.holder == "tgt"


def test_an_agent_with_no_lease_row_reads_as_none(db) -> None:
    # Arrange: "nobody has ever held it" — which check_write treats as UNKNOWN
    # and must resolve deliberately, never as a default.
    # Act
    loaded = load_lease("never-held", db_path=db)
    # Assert
    assert loaded is None
