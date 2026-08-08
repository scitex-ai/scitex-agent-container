"""An agent lives in exactly one place at a time, and a row can name its writer.

Two things this record buys, from the operator's 2026-08-07 idea:

  * an audit of where an agent has lived, and
  * the ATTRIBUTION signal that is missing today — the cards `host` column is
    NULL on 3247 of 3424 rows, so when two instances of one identity disagree,
    nothing can say which host wrote which row.

The second is what makes it worth building. The 08-07 split-brain was
diagnosable only because someone happened to be watching; with residency it is a
lookup.

The invariant under test is that "two homes at once" is UNREACHABLE, not merely
discouraged: opening a stay closes the previous one in the same operation. And
the boundary is half-open, so the handover instant — the one moment two answers
would otherwise be possible — belongs to exactly one host.

Pure, explicit timestamps, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._residency import (
    Residency,
    current_host,
    host_at,
    open_residency,
)

SRC = "ywata-note-win"
DST = "nas-03"
T0 = 1_000_000.0
T1 = T0 + 3600.0
T2 = T1 + 3600.0


@pytest.fixture
def moved() -> tuple[Residency, ...]:
    """Lived on SRC from T0, relocated to DST at T1."""
    history = open_residency((), host=SRC, now=T0)
    return open_residency(history, host=DST, now=T1)


# ---------------------------------------------------------------------------
# one home at a time
# ---------------------------------------------------------------------------


def test_a_first_move_opens_a_stay() -> None:
    # Arrange
    history = open_residency((), host=SRC, now=T0)
    # Act
    host = current_host(history)
    # Assert
    assert host == SRC


def test_moving_closes_the_previous_stay(moved: tuple[Residency, ...]) -> None:
    # Arrange: "two homes at once" must be unreachable, not discouraged.
    history = moved
    # Act
    still_open = [r for r in history if r.is_open]
    # Assert
    assert len(still_open) == 1


def test_the_closed_stay_ends_when_the_new_one_begins(
    moved: tuple[Residency, ...],
) -> None:
    # Arrange
    history = moved
    # Act
    previous = history[0]
    # Assert
    assert previous.to_ts == T1


def test_after_moving_the_agent_lives_on_the_target(
    moved: tuple[Residency, ...],
) -> None:
    # Arrange
    history = moved
    # Act
    host = current_host(history)
    # Assert
    assert host == DST


def test_re_recording_the_same_move_changes_nothing() -> None:
    # Arrange: a coordinator re-running after a crash must not litter the record
    # with the evidence of its own retries.
    history = open_residency((), host=SRC, now=T0)
    # Act
    again = open_residency(history, host=SRC, now=T1)
    # Assert
    assert again == history


def test_an_agent_with_no_history_lives_nowhere() -> None:
    # Arrange
    history: tuple[Residency, ...] = ()
    # Act
    host = current_host(history)
    # Assert
    assert host is None


def test_a_closed_history_means_stopped_not_misplaced() -> None:
    # Arrange: every stay ended and none reopened.
    history = (Residency(host=SRC, from_ts=T0, to_ts=T1),)
    # Act
    host = current_host(history)
    # Assert
    assert host is None


# ---------------------------------------------------------------------------
# attribution — which host wrote a row stamped `when`
# ---------------------------------------------------------------------------


def test_a_row_from_before_the_move_is_attributed_to_the_source(
    moved: tuple[Residency, ...],
) -> None:
    # Arrange
    history = moved
    # Act
    who = host_at(history, T0 + 1.0)
    # Assert
    assert who == SRC


def test_a_row_from_after_the_move_is_attributed_to_the_target(
    moved: tuple[Residency, ...],
) -> None:
    # Arrange
    history = moved
    # Act
    who = host_at(history, T1 + 1.0)
    # Assert
    assert who == DST


def test_the_handover_instant_belongs_to_the_target(
    moved: tuple[Residency, ...],
) -> None:
    # Arrange: half-open [from, to) — the one moment two answers would otherwise
    # be possible resolves to exactly one host.
    history = moved
    # Act
    who = host_at(history, T1)
    # Assert
    assert who == DST


def test_a_row_from_before_any_recorded_stay_is_unattributable(
    moved: tuple[Residency, ...],
) -> None:
    # Arrange: None means the history does not know — reading it as "the current
    # host" is the guess that makes a split-brain look explained when it is not.
    history = moved
    # Act
    who = host_at(history, T0 - 1.0)
    # Assert
    assert who is None


def test_a_row_from_a_gap_when_the_agent_was_stopped_is_unattributable() -> None:
    # Arrange: stopped at T1, restarted elsewhere at T2.
    history = (
        Residency(host=SRC, from_ts=T0, to_ts=T1),
        Residency(host=DST, from_ts=T2),
    )
    # Act
    who = host_at(history, T1 + 1.0)
    # Assert
    assert who is None


def test_an_open_stay_attributes_everything_after_it() -> None:
    # Arrange
    history = (Residency(host=DST, from_ts=T0),)
    # Act
    who = host_at(history, T0 + 999_999.0)
    # Assert
    assert who == DST


# ---------------------------------------------------------------------------
# time cannot be lied about
# ---------------------------------------------------------------------------


def test_a_stay_that_ends_before_it_starts_is_refused() -> None:
    # Arrange: the timestamps are the whole value of the record.
    fields = dict(host=SRC, from_ts=T1, to_ts=T0)

    # Act
    def build() -> Residency:
        return Residency(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_stay_with_no_host_is_refused() -> None:
    # Arrange
    fields = dict(host="", from_ts=T0)

    # Act
    def build() -> Residency:
        return Residency(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_backdating_a_move_before_the_current_stay_is_refused() -> None:
    # Arrange: it would close an interval that ends before it began.
    history = open_residency((), host=SRC, now=T1)

    # Act
    def move_backwards() -> tuple[Residency, ...]:
        return open_residency(history, host=DST, now=T0)

    # Assert
    with pytest.raises(ValueError):
        move_backwards()


def test_opening_a_stay_before_the_previous_one_ended_is_refused() -> None:
    # Arrange: overlapping stays would make attribution ambiguous, which is the
    # one thing this record exists to prevent.
    history = (Residency(host=SRC, from_ts=T0, to_ts=T2),)

    # Act
    def overlap() -> tuple[Residency, ...]:
        return open_residency(history, host=DST, now=T1)

    # Assert
    with pytest.raises(ValueError):
        overlap()


def test_moving_with_no_host_is_refused() -> None:
    # Arrange
    history = open_residency((), host=SRC, now=T0)

    # Act
    def move_nowhere() -> tuple[Residency, ...]:
        return open_residency(history, host="", now=T1)

    # Assert
    with pytest.raises(ValueError):
        move_nowhere()


def test_a_reopened_history_records_both_stays() -> None:
    # Arrange: stopped, then started again on another host.
    history = (Residency(host=SRC, from_ts=T0, to_ts=T1),)
    # Act
    reopened = open_residency(history, host=DST, now=T2)
    # Assert
    assert len(reopened) == 2
