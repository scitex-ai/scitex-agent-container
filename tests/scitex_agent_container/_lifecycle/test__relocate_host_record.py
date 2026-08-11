"""The db answers "where does this agent run". The spec's `host:` is dead text.

Operator, 2026-08-11:「設定ファイル、人が書くものはファイル、状態は db」. Where an
agent runs is an OBSERVATION, so it lives in the state db, and a relocation
writes it there and nowhere else.

The migration is the part with teeth, and these tests pin it: SEED ONCE, THEN
IGNORE. Once the db has an answer the spec's copy is never consulted again — not
compared, not warned about on every read — because a field that is authoritative
on Tuesday and ignored on Wednesday is worse than either.

The other property under test is that not-knowing is an answer. `sac agents
list` prints `host` as the literal string 'local' on every row today, which is a
placeholder standing where an observation belongs. This module returns None
instead, and None must not be rendered as a hostname.

Pure functions over a residency history. No db, no files, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_host_record import (
    CODE_FROM_DB,
    CODE_SEEDED_FROM_SPEC,
    CODE_UNKNOWN,
    HostAnswer,
    legacy_spec_host_notice,
    record_move,
    resolve_host,
)
from scitex_agent_container._lifecycle._residency import Residency, current_host

SRC = "ywata-note-win"
DST = "scitex-compute-04"
T0 = 1_000_000.0


def _living_on(host: str) -> tuple[Residency, ...]:
    return (Residency(host=host, from_ts=T0),)


# ---------------------------------------------------------------------------
# the db wins
# ---------------------------------------------------------------------------


def test_the_db_answers_when_it_knows() -> None:
    # Arrange
    history = _living_on(SRC)
    # Act
    answer = resolve_host(history)
    # Assert
    assert answer.host == SRC


def test_an_answer_from_the_db_says_so() -> None:
    # Arrange
    history = _living_on(SRC)
    # Act
    answer = resolve_host(history)
    # Assert
    assert answer.code == CODE_FROM_DB


def test_a_legacy_spec_host_never_overrides_the_db() -> None:
    # Arrange: the spec still says one thing and the db says another. The db is
    # authoritative, full stop — no comparison, no merge, no warning-per-read.
    history = _living_on(DST)
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC, now=T0 + 10)
    # Assert
    assert answer.host == DST


def test_reading_a_known_host_does_not_seed_from_the_spec() -> None:
    # Arrange
    history = _living_on(DST)
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC, now=T0 + 10)
    # Assert
    assert answer.seeded_from_spec is False


def test_reading_a_known_host_leaves_the_history_untouched() -> None:
    # Arrange
    history = _living_on(DST)
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC, now=T0 + 10)
    # Assert
    assert answer.history == history


# ---------------------------------------------------------------------------
# seed once
# ---------------------------------------------------------------------------


def test_an_empty_db_is_seeded_from_the_legacy_spec_host() -> None:
    # Arrange: so no agent has to be re-registered by hand.
    history: tuple[Residency, ...] = ()
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC, now=T0)
    # Assert
    assert answer.host == SRC


def test_a_seeded_answer_records_that_it_came_from_the_spec() -> None:
    # Arrange: the provenance must survive into the db row, or the value arrives
    # there looking like something that was measured.
    history: tuple[Residency, ...] = ()
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC, now=T0)
    # Assert
    assert answer.seeded_from_spec is True


def test_a_seeded_answer_carries_its_own_code() -> None:
    # Arrange
    history: tuple[Residency, ...] = ()
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC, now=T0)
    # Assert
    assert answer.code == CODE_SEEDED_FROM_SPEC


def test_seeding_returns_a_history_the_caller_can_persist() -> None:
    # Arrange
    history: tuple[Residency, ...] = ()
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC, now=T0)
    # Assert
    assert current_host(answer.history) == SRC


def test_the_second_read_after_seeding_comes_from_the_db() -> None:
    # Arrange: this is the "then ignore" half. Seeding happens once.
    seeded = resolve_host((), legacy_spec_host=SRC, now=T0)
    # Act
    again = resolve_host(seeded.history, legacy_spec_host=SRC, now=T0 + 1)
    # Assert
    assert again.code == CODE_FROM_DB


# ---------------------------------------------------------------------------
# not knowing is an answer
# ---------------------------------------------------------------------------


def test_an_empty_db_with_no_legacy_host_is_unknown() -> None:
    # Arrange
    history: tuple[Residency, ...] = ()
    # Act
    answer = resolve_host(history)
    # Assert
    assert answer.host is None


def test_an_unknown_host_refuses_to_be_guessed_from_the_local_hostname() -> None:
    # Arrange: the guess is what makes a split-brain look explained.
    history: tuple[Residency, ...] = ()
    # Act
    answer = resolve_host(history)
    # Assert
    assert "do NOT substitute the local hostname" in answer.reason


def test_seeding_without_a_timestamp_is_unknown_rather_than_backdated() -> None:
    # Arrange: a stay with no start time cannot answer an attribution question.
    history: tuple[Residency, ...] = ()
    # Act
    answer = resolve_host(history, legacy_spec_host=SRC)
    # Assert
    assert answer.code == CODE_UNKNOWN


def test_a_closed_history_reports_unknown_rather_than_the_last_host() -> None:
    # Arrange: an agent whose stay was closed and never reopened runs nowhere.
    history = (Residency(host=SRC, from_ts=T0, to_ts=T0 + 5),)
    # Act
    answer = resolve_host(history)
    # Assert
    assert answer.host is None


def test_a_blank_host_cannot_be_expressed_as_an_answer() -> None:
    # Arrange: an empty string renders as an answer while meaning nothing.
    build = lambda: HostAnswer(host="  ", code=CODE_FROM_DB, reason="x")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="real hostname or None")
    # Assert
    with caught:
        build()


# ---------------------------------------------------------------------------
# record_move — the whole of the operator's item #1
# ---------------------------------------------------------------------------


def test_recording_a_move_makes_the_target_the_current_host() -> None:
    # Arrange
    history = _living_on(SRC)
    # Act
    moved = record_move(history, to_host=DST, now=T0 + 10)
    # Assert
    assert current_host(moved) == DST


def test_recording_a_move_closes_the_previous_stay_in_the_same_step() -> None:
    # Arrange: "living in two places at once" stays unrepresentable.
    history = _living_on(SRC)
    # Act
    moved = record_move(history, to_host=DST, now=T0 + 10)
    # Assert
    assert moved[0].to_ts == T0 + 10


def test_re_recording_the_same_move_does_not_litter_the_history() -> None:
    # Arrange: a coordinator re-running after a crash must not leave the
    # evidence of its own retries in the record.
    once = record_move(_living_on(SRC), to_host=DST, now=T0 + 10)
    # Act
    twice = record_move(once, to_host=DST, now=T0 + 20)
    # Assert
    assert twice == once


def test_a_move_with_no_destination_is_refused() -> None:
    # Arrange
    call = lambda: record_move(_living_on(SRC), to_host="", now=T0)  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="moved TO")
    # Assert
    with caught:
        call()


# ---------------------------------------------------------------------------
# telling the operator the spec field is dead
# ---------------------------------------------------------------------------


def test_a_stale_spec_host_that_disagrees_is_called_out_as_ignored() -> None:
    # Arrange: the failure being avoided is an operator reading `host: nas-03`
    # in a file, believing it, and being wrong.
    # Act
    notice = legacy_spec_host_notice(spec_host=SRC, db_host=DST)
    # Assert
    assert "IGNORED" in notice


def test_the_notice_names_the_authoritative_value() -> None:
    # Arrange
    # Act
    notice = legacy_spec_host_notice(spec_host=SRC, db_host=DST)
    # Assert
    assert DST in notice


def test_a_spec_host_with_an_empty_db_is_described_as_a_one_time_seed() -> None:
    # Arrange
    # Act
    notice = legacy_spec_host_notice(spec_host=SRC, db_host=None)
    # Assert
    assert "SEED the db once" in notice


def test_a_spec_with_no_host_produces_no_notice() -> None:
    # Arrange: nothing to say, so nothing is said.
    # Act
    notice = legacy_spec_host_notice(spec_host=None, db_host=DST)
    # Assert
    assert notice == ""
