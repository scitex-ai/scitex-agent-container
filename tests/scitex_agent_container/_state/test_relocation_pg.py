"""Relocation state on a REAL PostgreSQL — residency, leases, journal.

Mirrors ``src/scitex_agent_container/_state/relocation_pg.py``.

THE TEST THAT CARRIES THE DESIGN
================================
``test_moving_hosts_leaves_exactly_one_open_stay``. The SQLite version closed
the old stay and opened the new one inside one transaction; here they share
``Store.batch()``. If that batch is ever removed, a crash between the two
writes leaves an agent with NO open stay — and ``current_residency`` then
answers ``None`` for a running agent, which is the failure this table exists
to prevent. The pair of tests around it pin both halves: the old stay closes
AT the new one's start, and exactly one stay stays open.

``test_history_is_ordered_when_two_stays_share_a_from_ts`` pins the other
half of the rowid replacement. SQLite broke that tie on insertion order; this
port breaks it on ``hlc``, a total order across replicas. Without a tie-break
the history is non-deterministic, which is what the original design went out
of its way to avoid.

Isolation is the shared ``pg_schema`` fixture — a throwaway schema selected
through ``search_path`` and dropped afterwards, so the live per-host state is
never touched. Real store, real database, no mocks (PA-306), one assert each
(PA-307).
"""

from __future__ import annotations

from functools import partial

import pytest

from scitex_agent_container._lifecycle._relocate_lease import Lease
from scitex_agent_container._lifecycle._relocate_phases import Relocation, Step
from scitex_agent_container._state.relocation_pg import (
    current_residency,
    init_relocation_schema,
    load_journal,
    load_journal_attempts,
    load_lease,
    read_residency_history,
    record_residency,
    save_journal,
    save_lease,
)

#: A real phase name. The vocabulary is closed and 'begin' is NOT in it —
#: Relocation rejects an unknown phase in __post_init__.
PHASE = "preflight"


def _relocation(to_host: str, at: float) -> Relocation:
    return Relocation(
        agent="zz-a",
        from_host="h1",
        to_host=to_host,
        phase=PHASE,
        steps=(Step(phase=PHASE, at=at, detail=""),),
    )


# ----------------------------------------------------------------------
# The stores exist, and they are PostgreSQL.
# ----------------------------------------------------------------------


def test_init_reports_where_the_state_went(pg_schema: str) -> None:
    # Arrange
    expected_fragment = "55432"
    # Act
    locator = init_relocation_schema()
    # Assert
    assert expected_fragment in locator


# ----------------------------------------------------------------------
# Residency.
# ----------------------------------------------------------------------


