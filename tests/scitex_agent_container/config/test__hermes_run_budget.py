"""Hermes run budgets are opt-in and validated at every parser boundary."""

import pytest

from scitex_agent_container.config._hermes_run_budget import (
    DEFAULT_HERMES_RUN_BUDGET_SECONDS,
    parse_selected_hermes_run_budget,
)


def test_selected_hermes_entry_supplies_run_budget():
    # Arrange
    spec = {
        "harness": "hermes",
        "available_harnesses": {
            "hermes": {
                "session": {"mode": "continue", "max_age_minutes": None},
                "channels": [],
                "run_budget_seconds": 45,
            }
        },
    }
    # Act
    budget = parse_selected_hermes_run_budget(spec)
    # Assert
    assert budget == 45


def test_hermes_default_leaves_run_budget_disabled():
    # Arrange
    spec = {
        "harness": "hermes",
        "available_harnesses": {
            "hermes": {
                "session": {"mode": "continue", "max_age_minutes": None},
                "channels": [],
            }
        },
    }
    # Act
    budget = parse_selected_hermes_run_budget(spec)
    # Assert
    assert budget is DEFAULT_HERMES_RUN_BUDGET_SECONDS is None


@pytest.mark.parametrize("value", [None, True, 1.5, "90", 0, -1])
def test_direct_parser_rejects_explicit_invalid_run_budget(value):
    # Arrange
    spec = {
        "harness": "hermes",
        "available_harnesses": {"hermes": {"run_budget_seconds": value}},
    }

    def action():
        return parse_selected_hermes_run_budget(spec)

    # Act
    run = action

    # Assert
    with pytest.raises(ValueError, match="positive integer"):
        run()
