"""Tests for config._parsers._context_management.parse_context_management."""

from __future__ import annotations

from scitex_agent_container.config._parsers._context_management import (
    parse_context_management,
)


def test_missing_returns_defaults():
    c = parse_context_management({})
    assert c.trigger_at_percent == 70.0
    assert c.strategy == "noop"
    assert c.warn_before_n_checks == 0
    assert c.check_interval_seconds == 300
    assert "<agent>.json" in c.state_file


def test_explicit_none_treated_as_empty():
    c = parse_context_management({"context_management": None})
    assert c.strategy == "noop"


def test_strategy_validated_invalid_falls_back():
    c = parse_context_management({"context_management": {"strategy": "explode"}})
    assert c.strategy == "noop"


def test_strategy_valid_options_kept():
    for s in ("compact", "restart", "noop"):
        c = parse_context_management({"context_management": {"strategy": s}})
        assert c.strategy == s


def test_trigger_invalid_falls_back():
    c = parse_context_management({"context_management": {"trigger_at_percent": "abc"}})
    assert c.trigger_at_percent == 70.0


def test_trigger_accepts_string_number():
    c = parse_context_management({"context_management": {"trigger_at_percent": "85.5"}})
    assert c.trigger_at_percent == 85.5


def test_warn_before_n_clamped_to_zero():
    c = parse_context_management({"context_management": {"warn_before_n_checks": -5}})
    assert c.warn_before_n_checks == 0


def test_warn_before_n_invalid_falls_back_to_zero():
    c = parse_context_management({"context_management": {"warn_before_n_checks": "x"}})
    assert c.warn_before_n_checks == 0


def test_check_interval_clamped_to_one_minimum():
    c = parse_context_management({"context_management": {"check_interval_seconds": 0}})
    assert c.check_interval_seconds == 1


def test_check_interval_invalid_falls_back_to_300():
    c = parse_context_management(
        {"context_management": {"check_interval_seconds": "xx"}}
    )
    assert c.check_interval_seconds == 300


def test_state_file_passthrough():
    c = parse_context_management(
        {"context_management": {"state_file": "/var/sac/<agent>.json"}}
    )
    assert c.state_file == "/var/sac/<agent>.json"