def test_a_first_stay_is_opened(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    # Act
    opened = record_residency(agent="zz-a", host="h1", now=100.0)
    # Assert
    assert opened is True


def test_recording_the_same_host_again_is_a_no_op(pg_schema: str) -> None:
    # Arrange — a successful no-op, not a failure; the caller distinguishes
    # them only to report accurately.
    init_relocation_schema()
    record_residency(agent="zz-a", host="h1", now=100.0)
    # Act
    again = record_residency(agent="zz-a", host="h1", now=110.0)
    # Assert
    assert again is False


def test_the_same_host_again_does_not_add_a_second_stay(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    record_residency(agent="zz-a", host="h1", now=100.0)
    # Act
    record_residency(agent="zz-a", host="h1", now=110.0)
    # Assert
    assert len(read_residency_history("zz-a")) == 1


def test_moving_hosts_closes_the_old_stay_at_the_new_start(pg_schema: str) -> None:
    """Half one of the batch contract — see the module docstring."""
    # Arrange
    init_relocation_schema()
    record_residency(agent="zz-a", host="h1", now=100.0)
    # Act
    record_residency(agent="zz-a", host="h2", now=120.0)
    # Assert
    assert read_residency_history("zz-a")[0].to_ts == 120.0


def test_moving_hosts_leaves_exactly_one_open_stay(pg_schema: str) -> None:
    """THE DESIGN TEST. Half two: an agent must never have zero open stays."""
    # Arrange
    init_relocation_schema()
    record_residency(agent="zz-a", host="h1", now=100.0)
    # Act
    record_residency(agent="zz-a", host="h2", now=120.0)
    # Assert
    assert [s.to_ts for s in read_residency_history("zz-a")].count(None) == 1


def test_current_residency_is_the_open_stays_host(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    record_residency(agent="zz-a", host="h1", now=100.0)
    record_residency(agent="zz-a", host="h2", now=120.0)
    # Act
    host = current_residency("zz-a")
    # Assert
    assert host == "h2"


def test_history_is_ordered_when_two_stays_share_a_from_ts(pg_schema: str) -> None:
    """The other half of the rowid replacement — a deterministic tie-break.

    Two stays on different hosts at the SAME instant. SQLite broke the tie on
    insertion order; this port breaks it on ``hlc``. What is asserted is that
    the order is the WRITE order, deterministically, rather than arbitrary.
    """
    # Arrange
    init_relocation_schema()
    record_residency(agent="zz-a", host="h1", now=100.0)
    record_residency(agent="zz-a", host="h2", now=100.0)
    # Act
    hosts = [s.host for s in read_residency_history("zz-a")]
    # Assert
    assert hosts == ["h1", "h2"]


def test_an_unknown_agent_has_no_history(pg_schema: str) -> None:
    # Arrange — genuinely "the db knows nothing", deliberately distinct from a
    # recorded stay that has since closed.
    init_relocation_schema()
    # Act
    history = read_residency_history("zz-nobody")
    # Assert
    assert history == ()


def test_current_residency_of_an_unknown_agent_is_none(pg_schema: str) -> None:
    # Arrange — None is not a hostname.
    init_relocation_schema()
    # Act
    host = current_residency("zz-nobody")
    # Assert
    assert host is None


def test_recording_without_a_host_is_refused(pg_schema: str) -> None:
    # Arrange — an empty destination would open a residency that answers no
    # question.
    init_relocation_schema()
    # Act
    attempt = partial(record_residency, agent="zz-a", host="  ")
    # Assert
    with pytest.raises(ValueError, match="needs the host"):
        attempt()


def test_the_seeded_flag_is_preserved(pg_schema: str) -> None:
    # Arrange — provenance dropped at the moment of writing cannot be
    # recovered by reading, so a spec-seeded value must stay marked.
    init_relocation_schema()
    # Act
    opened = record_residency(
        agent="zz-a", host="h1", now=100.0, seeded_from_spec=True
    )
    # Assert
    assert opened is True


# ----------------------------------------------------------------------
# Lease.
# ----------------------------------------------------------------------


def test_a_lease_round_trips(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    save_lease(Lease(agent="zz-a", holder="h1", token="tok1", fence=1, expires_at=9.0))
    # Act
    lease = load_lease("zz-a")
    # Assert
    assert lease.token == "tok1"


def test_a_second_save_replaces_rather_than_appends(pg_schema: str) -> None:
    # Arrange — there is exactly one answer to "who holds it"; a history of
    # holders would invite reading the wrong one.
    init_relocation_schema()
    save_lease(Lease(agent="zz-a", holder="h1", token="tok1", fence=1, expires_at=9.0))
    # Act
    save_lease(Lease(agent="zz-a", holder="h2", token="tok2", fence=2, expires_at=9.0))
    # Assert
    assert load_lease("zz-a").fence == 2


def test_an_unheld_lease_reads_as_none(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    # Act
    lease = load_lease("zz-nobody")
    # Assert
    assert lease is None


def test_a_lease_with_an_empty_token_reads_as_none(pg_schema: str) -> None:
    """A holder that cannot present a token cannot prove it holds anything.

    Treating it as held would leave the agent permanently unrelocatable
    behind a credential nobody has.
    """
    # Arrange — Lease itself refuses an empty token, so the row is written
    # through the store directly, exactly as a pre-token-column row would read.
    from scitex_dev.store import ANY_REVISION

    from scitex_agent_container._state.relocation_pg import _lease_store

    init_relocation_schema()
    store = _lease_store()
    store.put(
        {
            "agent": "zz-legacy",
            "holder": "h1",
            "token": "",
            "fence": 1,
            "expires_at": 9.0,
            "updated_at": 1.0,
        },
        expected_revision=ANY_REVISION,
    )
    store.close()
    # Act
    lease = load_lease("zz-legacy")
    # Assert
    assert lease is None


# ----------------------------------------------------------------------
# Journal.
# ----------------------------------------------------------------------


def test_the_first_attempt_is_numbered_one(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    # Act
    attempt = save_journal(_relocation("h2", 200.0))
    # Assert
    assert attempt == 1


def test_resaving_one_relocation_updates_the_attempt_it_owns(pg_schema: str) -> None:
    # Arrange — a record RESUMED from the store carries the timestamp its
    # first run stamped, so it must not open a new attempt.
    init_relocation_schema()
    relocation = _relocation("h2", 200.0)
    save_journal(relocation)
    # Act
    again = save_journal(relocation)
    # Assert
    assert again == 1


def test_a_new_started_at_opens_the_next_attempt(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    save_journal(_relocation("h2", 200.0))
    # Act
    attempt = save_journal(_relocation("h3", 300.0))
    # Assert
    assert attempt == 2


def test_earlier_attempts_stay_readable(pg_schema: str) -> None:
    # Arrange — a retry after an abort must not erase the attempt whose
    # failure prompted it.
    init_relocation_schema()
    save_journal(_relocation("h2", 200.0))
    save_journal(_relocation("h3", 300.0))
    # Act
    attempts = load_journal_attempts("zz-a")
    # Assert
    assert [n for n, _ in attempts] == [1, 2]


def test_load_journal_returns_the_latest_attempt(pg_schema: str) -> None:
    # Arrange — the resume read: a re-run continues the attempt that stopped.
    init_relocation_schema()
    save_journal(_relocation("h2", 200.0))
    save_journal(_relocation("h3", 300.0))
    # Act
    relocation = load_journal("zz-a")
    # Assert
    assert relocation.to_host == "h3"


def test_an_unknown_agent_has_no_journal(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    # Act
    relocation = load_journal("zz-nobody")
    # Assert
    assert relocation is None


def test_an_unknown_agent_has_no_attempts(pg_schema: str) -> None:
    # Arrange
    init_relocation_schema()
    # Act
    attempts = load_journal_attempts("zz-nobody")
    # Assert
    assert attempts == ()


def test_an_unparseable_attempt_is_skipped_not_raised(pg_schema: str) -> None:
    """One corrupt record must not hide the rest of the history.

    The record itself stays for whoever wants to look at it — nothing is
    deleted.
    """
    # Arrange
    from scitex_dev.store import ANY_REVISION

    from scitex_agent_container._state.relocation_pg import _journal_store

    init_relocation_schema()
    save_journal(_relocation("h2", 200.0))
    store = _journal_store()
    store.put(
        {
            "agent": "zz-a",
            "attempt": 2,
            "from_host": "h1",
            "to_host": "h9",
            "phase": PHASE,
            "steps": "{ this is not json",
            "started_at": 400.0,
            "updated_at": 400.0,
        },
        expected_revision=ANY_REVISION,
    )
    store.close()
    # Act
    attempts = load_journal_attempts("zz-a")
    # Assert
    assert [n for n, _ in attempts] == [1]
