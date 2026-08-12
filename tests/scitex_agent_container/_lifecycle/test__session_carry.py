"""An agent that resumes with no memory of the conversation that moved it has
been replaced, not relocated.

That is the defect this decision exists to prevent, measured on 2026-08-07: the
nas instance booted with NO memory of the originating conversation, because the
transcript lives inside the container overlay and nothing copied it.

The opposite mistake is just as real and less obvious: re-seeding a target that
has ALREADY booted would DISCARD its own history and re-fork from the source on
every restart. So "no" is sometimes the right answer, and the tests below pin
both directions.

The third answer is the one that has bitten this fleet repeatedly tonight —
UNKNOWN. A fact that was not observed must not read as "no". Deciding not to
carry because a probe failed produces exactly the 08-07 defect while looking
like a considered choice.

Pure decision, no I/O, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._session_carry import (
    CODE_ALREADY_DIVERGED,
    CODE_CARRY,
    CODE_NO_SOURCE_SESSION,
    CODE_NO_TRANSCRIPT,
    CODE_OPTED_OUT,
    CODE_UNKNOWN,
    SeedPlan,
    plan_session_carry,
)

UUID = "47f85b77-9a34-474e-a68d-12dd126e6f65"


def _plan(**overrides: object) -> SeedPlan:
    """A fully-observed, carryable situation, with one field swappable."""
    facts: dict[str, object] = dict(
        source_session_uuid=UUID,
        source_transcript_exists=True,
        target_has_own_marker=False,
        requested=True,
    )
    facts.update(overrides)
    return plan_session_carry(**facts)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the ordinary case
# ---------------------------------------------------------------------------


def test_a_live_session_with_a_transcript_is_carried() -> None:
    # Arrange
    plan = _plan()
    # Act
    carry = plan.carry
    # Assert
    assert carry is True


def test_a_carry_plan_names_the_session_it_will_seed() -> None:
    # Arrange: the marker written to the target is this uuid.
    plan = _plan()
    # Act
    uuid = plan.session_uuid
    # Assert
    assert uuid == UUID


def test_a_carry_plan_names_the_transcript_file() -> None:
    # Arrange: the copy is of <uuid>.jsonl, mirroring the source's layout.
    plan = _plan()
    # Act
    name = plan.transcript_name
    # Assert
    assert name == f"{UUID}.jsonl"


# ---------------------------------------------------------------------------
# first boot only — the refusal that protects a diverged target
# ---------------------------------------------------------------------------


def test_a_target_that_already_booted_is_not_re_seeded() -> None:
    # Arrange: re-seeding would discard the target's own history and re-fork
    # from the source on every restart.
    plan = _plan(target_has_own_marker=True)
    # Act
    carry = plan.carry
    # Assert
    assert carry is False


def test_the_diverged_refusal_explains_what_it_protects() -> None:
    # Arrange: this is a correct outcome, not a failure, and must read as one.
    plan = _plan(target_has_own_marker=True)
    # Act
    code = plan.code
    # Assert
    assert code == CODE_ALREADY_DIVERGED


# ---------------------------------------------------------------------------
# unknown is never "no"
# ---------------------------------------------------------------------------


def test_an_unobserved_target_marker_is_unknown_not_a_refusal() -> None:
    # Arrange: deciding "no" here would discard history on a guess.
    plan = _plan(target_has_own_marker=None)
    # Act
    carry = plan.carry
    # Assert
    assert carry is None


def test_an_unobserved_source_session_is_unknown_not_a_refusal() -> None:
    # Arrange: a failed read of session_id must not become "there is no session".
    plan = _plan(source_session_uuid=None)
    # Act
    carry = plan.carry
    # Assert
    assert carry is None


def test_an_unobserved_transcript_is_unknown_not_a_refusal() -> None:
    # Arrange
    plan = _plan(source_transcript_exists=None)
    # Act
    carry = plan.carry
    # Assert
    assert carry is None


def test_every_unknown_is_coded_unknown() -> None:
    # Arrange
    plan = _plan(target_has_own_marker=None)
    # Act
    code = plan.code
    # Assert
    assert code == CODE_UNKNOWN


def test_an_unknown_says_what_to_check() -> None:
    # Arrange: an unknown with no next step leaves the caller where it started.
    plan = _plan(source_session_uuid=None)
    # Act
    reason = plan.reason
    # Assert
    assert "session_id" in reason


# ---------------------------------------------------------------------------
# decided refusals
# ---------------------------------------------------------------------------


def test_a_source_with_no_live_session_carries_nothing() -> None:
    # Arrange: an empty uuid is an OBSERVED absence, unlike None.
    plan = _plan(source_session_uuid="")
    # Act
    code = plan.code
    # Assert
    assert code == CODE_NO_SOURCE_SESSION


def test_a_named_session_whose_transcript_is_missing_refuses() -> None:
    # Arrange: seeding a marker with no transcript produces an agent that
    # resumes into nothing — worse than starting fresh, because it looks resumed.
    plan = _plan(source_transcript_exists=False)
    # Act
    code = plan.code
    # Assert
    assert code == CODE_NO_TRANSCRIPT


def test_the_missing_transcript_refusal_still_names_the_session() -> None:
    # Arrange: naming the offending value is what makes it diagnosable.
    plan = _plan(source_transcript_exists=False)
    # Act
    uuid = plan.session_uuid
    # Assert
    assert uuid == UUID


def test_opting_out_is_honoured() -> None:
    # Arrange: --no-carry-session, for leaving a wedged session behind.
    plan = _plan(requested=False)
    # Act
    code = plan.code
    # Assert
    assert code == CODE_OPTED_OUT


def test_opting_out_says_what_is_being_given_up() -> None:
    # Arrange: the flag discards the conversation, and the help must not be coy.
    plan = _plan(requested=False)
    # Act
    reason = plan.reason
    # Assert
    assert "no memory" in reason


def test_opting_out_beats_every_other_consideration() -> None:
    # Arrange: an explicit human decision is not overridden by observations,
    # not even unknown ones.
    plan = _plan(requested=False, target_has_own_marker=None, source_session_uuid=None)
    # Act
    carry = plan.carry
    # Assert
    assert carry is False


# ---------------------------------------------------------------------------
# the shape validates itself
# ---------------------------------------------------------------------------


def test_a_carry_plan_without_a_session_uuid_is_rejected() -> None:
    # Arrange: a plan that says "carry" but cannot say what would be a caller's
    # problem three layers downstream.
    fields = dict(carry=True, code=CODE_CARRY, reason="ok", transcript_name="x.jsonl")

    # Act
    def build() -> SeedPlan:
        return SeedPlan(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_carry_plan_without_a_transcript_name_is_rejected() -> None:
    # Arrange
    fields = dict(carry=True, code=CODE_CARRY, reason="ok", session_uuid=UUID)

    # Act
    def build() -> SeedPlan:
        return SeedPlan(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_plan_refuses_an_empty_reason() -> None:
    # Arrange
    fields = dict(carry=False, code=CODE_OPTED_OUT, reason="")

    # Act
    def build() -> SeedPlan:
        return SeedPlan(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_an_unknown_carrying_a_success_code_is_rejected() -> None:
    # Arrange: the shape that lets an unknown pass for a decision.
    fields = dict(carry=None, code=CODE_CARRY, reason="contradictory")

    # Act
    def build() -> SeedPlan:
        return SeedPlan(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()
