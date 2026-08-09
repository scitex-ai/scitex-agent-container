"""Tests for the fleet hook-arming diff (``runtimes._hook_arming_diff``).

The verifier a to_home change must pass before it is trusted: every agent armed
the same hooks, credited to the same layers, on both sides. These pin the
outcome buckets, the deliberately-conservative ``safe``, the three-valued
treatment of an agent that was never measured, and the validator that refuses
an internally inconsistent diff.

STX-NM002: no mocks — pure dict in, dataclass out.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.runtimes._hook_arming_diff import (
    HookArmingDiff,
    diff_hook_arming,
)

_ONE = {"a": {"PreToolUse": {"guard.sh": "user-shared"}}}


def test_identical_snapshots_are_safe() -> None:
    # Arrange
    before = after = _ONE
    # Act
    diff = diff_hook_arming(before, after)
    # Assert
    assert diff.safe is True


def test_identical_snapshots_list_the_agent_as_unchanged() -> None:
    # Arrange
    before = after = _ONE
    # Act
    diff = diff_hook_arming(before, after)
    # Assert
    assert diff.unchanged == ("a",)


def test_a_dropped_hook_is_reported_as_lost() -> None:
    # Arrange — the dangerous direction: a guard stopped being armed.
    after = {"a": {"PreToolUse": {}}}
    # Act
    diff = diff_hook_arming(_ONE, after)
    # Assert
    assert diff.lost == {"a": ["PreToolUse: guard.sh"]}


def test_a_dropped_hook_is_not_safe() -> None:
    # Arrange
    after = {"a": {"PreToolUse": {}}}
    # Act
    diff = diff_hook_arming(_ONE, after)
    # Assert
    assert diff.safe is False


def test_a_gained_hook_is_also_not_safe() -> None:
    # Arrange — the promise is "identical", not "no worse".
    after = {"a": {"PreToolUse": {"guard.sh": "user-shared", "new.sh": "per-agent"}}}
    # Act
    diff = diff_hook_arming(_ONE, after)
    # Assert
    assert diff.safe is False


def test_a_relayered_hook_is_reported_as_reattributed() -> None:
    # Arrange — same hook still armed, credited to a different layer.
    after = {"a": {"PreToolUse": {"guard.sh": "per-agent"}}}
    # Act
    diff = diff_hook_arming(_ONE, after)
    # Assert
    assert diff.reattributed == {
        "a": ["PreToolUse: guard.sh (user-shared -> per-agent)"]
    }


def test_an_agent_missing_after_is_unmeasured_not_unchanged() -> None:
    # Arrange — the three-valued case: absent is not "fine".
    # Act
    diff = diff_hook_arming(_ONE, {})
    # Assert
    assert diff.unmeasured == ("a",)


def test_an_agent_missing_after_is_not_counted_as_compared() -> None:
    # Arrange — it was never compared, so it must not inflate the total.
    # Act
    diff = diff_hook_arming(_ONE, {})
    # Assert
    assert diff.agents_compared == 0


def test_an_unmeasured_agent_makes_the_diff_unsafe() -> None:
    # Arrange — a migration must not pass over agents it never looked at.
    # Act
    diff = diff_hook_arming(_ONE, {})
    # Assert
    assert diff.safe is False


def test_an_agent_missing_before_is_unexpected() -> None:
    # Arrange
    # Act
    diff = diff_hook_arming({}, _ONE)
    # Assert
    assert diff.unexpected == ("a",)


def test_same_command_on_two_events_counts_as_two_armings() -> None:
    # Arrange — losing one of them is a real loss, so they must not collapse.
    before = {
        "a": {"PreToolUse": {"g.sh": "user-shared"}, "Stop": {"g.sh": "user-shared"}}
    }
    after = {"a": {"PreToolUse": {"g.sh": "user-shared"}}}
    # Act
    diff = diff_hook_arming(before, after)
    # Assert
    assert diff.lost == {"a": ["Stop: g.sh"]}


def test_empty_snapshots_compare_safe() -> None:
    # Arrange — nothing on either side is consistent, if uninteresting.
    # Act
    diff = diff_hook_arming({}, {})
    # Assert
    assert diff.safe is True


def test_summary_names_the_losing_agents_count() -> None:
    # Arrange
    after = {"a": {"PreToolUse": {}}}
    # Act
    diff = diff_hook_arming(_ONE, after)
    # Assert
    assert "1 agent(s) LOST hooks" in diff.summary()


def test_inconsistent_diff_is_refused_at_construction() -> None:
    # Arrange — claims two agents compared but accounts for one.
    # Act
    # Assert
    with pytest.raises(ValueError, match="under-reporting"):
        HookArmingDiff(agents_compared=2, unchanged=("a",))


def test_an_agent_cannot_be_both_unmeasured_and_unexpected() -> None:
    # Arrange — missing from both sides at once is impossible.
    # Act
    # Assert
    with pytest.raises(ValueError, match="impossible"):
        HookArmingDiff(unmeasured=("a",), unexpected=("a",))
