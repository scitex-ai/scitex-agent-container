"""Tests for ``runtimes/_codex_hook_output`` — the hook stdout adapter."""

from __future__ import annotations

import json

from scitex_agent_container.runtimes._codex_hook_output import adapt_hook_output

_RTK = json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecisionReason": "RTK auto-rewrite",
            "updatedInput": {"command": "rtk ls /tmp"},
        }
    }
)


def test_updated_input_without_a_decision_gains_an_explicit_allow():
    # Arrange -- rtk's shape, measured 2026-09-05.
    text = _RTK
    # Act
    out = json.loads(adapt_hook_output(text))
    # Assert
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_stated_decision_is_kept():
    # Arrange -- a hook that denies must stay a denial.
    text = json.dumps(
        {"hookSpecificOutput": {"permissionDecision": "deny", "updatedInput": {}}}
    )
    # Act
    out = json.loads(adapt_hook_output(text))
    # Assert
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_non_json_output_passes_through_untouched():
    # Arrange -- most fleet hooks print prose or nothing.
    text = "BLOCKED by some_hook: reason\n"
    # Act
    out = adapt_hook_output(text)
    # Assert
    assert out == text


def test_empty_output_passes_through_untouched():
    # Arrange
    text = ""
    # Act
    out = adapt_hook_output(text)
    # Assert
    assert out == ""
