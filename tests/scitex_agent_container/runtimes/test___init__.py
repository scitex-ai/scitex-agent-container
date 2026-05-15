"""Tests for the ``runtimes`` package ``__getattr__`` (PEP 562).

Covers the lazy ``ClaudeSessionRuntime`` lookup branch + the unknown-attr
``AttributeError`` raise. No mocks: just imports the real package and
calls ``__getattr__`` (also reached implicitly by attribute access).
"""

from __future__ import annotations

import scitex_agent_container.runtimes as runtimes_pkg


def test_lazy_attribute_resolves_claude_session_runtime():
    # Arrange
    expected_name = "ClaudeSessionRuntime"
    # Act
    resolved = runtimes_pkg.ClaudeSessionRuntime
    # Assert
    assert resolved.__name__ == expected_name


def test_unknown_attribute_raises_attribute_error():
    # Arrange
    bogus = "not_a_real_runtime_xxx"
    # Act
    caught: Exception | None = None
    try:
        runtimes_pkg.__getattr__(bogus)
    except AttributeError as exc:
        caught = exc
    # Assert
    assert isinstance(caught, AttributeError)
