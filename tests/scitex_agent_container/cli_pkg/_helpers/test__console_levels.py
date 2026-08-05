"""Each output style maps to the scitex-logging level its prefix promises."""

from __future__ import annotations

import scitex_logging

from scitex_agent_container.cli_pkg._helpers._console import _STYLE_TO_LEVEL


def test_red_renders_as_erro_not_fail():
    # Arrange: the doctrine names an unambiguous failure ERRO; FAIL is a
    # different level scitex-logging renders with a different prefix.
    expected = scitex_logging.ERROR
    # Act
    actual = _STYLE_TO_LEVEL["red"]
    # Assert
    assert actual == expected


def test_error_is_an_explicit_alias_for_red():
    # Arrange
    expected = scitex_logging.ERROR
    # Act
    actual = _STYLE_TO_LEVEL["error"]
    # Assert
    assert actual == expected


def test_fail_stays_available_for_a_failed_check():
    # Arrange
    expected = scitex_logging.FAIL
    # Act
    actual = _STYLE_TO_LEVEL["fail"]
    # Assert
    assert actual == expected


def test_success_renders_as_succ():
    # Arrange
    expected = scitex_logging.SUCCESS
    # Act
    actual = _STYLE_TO_LEVEL["success"]
    # Assert
    assert actual == expected


def test_warn_pulls_the_eye_at_warning_level():
    # Arrange
    expected = scitex_logging.WARNING
    # Act
    actual = _STYLE_TO_LEVEL["warn"]
    # Assert
    assert actual == expected


def test_info_is_visible_by_default():
    # Arrange
    expected = scitex_logging.INFO
    # Act
    actual = _STYLE_TO_LEVEL["info"]
    # Assert
    assert actual == expected


def test_dim_is_hidden_below_debug_verbosity():
    # Arrange: detail moved off the default path must not print by default.
    expected = scitex_logging.DEBUG
    # Act
    actual = _STYLE_TO_LEVEL["dim"]
    # Assert
    assert actual == expected
