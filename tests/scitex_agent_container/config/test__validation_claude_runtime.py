"""Tests for ``spec.claude.runtime`` validation (Day-2 E).

The runtime field selects between the SDK runner (default) and the
tmux interactive-TUI driver. The validator owns the friendly
diagnostic for unknown values.

* missing / unset → accepted (parser defaults to ``"sdk"``).
* explicit ``"sdk"`` / ``"tmux"`` → accepted.
* anything else → rejected with a message that names the allowed set.

TQ-compliant: module docstring summarises intent; AAA on every test;
each test asserts exactly one fact.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._validation import validate_raw

_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {
        "runtime": "apptainer",
    },
}


def _spec(claude_runtime):
    if claude_runtime is None:
        return {**_BASE, "spec": {**_BASE["spec"], "claude": {}}}
    return {
        **_BASE,
        "spec": {
            **_BASE["spec"],
            "claude": {"runtime": claude_runtime},
        },
    }


def test_missing_runtime_passes_validation():
    # Arrange
    raw = _spec(None)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    bad = [e for e in errors if "spec.claude.runtime" in e]
    assert bad == []


def test_sdk_runtime_passes_validation():
    # Arrange
    raw = _spec("sdk")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    bad = [e for e in errors if "spec.claude.runtime" in e]
    assert bad == []


def test_tmux_runtime_passes_validation():
    # Arrange
    raw = _spec("tmux")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    bad = [e for e in errors if "spec.claude.runtime" in e]
    assert bad == []


@pytest.mark.parametrize("bad_value", ["screen", "podman", "container", "x"])
def test_unknown_runtime_is_rejected(bad_value):
    # Arrange
    raw = _spec(bad_value)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    bad = [e for e in errors if "spec.claude.runtime" in e]
    assert bad, f"expected rejection of {bad_value!r}, got no errors"


def test_unknown_runtime_error_names_allowed_set():
    """The diagnostic must name BOTH allowed values so the operator can fix it."""
    # Arrange
    raw = _spec("screen")
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.runtime" in e]
    # Assert
    assert any("'sdk'" in e and "'tmux'" in e for e in bad), bad


def test_unknown_runtime_error_echoes_offending_value():
    # Arrange
    raw = _spec("podman")
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.runtime" in e]
    # Assert
    assert any("'podman'" in e for e in bad), bad
