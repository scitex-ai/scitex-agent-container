"""The single fold, and the rule that UNKNOWN outranks a refutation.

Every branch is reached from a hand-built state, so the aggregation is proved
rather than trusted — including the branch that must NOT fire: a blind reading
can never be argued into a conviction.
"""

from __future__ import annotations

from scitex_agent_container._delivery._assess import (
    EXIT_DELIVERED,
    EXIT_NO_ROUTE,
    EXIT_REFUTED,
    EXIT_UNKNOWN,
    EXIT_UNSUBMITTED,
    assess_delivery,
)
from scitex_agent_container._delivery._state import DeliveryState


def _state(**signals):
    state = DeliveryState(agent="peer")
    for name, value in signals.items():
        state = state.with_signal(name, value, f"{name}={value}")
    return state


def _all_healthy(**overrides):
    signals = {
        "is_route_resolved": True,
        "is_payload_delivered": True,
        "is_payload_submitted": True,
    }
    signals.update(overrides)
    return _state(**signals)


def test_every_signal_healthy_verdicts_true():
    # Arrange
    state = _all_healthy()
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.verdict is True


def test_every_signal_healthy_exits_zero():
    # Arrange
    state = _all_healthy()
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.exit_code() == EXIT_DELIVERED


def test_default_state_verdicts_unknown():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.verdict is None


def test_unknown_outranks_a_refutation():
    # Arrange
    state = _state(is_route_resolved=True, is_payload_delivered=False)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.verdict is None


def test_unknown_names_the_unread_signal():
    # Arrange
    state = _state(is_route_resolved=True, is_payload_delivered=False)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.unresolved == ("is_payload_submitted",)


def test_unknown_warns_against_blind_resend():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert "Do not resend" in verdict.reason


def test_unknown_exits_could_not_determine():
    # Arrange
    state = _state(is_route_resolved=True, is_payload_delivered=True)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.exit_code() == EXIT_UNKNOWN


def test_missing_route_exits_no_route():
    # Arrange
    state = _state(
        is_route_resolved=False,
        is_payload_delivered=False,
        is_payload_submitted=False,
    )
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.exit_code() == EXIT_NO_ROUTE


def test_arrived_but_unsent_exits_unsubmitted():
    # Arrange
    state = _all_healthy(is_payload_submitted=False)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.exit_code() == EXIT_UNSUBMITTED


def test_arrived_but_unsent_verdicts_false():
    # Arrange
    state = _all_healthy(is_payload_submitted=False)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.verdict is False


def test_undelivered_exits_generic_refuted():
    # Arrange
    state = _all_healthy(is_payload_delivered=False)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.exit_code() == EXIT_REFUTED


def test_evidence_signals_never_change_verdict():
    # Arrange
    state = _all_healthy(is_login_banner_before=True, is_target_busy_before=True)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.verdict is True


def test_unreadable_pane_alone_stays_true():
    # Arrange
    state = _all_healthy(is_pane_readable=False)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.verdict is True


def test_refutation_names_the_deciding_signal():
    # Arrange
    state = _all_healthy(is_payload_submitted=False)
    # Act
    verdict = assess_delivery(state)
    # Assert
    assert verdict.deciding == ("is_payload_submitted",)
