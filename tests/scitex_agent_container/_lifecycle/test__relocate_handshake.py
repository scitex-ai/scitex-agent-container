"""A one-way signal is not a handshake, and these tests are the reason why.

Measured 2026-08-11: a2a between two LIVE agents delivered nothing, and nobody
noticed until a human asked. Every one-way signal was green — both processes
ran, both sidecars listened, both dispatch calls returned accepted. A relocation
gated on "the target started" would have handed the lease into exactly that, and
`abort` refuses past the handover, so there would have been no way back.

So each test below removes exactly one of the four things a real round trip has
to show, and pins that removing it refuses:

    accepted only               -> the agent was never asked anything
    accepted, no reply          -> THE 08-11 failure
    reply with the wrong nonce  -> it answers some other message
    reply with the wrong answer -> the transport works, the loop did not

Pure predicates over observations. No transport, no sleeping, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_handshake import (
    CODE_NO_REPLY,
    CODE_NONCE_MISMATCH,
    CODE_NOT_ACCEPTED,
    CODE_OK,
    CODE_UNKNOWN,
    CODE_WORK_NOT_PROVEN,
    HandshakeFacts,
    HandshakeVerdict,
    evaluate_handshake,
)

NONCE = "reloc-7f3a91"
ANSWER = "scitex-compute-04:19019"
OBSERVER = "the source agent"


def _facts(**overrides: object) -> HandshakeFacts:
    """A complete, honest round trip. Each test removes exactly one part."""
    base = dict(
        challenge_accepted=True,
        reply_observed=True,
        observed_by=OBSERVER,
        reply_nonce=NONCE,
        reply_answer=ANSWER,
    )
    base.update(overrides)
    return HandshakeFacts(**base)  # type: ignore[arg-type]


def _run(**overrides: object) -> HandshakeVerdict:
    return evaluate_handshake(_facts(**overrides), nonce=NONCE, expected_answer=ANSWER)


# ---------------------------------------------------------------------------
# the whole round trip
# ---------------------------------------------------------------------------


def test_a_complete_round_trip_proves_the_target_can_do_agent_work() -> None:
    # Arrange
    facts = _facts()
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.proven is True


def test_a_complete_round_trip_carries_the_ok_code() -> None:
    # Arrange
    facts = _facts()
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.code == CODE_OK


def test_a_proven_handshake_names_who_observed_the_reply() -> None:
    # Arrange: "the coordinator saw it" and "the source saw it" are different
    # measurements, and a report that omits which invites the stronger reading.
    facts = _facts()
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert OBSERVER in verdict.reason


# ---------------------------------------------------------------------------
# A -> B alone
# ---------------------------------------------------------------------------


def test_a_challenge_the_target_refused_proves_nothing() -> None:
    # Arrange
    facts = _facts(challenge_accepted=False)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.code == CODE_NOT_ACCEPTED


def test_a_refused_challenge_says_the_agent_was_never_asked() -> None:
    # Arrange: the distinction matters — nothing has been learned about the
    # agent's behaviour, only about its sidecar.
    facts = _facts(challenge_accepted=False)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert "never asked" in verdict.reason


def test_an_unobserved_acceptance_is_unknown_not_a_refusal() -> None:
    # Arrange: a dispatch call that raised is not a refusal by the target.
    facts = _facts(challenge_accepted=None)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.proven is None


# ---------------------------------------------------------------------------
# accepted but silent — the measured failure
# ---------------------------------------------------------------------------


def test_accepted_with_no_reply_refuses() -> None:
    # Arrange: this is the exact 2026-08-11 shape.
    facts = _facts(reply_observed=False)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.proven is False


def test_accepted_with_no_reply_carries_its_own_code() -> None:
    # Arrange
    facts = _facts(reply_observed=False)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.code == CODE_NO_REPLY


def test_a_silent_target_tells_the_operator_not_to_hand_over_the_lease() -> None:
    # Arrange
    facts = _facts(reply_observed=False)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert "do NOT hand over the lease" in verdict.hint


def test_an_unobserved_reply_is_unknown_because_waiting_longer_may_answer_it() -> None:
    # Arrange: "I did not see one in the time I waited" is not "the target is
    # broken", and the two call for different next actions.
    facts = _facts(reply_observed=None)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.code == CODE_UNKNOWN


# ---------------------------------------------------------------------------
# correlation — a reply must be THE reply
# ---------------------------------------------------------------------------


def test_a_reply_carrying_another_nonce_is_refused() -> None:
    # Arrange: without correlation, a relocation retried three times eventually
    # finds a reply that proves nothing.
    facts = _facts(reply_nonce="an-older-turn")
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.code == CODE_NONCE_MISMATCH


def test_a_mismatched_nonce_names_both_values() -> None:
    # Arrange
    facts = _facts(reply_nonce="an-older-turn")
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert "an-older-turn" in verdict.reason


def test_a_reply_with_no_readable_nonce_is_unknown() -> None:
    # Arrange
    facts = _facts(reply_nonce=None)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.proven is None


def test_an_empty_nonce_is_refused_at_the_call_rather_than_weakening_the_gate() -> None:
    # Arrange: an empty nonce would make every reply correlate.
    facts = _facts()
    # Act
    call = lambda: evaluate_handshake(facts, nonce="", expected_answer=ANSWER)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="non-empty nonce"):
        call()


# ---------------------------------------------------------------------------
# proof of work — an echo is not an agent
# ---------------------------------------------------------------------------


def test_a_correlated_reply_with_the_wrong_answer_is_refused() -> None:
    # Arrange: the message path works and the loop did not do the work.
    facts = _facts(reply_answer="i am fine thanks")
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.code == CODE_WORK_NOT_PROVEN


def test_an_unproven_answer_is_caught_before_the_lease_moves() -> None:
    # Arrange
    facts = _facts(reply_answer="i am fine thanks")
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert "before the lease moved" in verdict.hint


def test_a_reply_with_no_answer_at_all_is_unknown() -> None:
    # Arrange: a correlated reply with nothing in it shows the message
    # round-tripped, not that the loop did anything.
    facts = _facts(reply_answer=None)
    # Act
    verdict = evaluate_handshake(facts, nonce=NONCE, expected_answer=ANSWER)
    # Assert
    assert verdict.proven is None


def test_an_empty_expected_answer_is_refused_rather_than_waiving_the_check() -> None:
    # Arrange: a check that can be disabled by passing nothing will be.
    facts = _facts()
    # Act
    call = lambda: evaluate_handshake(facts, nonce=NONCE, expected_answer="")  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="non-empty expected_answer"):
        call()


# ---------------------------------------------------------------------------
# the verdict shape itself
# ---------------------------------------------------------------------------


def test_a_refusal_without_a_next_action_is_unrepresentable() -> None:
    # Arrange
    build = lambda: HandshakeVerdict(  # noqa: E731
        proven=False, code=CODE_NO_REPLY, reason="nothing came back"
    )
    # Act
    caught = pytest.raises(ValueError, match="hint")
    # Assert
    with caught:
        build()


def test_the_verdict_defines_no_bool_so_a_refusal_cannot_read_as_permission() -> None:
    # Arrange: `if verdict:` on a dataclass is true even for a refusal, and the
    # step after this one is the handover.
    verdict = _run(reply_observed=False)
    # Act
    has_bool = "__bool__" in type(verdict).__dict__
    # Assert
    assert has_bool is False
