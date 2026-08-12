"""Residency rows: at most one open stay, idempotent on a re-run, nothing deleted.

The invariant is not "we try not to write two open stays" — it is that living on
two hosts at once must not be representable, so the closing write and the opening
write happen in one transaction.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_relocation import (
    current_residency,
    init_relocation_schema,
    load_journal,
    read_residency_history,
    record_residency,
    save_journal,
)


@pytest.fixture()
def db(tmp_path):
    """A real sqlite file in ``tmp_path``, addressed explicitly.

    Every function under test takes ``db_path``, so the store is chosen by
    passing it rather than by rewriting the environment out from under the
    production resolver — which would test the resolver's absence, not the rows.
    """
    path = tmp_path / "state.db"
    init_relocation_schema(path)
    return path


def test_an_agent_the_table_never_heard_of_has_no_history(db) -> None:
    # Arrange: genuinely "the db knows nothing", which is what lets a legacy spec
    # host: seed it once — deliberately distinct from a stay that has closed.
    # Act
    history = read_residency_history("nobody", db_path=db)
    # Assert
    assert history == ()


def test_recording_a_move_opens_a_stay(db) -> None:
    # Arrange: the DONE-phase write. This IS the thing that moves an agent's host.
    record_residency(agent="a", host="h1", now=100.0, db_path=db)
    # Act
    host = current_residency("a", db_path=db)
    # Assert
    assert host == "h1"


def test_a_second_move_closes_the_first_stay(db) -> None:
    # Arrange: "living on two hosts at once" must be unrepresentable, not merely
    # discouraged.
    record_residency(agent="a", host="h1", now=100.0, db_path=db)
    record_residency(agent="a", host="h2", now=200.0, db_path=db)
    # Act
    open_stays = [r for r in read_residency_history("a", db_path=db) if r.to_ts is None]
    # Assert
    assert len(open_stays) == 1


def test_the_open_stay_after_a_move_is_the_new_host(db) -> None:
    # Arrange: the answer the whole feature exists to change.
    record_residency(agent="a", host="h1", now=100.0, db_path=db)
    record_residency(agent="a", host="h2", now=200.0, db_path=db)
    # Act
    host = current_residency("a", db_path=db)
    # Assert
    assert host == "h2"


def test_the_earlier_stay_is_kept_rather_than_deleted(db) -> None:
    # Arrange: the migration fact is the point — after a relocation completes,
    # the record that it happened is the only thing that answers an attribution
    # question later.
    record_residency(agent="a", host="h1", now=100.0, db_path=db)
    record_residency(agent="a", host="h2", now=200.0, db_path=db)
    # Act
    hosts = [r.host for r in read_residency_history("a", db_path=db)]
    # Assert
    assert hosts == ["h1", "h2"]


def test_re_recording_the_same_host_is_a_no_op(db) -> None:
    # Arrange: a coordinator that wrote the row and died before journalling
    # re-runs. It must not litter the history with the evidence of its retries.
    record_residency(agent="a", host="h1", now=100.0, db_path=db)
    # Act
    opened = record_residency(agent="a", host="h1", now=200.0, db_path=db)
    # Assert
    assert opened is False


def test_a_no_op_re_record_adds_no_row(db) -> None:
    # Arrange: the same rule, checked on the table rather than the return value.
    record_residency(agent="a", host="h1", now=100.0, db_path=db)
    record_residency(agent="a", host="h1", now=200.0, db_path=db)
    # Act
    history = read_residency_history("a", db_path=db)
    # Assert
    assert len(history) == 1


def test_an_empty_host_is_refused(db) -> None:
    # Arrange: an empty destination would open a residency that answers no
    # question, and would then be READ as an answer.
    call = lambda: record_residency(agent="a", host="  ", now=1.0, db_path=db)
    # Act
    outcome = call
    # Assert
    with pytest.raises(ValueError):
        outcome()


def test_a_journal_round_trips_through_the_store(db) -> None:
    # Arrange: this is what makes a crashed relocation RESUME rather than restart.
    from scitex_agent_container._lifecycle._relocate_phases import advance, begin

    reloc = begin(agent="a", from_host="h1", to_host="h2", now=1.0)
    moved, _ = advance(reloc, to_phase="source_drain", now=2.0, detail="d")
    save_journal(moved, db_path=db)
    # Act
    loaded = load_journal("a", db_path=db)
    # Assert
    assert loaded.phase == "source_drain"


def test_a_journal_keeps_its_steps(db) -> None:
    # Arrange: the record is self-describing — reading it tells you where the
    # relocation is, how it got there, and when, without a log that may have
    # rotated away.
    from scitex_agent_container._lifecycle._relocate_phases import advance, begin

    reloc = begin(agent="a", from_host="h1", to_host="h2", now=1.0)
    moved, _ = advance(
        reloc, to_phase="source_drain", now=2.0, detail="nothing to drain"
    )
    save_journal(moved, db_path=db)
    # Act
    loaded = load_journal("a", db_path=db)
    # Assert
    assert loaded.steps[-1].detail == "nothing to drain"


def test_no_journal_for_an_unknown_agent(db) -> None:
    # Arrange: the caller must open a fresh relocation, which it can only do if
    # "never started" is distinguishable from "stored".
    # Act
    loaded = load_journal("nobody", db_path=db)
    # Assert
    assert loaded is None
