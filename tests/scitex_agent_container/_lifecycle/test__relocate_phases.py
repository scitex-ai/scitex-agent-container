"""A relocation that dies mid-way must RESUME, and must never undo a handover.

The operator's question when this was designed (2026-08-07) was "what if it dies
mid-way". A relocation spans two hosts and a shared store, so it will be
interrupted eventually. These tests pin the two answers:

  * every transition is journalled and re-entrant, so a coordinator that crashed
    after doing the work but before recording it can simply re-run, and
  * the sequence is asymmetric around HANDOVER — reversible before it,
    forward-only after — because past that point the target already owns the
    lease and the source is fenced out.

An abort after the handover is REFUSED rather than accommodated: taking write
authority back from a live holder is itself a relocation and should be run as
one, not smuggled in under a name that promises the opposite.

Pure functions, explicit `now`, no mocks and no sleeping.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_phases import (
    ABORTED,
    CODE_ALREADY_THERE,
    CODE_BACKWARD,
    CODE_OK,
    CODE_PAST_NO_RETURN,
    CODE_SKIPS_PHASE,
    CODE_TERMINAL,
    CODE_UNKNOWN_PHASE,
    DONE,
    HANDOVER,
    PHASES,
    PREFLIGHT,
    SOURCE_DRAIN,
    SOURCE_STOP,
    TARGET_STANDBY,
    PhaseVerdict,
    Relocation,
    abort,
    advance,
    begin,
    is_past_no_return,
    resume_from,
)

AGENT = "scitex-agent-container"
SRC = "ywata-note-win"
DST = "nas-03"
T0 = 1_000_000.0


def _walk_to(phase: str) -> Relocation:
    """Drive a fresh relocation forward to ``phase`` one legal step at a time."""
    rel = begin(agent=AGENT, from_host=SRC, to_host=DST, now=T0)
    for i, nxt in enumerate(PHASES[1 : PHASES.index(phase) + 1], start=1):
        rel, verdict = advance(rel, to_phase=nxt, now=T0 + i, detail=f"step {i}")
        assert verdict.allowed is True  # harness guard, not the behaviour under test
    return rel


@pytest.fixture
def fresh() -> Relocation:
    """A relocation just opened, sitting at PREFLIGHT."""
    return begin(agent=AGENT, from_host=SRC, to_host=DST, now=T0)


@pytest.fixture
def handed_over() -> Relocation:
    """A relocation past the point of no return — the lease has moved."""
    return _walk_to(HANDOVER)


# ---------------------------------------------------------------------------
# begin — opens without touching anything
# ---------------------------------------------------------------------------


def test_a_new_relocation_opens_at_preflight(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    phase = rel.phase
    # Assert
    assert phase == PREFLIGHT


def test_a_new_relocation_journals_its_opening(fresh: Relocation) -> None:
    # Arrange: the record must be self-describing without an external log.
    rel = fresh
    # Act
    steps = rel.steps
    # Assert
    assert len(steps) == 1


def test_started_at_is_the_first_step(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    started = rel.started_at
    # Assert
    assert started == T0


def test_relocating_a_host_to_itself_is_refused() -> None:
    # Arrange: nothing would move, and the handover would take the lease from
    # and give it to the same host.
    fields = dict(agent=AGENT, from_host=SRC, to_host=SRC, now=T0)

    # Act
    def build() -> Relocation:
        return begin(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


# ---------------------------------------------------------------------------
# advance — one step forward, re-entry free, never backward
# ---------------------------------------------------------------------------


def test_the_next_phase_is_accepted(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    _, verdict = advance(rel, to_phase=TARGET_STANDBY, now=T0 + 1)
    # Assert
    assert verdict.allowed is True


def test_advancing_appends_to_the_journal(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    moved, _ = advance(rel, to_phase=TARGET_STANDBY, now=T0 + 1, detail="target up")
    # Assert
    assert len(moved.steps) == len(rel.steps) + 1


def test_re_advancing_to_the_current_phase_succeeds(fresh: Relocation) -> None:
    # Arrange: a coordinator that did the work then died before journalling must
    # be able to re-run without tripping over itself.
    rel = fresh
    # Act
    _, verdict = advance(rel, to_phase=PREFLIGHT, now=T0 + 1)
    # Assert
    assert verdict.code == CODE_ALREADY_THERE


def test_re_advancing_appends_nothing(fresh: Relocation) -> None:
    # Arrange: idempotent means no duplicate entry, not merely no error.
    rel = fresh
    # Act
    same, _ = advance(rel, to_phase=PREFLIGHT, now=T0 + 1)
    # Assert
    assert same.steps == rel.steps


def test_skipping_a_phase_is_refused(fresh: Relocation) -> None:
    # Arrange: jumping PREFLIGHT -> HANDOVER would move the lease to a target
    # nobody ever started.
    rel = fresh
    # Act
    _, verdict = advance(rel, to_phase=HANDOVER, now=T0 + 1)
    # Assert
    assert verdict.code == CODE_SKIPS_PHASE


def test_stepping_backward_is_refused(handed_over: Relocation) -> None:
    # Arrange
    rel = handed_over
    # Act
    _, verdict = advance(rel, to_phase=SOURCE_DRAIN, now=T0 + 9)
    # Assert
    assert verdict.code == CODE_BACKWARD


def test_an_unknown_phase_is_refused(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    _, verdict = advance(rel, to_phase="teleport", now=T0 + 1)
    # Assert
    assert verdict.code == CODE_UNKNOWN_PHASE


def test_a_completed_relocation_cannot_advance() -> None:
    # Arrange
    rel = _walk_to(DONE)
    # Act
    _, verdict = advance(rel, to_phase=DONE, now=T0 + 99)
    # Assert
    assert verdict.code == CODE_TERMINAL


def test_the_journal_keeps_the_whole_trail() -> None:
    # Arrange: reading the record alone must show how it got here, without a log
    # that may have rotated away.
    rel = _walk_to(DONE)
    # Act
    phases = [s.phase for s in rel.steps]
    # Assert
    assert phases == list(PHASES)


# ---------------------------------------------------------------------------
# resume_from — where a crashed coordinator picks up
# ---------------------------------------------------------------------------


def test_resume_points_at_the_next_phase(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    nxt = resume_from(rel)
    # Assert
    assert nxt == TARGET_STANDBY


def test_resume_after_a_partial_run_points_forward() -> None:
    # Arrange
    rel = _walk_to(SOURCE_DRAIN)
    # Act
    nxt = resume_from(rel)
    # Assert
    assert nxt == HANDOVER


def test_a_completed_relocation_has_nothing_to_resume() -> None:
    # Arrange: None must be handled explicitly by the caller, not looped over.
    rel = _walk_to(DONE)
    # Act
    nxt = resume_from(rel)
    # Assert
    assert nxt is None


# ---------------------------------------------------------------------------
# the point of no return
# ---------------------------------------------------------------------------


def test_before_the_handover_the_relocation_is_reversible() -> None:
    # Arrange
    rel = _walk_to(SOURCE_DRAIN)
    # Act
    past = is_past_no_return(rel)
    # Assert
    assert past is False


def test_at_the_handover_the_relocation_is_past_no_return(
    handed_over: Relocation,
) -> None:
    # Arrange: the lease has moved; the source is fenced out even while alive.
    rel = handed_over
    # Act
    past = is_past_no_return(rel)
    # Assert
    assert past is True


def test_after_the_handover_the_relocation_stays_past_no_return() -> None:
    # Arrange
    rel = _walk_to(SOURCE_STOP)
    # Act
    past = is_past_no_return(rel)
    # Assert
    assert past is True


# ---------------------------------------------------------------------------
# abort — legal only while nothing has moved
# ---------------------------------------------------------------------------


def test_aborting_before_the_handover_is_allowed(fresh: Relocation) -> None:
    # Arrange: the target is a harmless read-only standby, so abandoning costs
    # nothing.
    rel = fresh
    # Act
    _, verdict = abort(rel, now=T0 + 2, reason="target image missing")
    # Assert
    assert verdict.allowed is True


def test_an_aborted_relocation_says_so(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    stopped, _ = abort(rel, now=T0 + 2, reason="target image missing")
    # Assert
    assert stopped.phase == ABORTED


def test_an_abort_records_its_reason(fresh: Relocation) -> None:
    # Arrange
    rel = fresh
    # Act
    stopped, _ = abort(rel, now=T0 + 2, reason="target image missing")
    # Assert
    assert stopped.steps[-1].detail == "target image missing"


def test_an_abort_must_state_why(fresh: Relocation) -> None:
    # Arrange: an unexplained abandonment is indistinguishable from a crash.
    rel = fresh
    # Act
    _, verdict = abort(rel, now=T0 + 2, reason="")
    # Assert
    assert verdict.allowed is False


def test_aborting_at_the_handover_is_refused(handed_over: Relocation) -> None:
    # Arrange: undoing here would mean taking the lease back from a live holder.
    rel = handed_over
    # Act
    _, verdict = abort(rel, now=T0 + 9, reason="changed my mind")
    # Assert
    assert verdict.code == CODE_PAST_NO_RETURN


def test_aborting_after_the_handover_is_refused() -> None:
    # Arrange
    rel = _walk_to(SOURCE_STOP)
    # Act
    _, verdict = abort(rel, now=T0 + 9, reason="changed my mind")
    # Assert
    assert verdict.code == CODE_PAST_NO_RETURN


def test_aborting_a_completed_relocation_is_refused() -> None:
    # Arrange
    rel = _walk_to(DONE)
    # Act
    _, verdict = abort(rel, now=T0 + 99, reason="too late")
    # Assert
    assert verdict.code == CODE_TERMINAL


def test_aborting_twice_is_a_no_op(fresh: Relocation) -> None:
    # Arrange
    stopped, _ = abort(fresh, now=T0 + 2, reason="target image missing")
    # Act
    _, verdict = abort(stopped, now=T0 + 3, reason="target image missing")
    # Assert
    assert verdict.code == CODE_ALREADY_THERE


def test_an_aborted_relocation_is_not_past_no_return(fresh: Relocation) -> None:
    # Arrange: ABORTED sits outside the ordered phases, so the side-of-the-line
    # question must not accidentally compare it by index.
    stopped, _ = abort(fresh, now=T0 + 2, reason="target image missing")
    # Act
    past = is_past_no_return(stopped)
    # Assert
    assert past is False


# ---------------------------------------------------------------------------
# the shapes validate themselves, where they are built
# ---------------------------------------------------------------------------


def test_a_relocation_refuses_an_empty_agent() -> None:
    # Arrange
    fields = dict(agent="", from_host=SRC, to_host=DST, now=T0)

    # Act
    def build() -> Relocation:
        return begin(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_relocation_refuses_an_unknown_phase() -> None:
    # Arrange
    fields = dict(agent=AGENT, from_host=SRC, to_host=DST, phase="sideways", steps=())

    # Act
    def build() -> Relocation:
        return Relocation(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_verdict_refuses_a_permission_that_is_not_coded_ok() -> None:
    # Arrange: allowed=True with a refusal code is the shape that lets a caller
    # reading one field draw the opposite conclusion.
    fields = dict(allowed=True, code=CODE_BACKWARD, reason="contradictory")

    # Act
    def build() -> PhaseVerdict:
        return PhaseVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_verdict_refuses_an_empty_reason() -> None:
    # Arrange
    fields = dict(allowed=False, code=CODE_BACKWARD, reason="")

    # Act
    def build() -> PhaseVerdict:
        return PhaseVerdict(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_refusal_is_truthy_so_callers_must_read_the_field(fresh: Relocation) -> None:
    # Arrange: deliberately no __bool__, so `if verdict:` cannot read as
    # permission. This documents the trap rather than hiding it.
    _, verdict = advance(fresh, to_phase=HANDOVER, now=T0 + 1)
    # Act
    truthy = bool(verdict)
    # Assert
    assert truthy is True


def test_the_success_code_differs_from_the_idempotent_code() -> None:
    # Arrange: a caller distinguishing "work happened" from "already done"
    # needs the two to be different numbers, not the same one twice.
    codes = (CODE_OK, CODE_ALREADY_THERE)
    # Act
    distinct = len(set(codes))
    # Assert
    assert distinct == 2
