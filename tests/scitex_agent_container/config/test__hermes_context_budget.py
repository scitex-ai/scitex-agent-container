"""Deterministic shared-engine context budget tests."""

import pytest

from scitex_agent_container.config._hermes_context_budget import (
    HermesFleetBudgetError,
    derive_hermes_fleet_context_budget,
)


def test_six_agents_on_observed_qwen_capacity_derive_256k_threshold():
    # Arrange
    capacity = 2_180_096
    # Act
    result = derive_hermes_fleet_context_budget(
        engine_capacity_tokens=capacity,
        agent_count=6,
        capacity_age_seconds=0,
    )
    # Assert
    assert (
        result.threshold_tokens,
        result.usable_capacity_tokens,
        result.reserved_capacity_tokens,
    ) == (262_144, 1_572_864, 607_232)


@pytest.mark.parametrize(
    "overrides",
    [
        {"engine_capacity_tokens": None},
        {"capacity_age_seconds": None},
        {"capacity_age_seconds": 31},
        {"agent_count": 0},
    ],
)
def test_fleet_budget_fails_closed_on_untrusted_inputs(overrides):
    # Arrange
    kwargs = {
        "engine_capacity_tokens": 2_180_096,
        "agent_count": 6,
        "capacity_age_seconds": 0,
    }
    kwargs.update(overrides)
    # Act
    def action():
        derive_hermes_fleet_context_budget(**kwargs)

    # Assert
    with pytest.raises(HermesFleetBudgetError):
        action()
