"""Tests for config._parsers._startup: parse_startup_commands."""

from __future__ import annotations

from scitex_agent_container.config._parsers._startup import (
    parse_startup_commands,
)

# ---------------------------------------------------------------------------
# parse_startup_commands (legacy top-level field)
# ---------------------------------------------------------------------------


def test_legacy_missing_returns_empty():
    # Arrange
    spec: dict = {}
    # Act
    out = parse_startup_commands(spec)
    # Assert
    assert out == []


def test_legacy_drops_items_without_command_keeps_only_valid():
    # Arrange
    spec = {"startup_commands": [{"command": "echo hi"}, {"delay": 5}, "skip-me"]}
    # Act
    out = parse_startup_commands(spec)
    # Assert
    assert len(out) == 1


def test_legacy_drops_items_without_command_preserves_command_value():
    # Arrange
    spec = {"startup_commands": [{"command": "echo hi"}, {"delay": 5}, "skip-me"]}
    # Act
    out = parse_startup_commands(spec)
    # Assert
    assert out[0].command == "echo hi"


def test_legacy_drops_items_without_command_defaults_delay_to_zero():
    # Arrange
    spec = {"startup_commands": [{"command": "echo hi"}, {"delay": 5}, "skip-me"]}
    # Act
    out = parse_startup_commands(spec)
    # Assert
    assert out[0].delay == 0


def test_legacy_coerces_delay_int():
    # Arrange
    spec = {"startup_commands": [{"command": "x", "delay": "3"}]}
    # Act
    out = parse_startup_commands(spec)
    # Assert
    assert out[0].delay == 3

