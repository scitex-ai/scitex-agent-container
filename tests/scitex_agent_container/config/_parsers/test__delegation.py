"""The neutral delegation policy has strict, predictable defaults."""

import pytest

from scitex_agent_container.config._parsers._delegation import parse_delegation


def test_defaults_to_two_isolated_children():
    # Arrange
    raw = {}
    # Act
    parsed = parse_delegation(raw)
    # Assert
    assert (parsed.max_concurrent_children, parsed.worktree_isolation) == (2, True)


def test_authored_policy_round_trips():
    # Arrange
    raw = {
        "delegation": {
            "max_concurrent_children": 4,
            "worktree_isolation": False,
        }
    }
    # Act
    parsed = parse_delegation(raw)
    # Assert
    assert (parsed.max_concurrent_children, parsed.worktree_isolation) == (4, False)


@pytest.mark.parametrize("value", [True, 0, -1, 9, "2"])
def test_cap_must_be_a_bounded_integer(value):
    # Arrange
    raw = {"delegation": {"max_concurrent_children": value}}
    # Act
    ctx = pytest.raises(ValueError, match="integer between 1 and 8")
    # Assert
    with ctx:
        parse_delegation(raw)
