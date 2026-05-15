"""Tests for config._parsers._startup: parse_startup_commands + parse_startup."""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._startup import (
    parse_startup,
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


# ---------------------------------------------------------------------------
# parse_startup (new spec.startup block)
# ---------------------------------------------------------------------------


def test_startup_missing_falls_back_to_legacy_ready_patterns_empty():
    # Arrange
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_patterns == []


def test_startup_missing_falls_back_to_legacy_default_idle_ticks():
    # Arrange
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_idle_ticks == 3


def test_startup_missing_falls_back_to_legacy_default_poll_interval():
    # Arrange
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_poll_interval_seconds == 0.5


def test_startup_missing_falls_back_to_legacy_default_timeout():
    # Arrange
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_timeout_seconds == 60.0


def test_startup_missing_falls_back_to_legacy_default_on_timeout():
    # Arrange
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.on_timeout == "capture_and_proceed"


def test_startup_missing_falls_back_to_legacy_commands_count():
    # Arrange
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    # Act
    s = parse_startup(spec)
    # Assert
    assert len(s.commands) == 1


def test_startup_missing_falls_back_to_legacy_command_value():
    # Arrange
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.commands[0].command == "echo legacy"


def test_startup_non_dict_falls_back_to_legacy():
    # Arrange
    spec = {"startup": "garbage", "startup_commands": []}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.commands == []


def test_startup_parses_ready_patterns_string_and_dict():
    # Arrange
    spec = {
        "startup": {
            "ready_patterns": ["^ready$", {"regex": "DONE"}, {}, 42],
        }
    }
    # Act
    s = parse_startup(spec)
    # Assert
    assert [p.regex for p in s.ready_patterns] == ["^ready$", "DONE"]


def test_startup_idle_ticks_clamps_to_minimum_one():
    # Arrange
    spec = {"startup": {"ready_idle_ticks": 0}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_idle_ticks == 1


def test_startup_poll_interval_clamps_to_minimum():
    # Arrange
    spec = {"startup": {"ready_poll_interval_seconds": 0.001}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_poll_interval_seconds == 0.05


def test_startup_timeout_clamps_to_minimum():
    # Arrange
    spec = {"startup": {"ready_timeout_seconds": 0.1}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_timeout_seconds == 1.0


def test_startup_idle_ticks_invalid_falls_back_to_default():
    # Arrange
    spec = {"startup": {"ready_idle_ticks": "abc"}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_idle_ticks == 3


def test_startup_poll_interval_invalid_falls_back_to_default():
    # Arrange
    spec = {"startup": {"ready_poll_interval_seconds": "x"}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_poll_interval_seconds == 0.5


def test_startup_timeout_invalid_falls_back_to_default():
    # Arrange
    spec = {"startup": {"ready_timeout_seconds": "y"}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.ready_timeout_seconds == 60.0


def test_startup_on_timeout_bogus_value_falls_back_to_default():
    # Arrange
    spec = {"startup": {"on_timeout": "bogus"}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.on_timeout == "capture_and_proceed"


@pytest.mark.parametrize(
    "value",
    ["capture_and_proceed", "capture_and_fail"],
)
def test_startup_on_timeout_accepts_valid_value(value):
    # Arrange
    spec = {"startup": {"on_timeout": value}}
    # Act
    s = parse_startup(spec)
    # Assert
    assert s.on_timeout == value


def test_startup_block_commands_take_precedence_over_legacy():
    # Arrange
    spec = {
        "startup": {"commands": ["echo new"]},
        "startup_commands": [{"command": "echo old"}],
    }
    # Act
    s = parse_startup(spec)
    # Assert
    assert [c.command for c in s.commands] == ["echo new"]


def test_startup_block_empty_commands_falls_back_to_legacy():
    # Arrange
    spec = {
        "startup": {"commands": []},
        "startup_commands": [{"command": "echo old"}],
    }
    # Act
    s = parse_startup(spec)
    # Assert
    assert [c.command for c in s.commands] == ["echo old"]
