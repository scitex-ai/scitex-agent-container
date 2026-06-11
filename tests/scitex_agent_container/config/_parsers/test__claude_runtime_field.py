"""Tests for ``spec.claude.runtime`` (Day-2 E).

The runtime field selects between the SDK runner (default) and the
tmux interactive-TUI driver salvaged for the post-2026-06-15 SDK
split.

* default → ``"sdk"``
* explicit ``"tmux"`` → ``"tmux"``
* unknown values are NOT rejected here (parser is value-tolerant);
  the diagnostic lives in the validator, exercised in
  ``test__validation_claude_runtime``.

TQ-compliant: module docstring summarises intent; AAA on every test;
each test asserts exactly one fact.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._claude import parse_claude


def test_missing_runtime_defaults_to_sdk():
    # Arrange
    spec: dict = {}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.runtime == "sdk"


def test_empty_claude_block_defaults_runtime_to_sdk():
    # Arrange
    spec: dict = {"claude": {}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.runtime == "sdk"


def test_explicit_sdk_runtime_parses_as_sdk():
    # Arrange
    spec: dict = {"claude": {"runtime": "sdk"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.runtime == "sdk"


def test_explicit_tmux_runtime_parses_as_tmux():
    # Arrange
    spec: dict = {"claude": {"runtime": "tmux"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.runtime == "tmux"


@pytest.mark.parametrize("bad", [None, 42, [], {}])
def test_non_string_runtime_falls_back_to_sdk(bad):
    """Parser is value-tolerant — the validator owns the rejection diagnostic."""
    # Arrange
    spec: dict = {"claude": {"runtime": bad}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.runtime == "sdk"


def test_unknown_runtime_value_passes_through_for_validator_to_catch():
    """An unknown but non-empty string passes through verbatim.

    The validator (see ``test__validation_claude_runtime``) is the
    single source of the "unknown runtime" diagnostic.
    """
    # Arrange
    spec: dict = {"claude": {"runtime": "screen"}}
    # Act
    result = parse_claude(spec)
    # Assert
    assert result.runtime == "screen"
