"""Selected Hermes runs have an explicit, short steering boundary."""

from scitex_agent_container.config._hermes_run_budget import (
    DEFAULT_HERMES_MAX_TURNS,
    DEFAULT_HERMES_RUN_BUDGET_SECONDS,
    parse_selected_hermes_max_turns,
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


def test_hermes_defaults_do_not_cap_autonomous_work():
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


def test_explicit_null_limits_are_unlimited():
    spec = {
        "harness": "hermes",
        "available_harnesses": {
            "hermes": {
                "session": {"mode": "continue", "max_age_minutes": None},
                "max_turns": None,
                "run_budget_seconds": None,
            }
        },
    }

    assert parse_selected_hermes_max_turns(spec) is DEFAULT_HERMES_MAX_TURNS is None
    assert parse_selected_hermes_run_budget(spec) is None
