"""Tests for ``config._parsers._context_management.parse_context_management``.

Each test pins exactly one observable behaviour of the parser. The
``spec.context_management`` block is optional and collapses to a
``ContextManagementConfig`` with documented defaults when missing or
explicitly ``None``. The ``strategy`` field is validated against the
closed set ``{compact, restart, noop}`` with ``noop`` as the fall-back.
Numeric fields (``trigger_at_percent``, ``warn_before_n_checks``,
``check_interval_seconds``) are coerced from strings when possible and
fall back to their defaults on malformed input. ``warn_before_n_checks``
is clamped to ``>= 0`` and ``check_interval_seconds`` to ``>= 1``. The
``state_file`` field passes through verbatim and defaults to the
canonical ``~/.scitex/agent-container/state/<agent>.json`` placeholder.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape default-field invariants over one arrange/act collapse into
``pytest.parametrize`` over ``(attr, expected)`` pairs.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._context_management import (
    parse_context_management,
)

# ---------------------------------------------------------------------------
# Missing context_management block → default ContextManagementConfig
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("trigger_at_percent", 70.0),
        ("strategy", "noop"),
        ("warn_before_n_checks", 0),
        ("check_interval_seconds", 300),
    ],
)
def test_missing_block_yields_default_field(attr, expected):
    # Arrange
    spec: dict = {}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert getattr(result, attr) == expected


def test_missing_block_yields_default_state_file_with_agent_placeholder():
    # Arrange
    spec: dict = {}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert "<agent>.json" in result.state_file


# ---------------------------------------------------------------------------
# Explicit None block → treated as empty dict (defaults restored)
# ---------------------------------------------------------------------------


def test_explicit_none_block_yields_default_strategy():
    # Arrange
    spec = {"context_management": None}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.strategy == "noop"


# ---------------------------------------------------------------------------
# strategy validation against closed set
# ---------------------------------------------------------------------------


def test_invalid_strategy_falls_back_to_noop():
    # Arrange
    spec = {"context_management": {"strategy": "explode"}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.strategy == "noop"


@pytest.mark.parametrize("strategy", ["compact", "restart", "noop"])
def test_valid_strategy_value_is_preserved(strategy):
    # Arrange
    spec = {"context_management": {"strategy": strategy}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.strategy == strategy


# ---------------------------------------------------------------------------
# trigger_at_percent coercion
# ---------------------------------------------------------------------------


def test_trigger_at_percent_unparsable_string_falls_back_to_default():
    # Arrange
    spec = {"context_management": {"trigger_at_percent": "abc"}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.trigger_at_percent == 70.0


def test_trigger_at_percent_numeric_string_is_coerced_to_float():
    # Arrange
    spec = {"context_management": {"trigger_at_percent": "85.5"}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.trigger_at_percent == 85.5


# ---------------------------------------------------------------------------
# warn_before_n_checks clamping and coercion
# ---------------------------------------------------------------------------


def test_warn_before_n_checks_negative_value_is_clamped_to_zero():
    # Arrange
    spec = {"context_management": {"warn_before_n_checks": -5}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.warn_before_n_checks == 0


def test_warn_before_n_checks_unparsable_string_falls_back_to_zero():
    # Arrange
    spec = {"context_management": {"warn_before_n_checks": "x"}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.warn_before_n_checks == 0


# ---------------------------------------------------------------------------
# check_interval_seconds clamping and coercion
# ---------------------------------------------------------------------------


def test_check_interval_seconds_zero_is_clamped_to_one_minimum():
    # Arrange
    spec = {"context_management": {"check_interval_seconds": 0}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.check_interval_seconds == 1


def test_check_interval_seconds_unparsable_string_falls_back_to_default():
    # Arrange
    spec = {"context_management": {"check_interval_seconds": "xx"}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.check_interval_seconds == 300


# ---------------------------------------------------------------------------
# state_file pass-through
# ---------------------------------------------------------------------------


def test_state_file_value_is_passed_through_verbatim():
    # Arrange
    spec = {"context_management": {"state_file": "/var/sac/<agent>.json"}}
    # Act
    result = parse_context_management(spec)
    # Assert
    assert result.state_file == "/var/sac/<agent>.json"
