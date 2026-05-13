"""Tests for config._parsers._startup: parse_startup_commands + parse_startup."""

from __future__ import annotations

from scitex_agent_container.config._parsers._startup import (
    parse_startup,
    parse_startup_commands,
)

# ---------------------------------------------------------------------------
# parse_startup_commands (legacy top-level field)
# ---------------------------------------------------------------------------


def test_legacy_missing_returns_empty():
    assert parse_startup_commands({}) == []


def test_legacy_drops_items_without_command():
    out = parse_startup_commands(
        {"startup_commands": [{"command": "echo hi"}, {"delay": 5}, "skip-me"]}
    )
    assert len(out) == 1
    assert out[0].command == "echo hi"
    assert out[0].delay == 0


def test_legacy_coerces_delay_int():
    out = parse_startup_commands({"startup_commands": [{"command": "x", "delay": "3"}]})
    assert out[0].delay == 3


# ---------------------------------------------------------------------------
# parse_startup (new spec.startup block)
# ---------------------------------------------------------------------------


def test_startup_missing_falls_back_to_legacy_commands():
    spec = {"startup_commands": [{"command": "echo legacy"}]}
    s = parse_startup(spec)
    assert s.ready_patterns == []
    assert s.ready_idle_ticks == 3
    assert s.ready_poll_interval_seconds == 0.5
    assert s.ready_timeout_seconds == 60.0
    assert s.on_timeout == "capture_and_proceed"
    assert len(s.commands) == 1
    assert s.commands[0].command == "echo legacy"


def test_startup_non_dict_falls_back_to_legacy():
    s = parse_startup({"startup": "garbage", "startup_commands": []})
    assert s.commands == []


def test_startup_parses_ready_patterns_string_and_dict():
    s = parse_startup(
        {
            "startup": {
                "ready_patterns": ["^ready$", {"regex": "DONE"}, {}, 42],
            }
        }
    )
    regexes = [p.regex for p in s.ready_patterns]
    assert regexes == ["^ready$", "DONE"]


def test_startup_numeric_clamps_and_coercion():
    s = parse_startup(
        {
            "startup": {
                "ready_idle_ticks": 0,  # clamps to 1
                "ready_poll_interval_seconds": 0.001,  # clamps to 0.05
                "ready_timeout_seconds": 0.1,  # clamps to 1.0
            }
        }
    )
    assert s.ready_idle_ticks == 1
    assert s.ready_poll_interval_seconds == 0.05
    assert s.ready_timeout_seconds == 1.0


def test_startup_numeric_invalid_falls_back_to_defaults():
    s = parse_startup(
        {
            "startup": {
                "ready_idle_ticks": "abc",
                "ready_poll_interval_seconds": "x",
                "ready_timeout_seconds": "y",
            }
        }
    )
    assert s.ready_idle_ticks == 3
    assert s.ready_poll_interval_seconds == 0.5
    assert s.ready_timeout_seconds == 60.0


def test_startup_on_timeout_validated():
    s = parse_startup({"startup": {"on_timeout": "bogus"}})
    assert s.on_timeout == "capture_and_proceed"

    s2 = parse_startup({"startup": {"on_timeout": "capture_and_fail"}})
    assert s2.on_timeout == "capture_and_fail"


def test_startup_block_commands_take_precedence_over_legacy():
    spec = {
        "startup": {"commands": ["echo new"]},
        "startup_commands": [{"command": "echo old"}],
    }
    s = parse_startup(spec)
    assert [c.command for c in s.commands] == ["echo new"]


def test_startup_block_empty_commands_falls_back_to_legacy():
    spec = {
        "startup": {"commands": []},
        "startup_commands": [{"command": "echo old"}],
    }
    s = parse_startup(spec)
    assert [c.command for c in s.commands] == ["echo old"]
