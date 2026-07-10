"""Tests for the Mode A action layer (task #13, op-2026-06-12-13).

``backoff_agent`` is the per-agent transient-rate-limit backoff
decision: how long to wait before retrying the SAME account, plus the
consecutive-hit escalation accounting the classifier's docstring
reserves for the action layer ("5 backoffs in a row -> escalate to
ROTATE"). Pure function — no sleep, no IO, no clock read.

Test style (STX-TQ002 / STX-TQ007): explicit ``# Arrange`` / ``# Act``
/ ``# Assert`` markers each on their own line, in order; one logical
assertion per test. No mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._account.backoff_agent import (
    DEFAULT_ESCALATE_AFTER,
    DEFAULT_MIN_BACKOFF_S,
    BackoffDecision,
    backoff_agent,
)

# ---------------------------------------------------------------------------
# delay_s — floor vs exponential ramp
# ---------------------------------------------------------------------------


def test_first_hit_delay_is_the_floor_not_the_tiny_exponential_start():
    # Arrange — base_delay_s=1.0 at prior_consecutive_hits=0 would be 1.0s,
    # far too fast for a real provider rate-limit window.
    decision = backoff_agent(prior_consecutive_hits=0)
    # Act
    delay = decision.delay_s
    # Assert
    assert delay == DEFAULT_MIN_BACKOFF_S


def test_exponential_ramp_overtakes_the_floor_once_high_enough():
    # Arrange — base=1.0, prior=5 -> exponential=32.0 > floor=30.0.
    decision = backoff_agent(prior_consecutive_hits=5)
    # Act
    delay = decision.delay_s
    # Assert
    assert delay == 32.0


def test_delay_never_drops_below_the_custom_floor():
    # Arrange
    decision = backoff_agent(prior_consecutive_hits=0, min_backoff_s=5.0)
    # Act
    delay = decision.delay_s
    # Assert
    assert delay == 5.0


def test_custom_base_delay_feeds_the_exponential_ramp():
    # Arrange — base=10.0, prior=1 -> exponential=20.0 > floor=5.0.
    decision = backoff_agent(
        prior_consecutive_hits=1, base_delay_s=10.0, min_backoff_s=5.0
    )
    # Act
    delay = decision.delay_s
    # Assert
    assert delay == 20.0


# ---------------------------------------------------------------------------
# hit_count bookkeeping
# ---------------------------------------------------------------------------


def test_hit_count_increments_prior_by_one():
    # Arrange
    decision = backoff_agent(prior_consecutive_hits=2)
    # Act
    hit_count = decision.hit_count
    # Assert
    assert hit_count == 3


def test_zero_prior_hits_yields_hit_count_one():
    # Arrange
    decision = backoff_agent(prior_consecutive_hits=0)
    # Act
    hit_count = decision.hit_count
    # Assert
    assert hit_count == 1


def test_negative_prior_hits_raises_value_error():
    # Arrange
    raised: BaseException | None = None
    # Act
    try:
        backoff_agent(prior_consecutive_hits=-1)
    except ValueError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the raise IS the act, capturing lets the Assert check the type.)
        raised = exc
    # Assert
    assert isinstance(raised, ValueError)


# ---------------------------------------------------------------------------
# escalate_to_rotate — consecutive-hit escalation guard
# ---------------------------------------------------------------------------


def test_escalation_does_not_fire_below_threshold():
    # Arrange — prior=3 -> hit_count=4 < default escalate_after=5.
    decision = backoff_agent(prior_consecutive_hits=3)
    # Act
    escalate = decision.escalate_to_rotate
    # Assert
    assert escalate is False


def test_escalation_fires_exactly_at_threshold():
    # Arrange — prior=4 -> hit_count=5 == default escalate_after=5.
    decision = backoff_agent(prior_consecutive_hits=4)
    # Act
    escalate = decision.escalate_to_rotate
    # Assert
    assert escalate is True


def test_escalation_stays_true_beyond_threshold():
    # Arrange
    decision = backoff_agent(prior_consecutive_hits=10)
    # Act
    escalate = decision.escalate_to_rotate
    # Assert
    assert escalate is True


def test_custom_escalate_after_is_honoured():
    # Arrange — prior=1 -> hit_count=2 == custom escalate_after=2.
    decision = backoff_agent(prior_consecutive_hits=1, escalate_after=2)
    # Act
    escalate = decision.escalate_to_rotate
    # Assert
    assert escalate is True


def test_default_escalate_after_constant_is_five():
    # Arrange
    value = DEFAULT_ESCALATE_AFTER
    # Act
    matches = value == 5
    # Assert
    assert matches is True


# ---------------------------------------------------------------------------
# BackoffDecision — frozen dataclass contract
# ---------------------------------------------------------------------------


def test_backoff_decision_is_frozen_against_mutation():
    # Arrange
    decision = backoff_agent(prior_consecutive_hits=0)
    raised: BaseException | None = None
    # Act
    try:
        decision.delay_s = 999.0  # type: ignore[misc]
    except Exception as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; frozen mutation is the Act.)
        raised = exc
    # Assert
    assert raised is not None


def test_backoff_agent_returns_backoff_decision_instance():
    # Arrange
    decision = backoff_agent(prior_consecutive_hits=0)
    # Act
    is_decision = isinstance(decision, BackoffDecision)
    # Assert
    assert is_decision is True


@pytest.mark.parametrize("prior", [0, 1, 2, 3, 4, 5, 100])
def test_delay_s_is_always_at_least_the_floor(prior):
    # Arrange
    decision = backoff_agent(prior_consecutive_hits=prior)
    # Act
    at_least_floor = decision.delay_s >= DEFAULT_MIN_BACKOFF_S
    # Assert
    assert at_least_floor is True
