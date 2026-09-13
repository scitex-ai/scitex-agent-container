"""Selected Hermes runs have an explicit, short steering boundary."""

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


def test_hermes_default_caps_one_run_at_two_minutes():
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
    assert budget == DEFAULT_HERMES_RUN_BUDGET_SECONDS == 120
