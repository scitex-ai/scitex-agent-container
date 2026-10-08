"""Tests for self-healing redelivery: confirm -> retry -> route-switch.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks; the deliver
seam takes plain functions with the same ``(agent, message, *,
strategy)`` signature carrying scripted DeliveryStates.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._delivery._assess import (
    EXIT_DELIVERED,
    EXIT_NO_ROUTE,
    EXIT_REFUTED,
    EXIT_UNKNOWN,
    EXIT_UNSUBMITTED,
)
from scitex_agent_container._delivery._redeliver import (
    RedeliveryPolicy,
    redeliver,
)
from scitex_agent_container._delivery._state import DeliveryState


# ---------------------------------------------------------------------------
# Scripted states — one per fold outcome
# ---------------------------------------------------------------------------


def _state(agent: str, **signals: bool | None) -> DeliveryState:
    state = DeliveryState(agent=agent, strategy="tui")
    for name, value in signals.items():
        state = state.with_signal(name, value, f"{name}={value}")
    return state


def _delivered(agent: str = "peer") -> DeliveryState:
    return _state(
        agent,
        is_route_resolved=True,
        is_payload_delivered=True,
        is_payload_submitted=True,
    )


def _refuted(agent: str = "peer") -> DeliveryState:
    # Complete information, generic failure -> EXIT_REFUTED (deciding
    # names neither the route nor the submit signal).
    return _state(
        agent,
        is_route_resolved=True,
        is_payload_delivered=False,
        is_payload_submitted=True,
    )


def _no_route(agent: str = "peer") -> DeliveryState:
    return _state(
        agent,
        is_route_resolved=False,
        is_payload_delivered=True,
        is_payload_submitted=True,
    )


def _unsubmitted(agent: str = "peer") -> DeliveryState:
    return _state(
        agent,
        is_route_resolved=True,
        is_payload_delivered=True,
        is_payload_submitted=False,
    )


def _unknown(agent: str = "peer") -> DeliveryState:
    return DeliveryState(agent=agent, strategy="tui")


def _script(*states: DeliveryState):
    """A deliver_fn playing back ``states`` in order; records strategies."""
    return _Script(*states)


class _Script:
    """Playback deliver_fn with the production ``(agent, message, *,
    strategy)`` signature. Plain object, not a mock."""

    def __init__(self, *states: DeliveryState) -> None:
        self._states = states
        self.seen: list[str] = []
        self.calls = 0

    def __call__(
        self, agent: str, message: str, *, strategy: str = "auto"
    ) -> DeliveryState:
        self.seen.append(strategy)
        state = self._states[min(self.calls, len(self._states) - 1)]
        self.calls += 1
        return state


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def test_first_attempt_delivered_confirms_without_retry():
    # Arrange
    sender = _script(_delivered())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.delivered is True


def test_first_attempt_delivered_makes_exactly_one_send():
    # Arrange
    sender = _script(_delivered())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.attempts_made == 1


def test_first_attempt_delivered_records_confirmed_action():
    # Arrange
    sender = _script(_delivered())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.actions == ("confirmed",)


# ---------------------------------------------------------------------------
# retry (generic REFUTED only)
# ---------------------------------------------------------------------------


def test_refuted_then_delivered_recovers_on_retry():
    # Arrange
    sender = _script(_refuted(), _delivered())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.delivered is True


def test_refuted_then_delivered_marks_retried_and_confirmed():
    # Arrange
    sender = _script(_refuted(), _delivered())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.actions == ("retried", "confirmed")


def test_refuted_forever_exhausts_the_retry_budget():
    # Arrange
    sender = _script(_refuted())
    # Act
    report = redeliver(
        "peer",
        "hello",
        strategy="tui",
        policy=RedeliveryPolicy(max_retries=2),
        deliver_fn=sender,
    )
    # Assert
    assert report.attempts_made == 3


def test_refuted_forever_escalates_with_report():
    # Arrange
    sender = _script(_refuted())
    # Act
    report = redeliver(
        "peer",
        "hello",
        strategy="tui",
        policy=RedeliveryPolicy(max_retries=2),
        deliver_fn=sender,
    )
    # Assert
    assert report.actions[-1] == "escalated"


# ---------------------------------------------------------------------------
# route-switch
# ---------------------------------------------------------------------------


def test_no_route_switches_strategy_instead_of_resending():
    # Arrange
    sender = _script(_no_route(), _delivered())
    # Act
    report = redeliver(
        "peer",
        "hello",
        strategy="tui",
        policy=RedeliveryPolicy(switch_strategy="sdk"),
        deliver_fn=sender,
    )
    # Assert
    assert sender.seen == ["tui", "sdk"]


def test_no_route_switch_records_the_switch_action():
    # Arrange
    sender = _script(_no_route(), _delivered())
    # Act
    report = redeliver(
        "peer",
        "hello",
        strategy="tui",
        policy=RedeliveryPolicy(switch_strategy="sdk"),
        deliver_fn=sender,
    )
    # Assert
    assert "route-switched tui->sdk" in report.actions


def test_no_route_without_switch_sends_once_and_escalates():
    # Arrange
    sender = _script(_no_route())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.attempts_made == 1


def test_no_route_remedy_names_liveness_not_resend():
    # Arrange
    sender = _script(_no_route())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert "do NOT" in report.remedy and "resend" in report.remedy


def test_refuted_retries_then_switch_fires_when_armed():
    # Arrange
    sender = _script(_refuted(), _refuted(), _delivered())
    # Act
    report = redeliver(
        "peer",
        "hello",
        strategy="tui",
        policy=RedeliveryPolicy(max_retries=1, switch_strategy="sdk"),
        deliver_fn=sender,
    )
    # Assert
    assert sender.seen == ["tui", "tui", "sdk"]


# ---------------------------------------------------------------------------
# must-never-resend outcomes
# ---------------------------------------------------------------------------


def test_unknown_never_resends():
    # Arrange
    sender = _script(_unknown())
    # Act
    report = redeliver(
        "peer",
        "hello",
        strategy="tui",
        policy=RedeliveryPolicy(max_retries=5),
        deliver_fn=sender,
    )
    # Assert
    assert report.attempts_made == 1


def test_unknown_folds_to_exit_unknown():
    # Arrange
    sender = _script(_unknown())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.attempts[0].exit_code == EXIT_UNKNOWN


def test_unsubmitted_never_resends():
    # Arrange
    sender = _script(_unsubmitted())
    # Act
    report = redeliver(
        "peer",
        "hello",
        strategy="tui",
        policy=RedeliveryPolicy(max_retries=5, switch_strategy="sdk"),
        deliver_fn=sender,
    )
    # Assert
    assert report.attempts_made == 1


def test_unsubmitted_folds_to_exit_unsubmitted():
    # Arrange
    sender = _script(_unsubmitted())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.attempts[0].exit_code == EXIT_UNSUBMITTED


def test_unsubmitted_remedy_is_enter_not_resend():
    # Arrange
    sender = _script(_unsubmitted())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert "Enter" in report.remedy


# ---------------------------------------------------------------------------
# policy + report shape
# ---------------------------------------------------------------------------


def test_negative_max_retries_rejected():
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        RedeliveryPolicy(max_retries=-1)


def test_report_summary_names_agent_and_attempts():
    # Arrange
    sender = _script(_refuted(), _delivered())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert "agent=peer" in report.summary_text()


def test_report_dict_carries_every_attempt_fold():
    # Arrange
    sender = _script(_refuted(), _delivered())
    # Act
    payload = redeliver(
        "peer", "hello", strategy="tui", deliver_fn=sender
    ).to_dict()
    # Assert
    assert [a["exit_code"] for a in payload["attempts"]] == [
        EXIT_REFUTED,
        EXIT_DELIVERED,
    ]


def test_no_route_folds_to_exit_no_route():
    # Arrange
    sender = _script(_no_route())
    # Act
    report = redeliver("peer", "hello", strategy="tui", deliver_fn=sender)
    # Assert
    assert report.attempts[0].exit_code == EXIT_NO_ROUTE
