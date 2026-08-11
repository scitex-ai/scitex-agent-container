"""`sac agents list` reports "defined" as a status. That is the bug these tests pin.

"defined" is a SPEC fact — a file exists — rendered in a STATE column, which is
why the listing reports zero running agents while agents are demonstrably
running. The same listing puts the literal string 'local' in a `host` column
that is supposed to carry an observation.

The operator's item #2 asks for the two to be clearly separated. A comment
saying so is obeyed until the first hurry, so the separation is a REFUSAL: a
write that names a field from the wrong vocabulary raises with the field named,
and a field in neither vocabulary raises too — because a pass-through default
means the separation only holds for the fields someone remembered to list.

Pure dict in, dict out.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_records import (
    SPEC_FIELDS,
    STATE_FIELDS,
    classify_field,
    spec_patch,
    state_record,
)

AGENT = "scitex-agent-container"
SRC = "ywata-note-win"
DST = "scitex-compute-04"


# ---------------------------------------------------------------------------
# the two vocabularies do not overlap
# ---------------------------------------------------------------------------


def test_no_field_is_declared_in_both_vocabularies() -> None:
    # Arrange: a field meaning intent in one place and an observation in another
    # is exactly how a status column ends up saying "defined".
    both = SPEC_FIELDS & STATE_FIELDS
    # Act
    overlap = sorted(both)
    # Assert
    assert overlap == []


def test_a_spec_field_classifies_as_spec() -> None:
    # Arrange
    name = "runtime"
    # Act
    kind = classify_field(name)
    # Assert
    assert kind == "spec"


def test_host_classifies_as_state_because_it_is_observed_not_declared() -> None:
    # Arrange: the 2026-08-11 ruling —「設定ファイル、人が書くものはファイル、
    # 状態は db」. A human typing `host: nas-03` is recording a fact, not
    # declaring a preference, and a fact in a git-tracked file that exists in
    # two copies on two machines will eventually be wrong in one of them.
    name = "host"
    # Act
    kind = classify_field(name)
    # Assert
    assert kind == "state"


def test_a_state_field_classifies_as_state() -> None:
    # Arrange
    name = "phase"
    # Act
    kind = classify_field(name)
    # Assert
    assert kind == "state"


def test_an_undeclared_field_is_refused_rather_than_passed_through() -> None:
    # Arrange
    call = lambda: classify_field("some_new_thing")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="neither")
    # Assert
    with caught:
        call()


# ---------------------------------------------------------------------------
# spec_patch — declarations only
# ---------------------------------------------------------------------------


def test_a_declaration_is_a_legal_spec_patch() -> None:
    # Arrange
    values = {"runtime": "apptainer"}
    # Act
    patch = spec_patch(**values)
    # Assert
    assert patch == {"runtime": "apptainer"}


def test_writing_the_host_into_a_spec_now_raises() -> None:
    # Arrange: this is the enforcement, not a note. Any code path that tried to
    # write a host into a spec fails at the call site with the field named.
    call = lambda: spec_patch(host=DST)  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="host")
    # Assert
    with caught:
        call()


def test_writing_an_observation_into_the_spec_is_refused() -> None:
    # Arrange: `phase` is something that was observed, not something declared.
    call = lambda: spec_patch(runtime="apptainer", phase="handover")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="observed data")
    # Assert
    with caught:
        call()


def test_the_spec_refusal_names_the_offending_field() -> None:
    # Arrange
    call = lambda: spec_patch(runtime="apptainer", lease_fence=4)  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="lease_fence")
    # Assert
    with caught:
        call()


def test_the_host_is_a_legal_state_record_field() -> None:
    # Arrange: the other half — it is refused in the spec and accepted here.
    values = dict(agent=AGENT, from_host=SRC, to_host=DST, host=DST)
    # Act
    record = state_record(**values)
    # Assert
    assert record["host"] == DST


def test_an_empty_spec_patch_is_refused_because_it_writes_nothing() -> None:
    # Arrange
    call = lambda: spec_patch()  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="writes nothing")
    # Assert
    with caught:
        call()


# ---------------------------------------------------------------------------
# state_record — observations only, and it must be joinable
# ---------------------------------------------------------------------------


def test_a_relocation_outcome_is_a_legal_state_record() -> None:
    # Arrange
    values = dict(agent=AGENT, from_host=SRC, to_host=DST, phase="done")
    # Act
    record = state_record(**values)
    # Assert
    assert record["phase"] == "done"


def test_writing_a_declaration_into_the_state_db_is_refused() -> None:
    # Arrange: `runtime` is what a human declared; it is not an observation.
    call = lambda: state_record(  # noqa: E731
        agent=AGENT, from_host=SRC, to_host=DST, runtime="apptainer"
    )
    # Act
    caught = pytest.raises(ValueError, match="declared data")
    # Assert
    with caught:
        call()


def test_a_state_row_that_names_no_agent_is_refused() -> None:
    # Arrange: a row that cannot be joined is the cards `host` column all over
    # again — NULL on 3247 of 3424 rows, so attribution is unanswerable.
    call = lambda: state_record(agent="", from_host=SRC, to_host=DST)  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="agent")
    # Assert
    with caught:
        call()


def test_a_state_row_that_names_no_destination_is_refused() -> None:
    # Arrange
    call = lambda: state_record(agent=AGENT, from_host=SRC, to_host="")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="to_host")
    # Assert
    with caught:
        call()


def test_the_migration_retained_flag_is_a_state_field() -> None:
    # Arrange: item #9 — the fact of a migration is never discarded, so it lives
    # in the state db rather than being cleaned up on success.
    values = dict(agent=AGENT, from_host=SRC, to_host=DST, migration_retained=True)
    # Act
    record = state_record(**values)
    # Assert
    assert record["migration_retained"] is True
