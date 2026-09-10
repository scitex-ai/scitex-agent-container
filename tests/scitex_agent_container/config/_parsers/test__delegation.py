"""The neutral delegation policy has strict, predictable defaults."""

import pytest

from scitex_agent_container.config._parsers._delegation import parse_delegation


def test_defaults_to_two_isolated_children():
    parsed = parse_delegation({})

    assert parsed.max_concurrent_children == 2
    assert parsed.worktree_isolation is True


def test_authored_policy_round_trips():
    parsed = parse_delegation(
        {
            "delegation": {
                "max_concurrent_children": 4,
                "worktree_isolation": False,
            }
        }
    )

    assert parsed.max_concurrent_children == 4
    assert parsed.worktree_isolation is False


@pytest.mark.parametrize("value", [True, 0, -1, 9, "2"])
def test_cap_must_be_a_bounded_integer(value):
    with pytest.raises(ValueError, match="integer between 1 and 8"):
        parse_delegation({"delegation": {"max_concurrent_children": value}})
