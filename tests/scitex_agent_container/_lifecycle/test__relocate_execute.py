"""The driver's whole job is the ORDER, and the asymmetry around the atomic point.

Before HANDOVER a failure aborts, and nothing durable has been written to undo:
the host goes to the state db at DONE and to no spec file at any point. At or
after the handover a failure STOPS AND REPORTS — there is no rollback, because
the target already owns the lease and the source is fenced out, so an "undo"
would mean taking write authority back from a live holder.

The property that is easiest to lose and most expensive to lose is the third
one: an UNKNOWN stops as firmly as a failure. A step whose unknown was folded
into success would hand the lease to a target nobody established was working —
the 2026-08-07 failure, with the source already stopped.

Effects are real callables returning real values. Nothing is mocked and nothing
touches a host: that is exactly why every branch below is reachable in a test.
"""

from __future__ import annotations

import itertools

import pytest

from scitex_agent_container._lifecycle._relocate_execute import (
    CODE_ABORTED,
    CODE_COMPLETED,
    CODE_STOPPED_PAST_NO_RETURN,
    CODE_UNKNOWN,
    ExecuteOutcome,
    PhaseEffects,
    StepResult,
    execute,
)
from scitex_agent_container._lifecycle._relocate_phases import (
    ABORTED,
    DONE,
    HANDOVER,
    HANDSHAKE,
    PHASES,
    SOURCE_DRAIN,
    SOURCE_STOP,
    TARGET_STANDBY,
    TRANSPORT,
    Relocation,
    advance,
    begin,
)

AGENT = "scitex-agent-container"
SRC = "ywata-note-win"
DST = "scitex-compute-04"
T0 = 1_000_000.0


def _clock():
    """A monotonic clock as a callable — real time, just not wall time."""
    ticks = itertools.count(T0)
    return lambda: float(next(ticks))


def _ok(what: str) -> StepResult:
    return StepResult(ok=True, detail=what)


def _fail(what: str) -> StepResult:
    return StepResult(ok=False, detail=what, hint="fix it and re-run")


def _unknown(what: str) -> StepResult:
    return StepResult(ok=None, detail=what, hint="go and measure it")


def _effects(**overrides) -> PhaseEffects:
    """Every phase succeeds unless a test replaces one."""
    base = dict(
        start_target_standby=lambda: _ok("target started read-only"),
        handshake=lambda: _ok("round trip observed"),
        drain_source=lambda: _ok("source drained"),
        hand_over_lease=lambda: _ok("lease moved at fence 4"),
        stop_source=lambda: _ok("source stopped and verified"),
        transport_transcript=lambda: _ok("transcript verified on the target"),
        finish=lambda: _ok("residency and origin recorded"),
    )
    base.update(overrides)
    return PhaseEffects(**base)


def _fresh() -> Relocation:
    return begin(agent=AGENT, from_host=SRC, to_host=DST, now=T0)


def _at(phase: str) -> Relocation:
    """A relocation whose journal already records everything up to ``phase``."""
    rel = _fresh()
    for i, nxt in enumerate(PHASES[1 : PHASES.index(phase) + 1], start=1):
        rel, verdict = advance(rel, to_phase=nxt, now=T0 + i, detail=f"step {i}")
        assert verdict.allowed is True  # harness guard, not the behaviour tested
    return rel


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_every_phase_succeeding_completes_the_relocation() -> None:
    # Arrange
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.completed is True


def test_a_completed_relocation_ends_at_done() -> None:
    # Arrange
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.relocation.phase == DONE


def test_a_completed_relocation_journals_every_phase_in_order() -> None:
    # Arrange
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert tuple(s.phase for s in outcome.relocation.steps) == PHASES


def test_the_first_thing_a_relocation_does_is_drain_the_source() -> None:
    # Arrange: the order is the design, so it is asserted rather than assumed.
    # Nothing precedes the drain — in particular no spec is edited. The source
    # side goes first because the transcript cannot be copied out from under a
    # running agent, and the target cannot start before the transcript lands.
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.log[0].startswith(SOURCE_DRAIN)


