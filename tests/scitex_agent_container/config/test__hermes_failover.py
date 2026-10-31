"""Declared pool policy refuses ambiguity before credential resolution."""

import pytest

from scitex_agent_container.config._hermes_failover import (
    parse_selected_hermes_failover,
)


def _spec(policy):
    return {
        "harness": "hermes",
        "available_harnesses": {"hermes": {"failover": policy}},
    }


def test_explicit_balance_keeps_the_declared_accounts_and_no_backup():
    # Arrange
    spec = _spec(
        {
            "accounts": {"muse": ["GO_ADMITTED_A", "GO_ADMITTED_B"]},
            "strategy": "round_robin",
        }
    )

    # Act
    policy = parse_selected_hermes_failover(spec)

    # Assert
    assert (policy.accounts, policy.engines, policy.strategy) == (
        {"muse": ["GO_ADMITTED_A", "GO_ADMITTED_B"]},
        [],
        "round_robin",
    )


def test_existing_policy_keeps_fill_first():
    # Arrange
    spec = _spec({"accounts": {"muse": ["GO_ADMITTED_A"]}})

    # Act
    policy = parse_selected_hermes_failover(spec)

    # Assert
    assert policy.strategy == "fill_first"


@pytest.mark.parametrize("strategy", ["typo", "random", "", None, True, []])
def test_unknown_strategy_refuses_before_any_credential_resolution(strategy):
    # Arrange
    spec = _spec({"accounts": {"muse": ["GO_ADMITTED_A"]}, "strategy": strategy})

    # Act
    # Assert
    with pytest.raises(ValueError, match="strategy must be"):
        parse_selected_hermes_failover(spec)


def test_balance_cannot_claim_an_undeclared_ambient_pool():
    # Arrange
    spec = _spec({"strategy": "round_robin"})

    # Act
    # Assert
    with pytest.raises(ValueError, match="requires declared account pools"):
        parse_selected_hermes_failover(spec)


def test_same_account_alias_cannot_be_listed_twice():
    # Arrange
    spec = _spec(
        {
            "accounts": {"muse": ["GO_ADMITTED_A", "GO_ADMITTED_A"]},
            "strategy": "round_robin",
        }
    )

    # Act
    # Assert
    with pytest.raises(ValueError, match="repeats an env name"):
        parse_selected_hermes_failover(spec)