def test_the_source_is_stopped_before_the_transcript_is_transported() -> None:
    # Arrange: THE ordering constraint the transport phase exists under. A live
    # agent appends mid-read, and the resulting jsonl parses, resumes, and
    # silently ends the conversation early.
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.log.index(
        next(x for x in outcome.log if x.startswith(SOURCE_STOP))
    ) < outcome.log.index(next(x for x in outcome.log if x.startswith(TRANSPORT)))


def test_the_transcript_is_transported_before_the_target_starts() -> None:
    # Arrange: the other half. Once the target has booted it owns its own
    # session marker, and seeding over it would discard whatever it did.
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.log.index(
        next(x for x in outcome.log if x.startswith(TRANSPORT))
    ) < outcome.log.index(next(x for x in outcome.log if x.startswith(TARGET_STANDBY)))


def test_a_transport_failure_aborts_before_the_target_is_ever_started() -> None:
    # Arrange: a target started on a failed transport is the "healthy agent with
    # no memory" shape. It must never get that far.
    effects = _effects(
        transport_transcript=lambda: _fail("the target's copy is 40 lines short")
    )
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.standby_left_running is False


def test_a_transport_failure_reports_the_source_as_left_stopped() -> None:
    # Arrange: the price of the ordering, stated as a field rather than left for
    # the operator to deduce — at this point the agent is down on both hosts.
    effects = _effects(
        transport_transcript=lambda: _fail("the target's copy is 40 lines short")
    )
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.source_left_stopped is True


def test_a_transport_failure_tells_the_operator_how_to_undo_it() -> None:
    # Arrange: "nothing was changed" would be true of every durable record and
    # false of the thing the operator will actually notice.
    effects = _effects(
        transport_transcript=lambda: _fail("the target's copy is 40 lines short")
    )
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert f"is STOPPED on {SRC}" in outcome.hint


def test_a_failure_before_the_source_is_stopped_leaves_it_running() -> None:
    # Arrange: the counterpart — an abort at the drain must NOT tell the
    # operator to restart an agent that never stopped.
    effects = _effects(drain_source=lambda: _fail("in-flight work would be lost"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.source_left_stopped is False


def test_the_handshake_runs_before_the_handover() -> None:
    # Arrange
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.log.index(
        next(x for x in outcome.log if x.startswith(HANDSHAKE))
    ) < outcome.log.index(next(x for x in outcome.log if x.startswith(HANDOVER)))


def test_a_completed_relocation_carries_the_completed_code() -> None:
    # Arrange
    effects = _effects()
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.code == CODE_COMPLETED


# ---------------------------------------------------------------------------
# resuming — a crash continues, it does not restart
# ---------------------------------------------------------------------------


def test_a_resumed_relocation_does_not_re_run_the_phases_already_journalled() -> None:
    # Arrange: a coordinator that died after stopping the source re-runs from
    # the transport, NOT from the beginning — re-draining and re-stopping an
    # already-stopped agent is wasted work at best.
    effects = _effects()
    # Act
    outcome = execute(_at(SOURCE_STOP), effects=effects, now=_clock())
    # Assert
    assert outcome.log[0].startswith(TRANSPORT)


def test_a_resumed_relocation_still_reaches_done() -> None:
    # Arrange
    effects = _effects()
    # Act
    outcome = execute(_at(HANDOVER), effects=effects, now=_clock())
    # Assert
    assert outcome.relocation.phase == DONE


# ---------------------------------------------------------------------------
# before the atomic point — abort and undo
# ---------------------------------------------------------------------------


def test_a_handshake_failure_aborts_the_relocation() -> None:
    # Arrange: this is the gate the 2026-08-11 silence would have hit.
    effects = _effects(handshake=lambda: _fail("no reply reached the source"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.relocation.phase == ABORTED


def test_a_pre_handover_failure_carries_the_aborted_code() -> None:
    # Arrange
    effects = _effects(handshake=lambda: _fail("no reply reached the source"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.code == CODE_ABORTED


def test_a_pre_handover_abort_leaves_the_recorded_host_unchanged() -> None:
    # Arrange: the host is written to the db at DONE and nowhere else, so an
    # abort has nothing to reverse — and the message must say so rather than
    # leaving the reader to wonder what state a file is in.
    effects = _effects(handshake=lambda: _fail("no reply reached the source"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert "host in the state db is unchanged" in outcome.hint


def test_a_pre_handover_failure_says_the_lease_never_moved() -> None:
    # Arrange
    effects = _effects(start_target_standby=lambda: _fail("the target did not boot"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert f"lease is still with {SRC}" in outcome.hint


def test_a_failure_names_the_phase_it_stopped_at() -> None:
    # Arrange
    effects = _effects(start_target_standby=lambda: _fail("the target did not boot"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.stopped_at == TARGET_STANDBY


def test_a_failure_to_start_the_standby_leaves_nothing_running() -> None:
    # Arrange: an observed "it did not boot" is a real answer, so there is no
    # abandoned process to warn about.
    effects = _effects(start_target_standby=lambda: _fail("the target did not boot"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.standby_left_running is False


def test_an_unconfirmed_standby_start_is_reported_as_possibly_left_running() -> None:
    # Arrange: an effect that could not say whether the target came up may well
    # have started it, and "nothing was left running" would send nobody to look.
    effects = _effects(start_target_standby=lambda: _unknown("no health line seen"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.standby_left_running is True


def test_an_abort_after_the_standby_warns_that_it_is_still_running() -> None:
    # Arrange: stopping it here would be another remote action taken at the
    # moment least is known, so it is reported rather than cleaned up.
    effects = _effects(handshake=lambda: _fail("no reply reached the source"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert "STILL RUNNING" in outcome.hint


# ---------------------------------------------------------------------------
# unknown stops as firmly as failure, and says something different
# ---------------------------------------------------------------------------


def test_an_unknown_step_does_not_continue_to_the_next_phase() -> None:
    # Arrange: an unknown handshake folded into success hands the lease to a
    # target nobody established was working.
    effects = _effects(handshake=lambda: _unknown("no reply seen in 60s"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.stopped_at == HANDSHAKE


def test_an_unknown_step_reports_completed_as_unknown_not_false() -> None:
    # Arrange
    effects = _effects(handshake=lambda: _unknown("no reply seen in 60s"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.completed is None


def test_an_unknown_step_carries_its_own_code() -> None:
    # Arrange: go-and-measure is a different action from go-and-fix.
    effects = _effects(handshake=lambda: _unknown("no reply seen in 60s"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.code == CODE_UNKNOWN


def test_an_unknown_step_keeps_the_measure_it_hint() -> None:
    # Arrange
    effects = _effects(handshake=lambda: _unknown("no reply seen in 60s"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert "go and measure it" in outcome.hint


# ---------------------------------------------------------------------------
# at or after the atomic point — no rollback, ever
# ---------------------------------------------------------------------------


def test_a_failure_after_the_handover_is_not_aborted() -> None:
    # Arrange: aborting would mean taking the lease back from a live holder.
    effects = _effects(finish=lambda: _fail("the residency row could not be written"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.relocation.phase != ABORTED


def test_a_failure_after_the_handover_carries_the_past_no_return_code() -> None:
    # Arrange
    effects = _effects(finish=lambda: _fail("the residency row could not be written"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.code == CODE_STOPPED_PAST_NO_RETURN


def test_a_failure_after_the_handover_says_it_cannot_be_rolled_back() -> None:
    # Arrange
    effects = _effects(finish=lambda: _fail("the residency row could not be written"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert "cannot be rolled back" in outcome.hint


def test_a_failure_after_the_handover_names_the_host_that_now_holds_the_lease() -> None:
    # Arrange
    effects = _effects(finish=lambda: _fail("the residency row could not be written"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert f"{DST} holds the lease" in outcome.hint


def test_a_failure_after_the_handover_does_not_warn_about_an_abandoned_standby() -> (
    None
):
    # Arrange: past the atomic point the "standby" is the live agent. Calling it
    # something to clean up would invite stopping the only running instance.
    effects = _effects(finish=lambda: _fail("the residency row could not be written"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.standby_left_running is False


def test_a_failure_at_the_handover_itself_is_still_reversible() -> None:
    # Arrange: the lease did NOT move, so this side of the point is the safe one.
    effects = _effects(hand_over_lease=lambda: _fail("the lease was held by another"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.relocation.phase == ABORTED


def test_a_failure_at_the_handover_says_the_host_record_is_untouched() -> None:
    # Arrange: the db write happens at DONE, so an abort here leaves the
    # recorded host exactly where it was.
    effects = _effects(hand_over_lease=lambda: _fail("the lease was held by another"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert "host in the state db is unchanged" in outcome.hint


def test_a_stopped_relocation_past_the_atomic_point_reports_it() -> None:
    # Arrange
    effects = _effects(finish=lambda: _fail("the residency row could not be written"))
    # Act
    outcome = execute(_fresh(), effects=effects, now=_clock())
    # Assert
    assert outcome.past_no_return is True


# ---------------------------------------------------------------------------
# a missing effect is refused, not skipped
# ---------------------------------------------------------------------------


def test_a_relocation_with_no_handover_effect_refuses_to_start() -> None:
    # Arrange: journalling its way to DONE having moved nothing is the most
    # convincing possible imitation of a successful relocation.
    effects = _effects(hand_over_lease=None)
    # Act
    call = lambda: execute(_fresh(), effects=effects, now=_clock())  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match=HANDOVER):
        call()


def test_the_missing_effect_refusal_names_every_phase_that_has_none() -> None:
    # Arrange
    effects = _effects(stop_source=None, finish=None)
    # Act
    call = lambda: execute(_fresh(), effects=effects, now=_clock())  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match=SOURCE_STOP):
        call()


# ---------------------------------------------------------------------------
# the result shapes refuse to be half-written
# ---------------------------------------------------------------------------


def test_a_step_that_did_not_succeed_must_name_the_next_action() -> None:
    # Arrange
    build = lambda: StepResult(ok=False, detail="it broke")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="hint")
    # Assert
    with caught:
        build()


def test_a_step_with_no_detail_is_unrepresentable() -> None:
    # Arrange: the detail is what the journal records months later.
    build = lambda: StepResult(ok=True, detail="")  # noqa: E731
    # Act
    caught = pytest.raises(ValueError, match="detail")
    # Assert
    with caught:
        build()


def test_an_incomplete_outcome_must_say_what_to_do_next() -> None:
    # Arrange
    build = lambda: ExecuteOutcome(  # noqa: E731
        completed=False, code=CODE_ABORTED, reason="stopped", relocation=_fresh()
    )
    # Act
    caught = pytest.raises(ValueError, match="what to do next")
    # Assert
    with caught:
        build()


def test_an_unattempted_standby_is_not_reported_as_left_running() -> None:
    # Arrange: measured 2026-08-11 on the canary run. The unbuilt target_standby
    # returned UNKNOWN, the outcome read that as "a standby may be running", and
    # the recovery instruction sent the operator to stop a process that had never
    # been started. A false alarm in a recovery instruction is not a safe default.
    from scitex_agent_container._lifecycle._relocate_execute import _standby_running
    from scitex_agent_container._lifecycle._relocate_phases import (
        TARGET_STANDBY,
        TRANSPORT,
    )

    # Act
    left = _standby_running(TRANSPORT, TARGET_STANDBY, unknown=True, attempted=False)
    # Assert
    assert left is False


def test_an_attempted_standby_that_could_not_be_measured_is_still_reported() -> None:
    # Arrange: the case the conservative default was written for and which must
    # NOT be lost — an effect that tried and could not tell may well have started
    # something, and "nothing was left running" there is the reassurance that
    # sends nobody to look.
    from scitex_agent_container._lifecycle._relocate_execute import _standby_running
    from scitex_agent_container._lifecycle._relocate_phases import (
        TARGET_STANDBY,
        TRANSPORT,
    )

    # Act
    left = _standby_running(TRANSPORT, TARGET_STANDBY, unknown=True, attempted=True)
    # Assert
    assert left is True


def test_a_step_result_is_attempted_by_default() -> None:
    # Arrange: every existing caller keeps its meaning, and a forgetful new one
    # over-warns rather than under-warns.
    from scitex_agent_container._lifecycle._relocate_execute import StepResult

    # Act
    result = StepResult(ok=True, detail="did a thing")
    # Assert
    assert result.attempted is True
