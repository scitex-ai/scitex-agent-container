"""Tests for the Day-2 (E) runtime dispatcher.

``_get_runtime`` branches on ``config.claude.runtime`` so callers in
``_start.py`` / ``_stop.py`` don't have to know about the SDK vs tmux
split. The dispatcher is a simple if/elif; the test pins the wire-up
so a typo or accidental removal surfaces immediately.

Uses the simple Protocol seam: each test passes a SimpleNamespace
mimicking ``AgentConfig`` instead of a fully-baked one, because the
dispatcher only reads two attributes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle._runtime_select import _get_runtime


def _config(*, claude_runtime: str | None = None, runtime: str = "apptainer"):
    """Build a minimal config-like object for the dispatcher."""
    claude = SimpleNamespace(runtime=claude_runtime) if claude_runtime else None
    if claude is None:
        # Default ClaudeSpec carries runtime="sdk"; mirror that.
        claude = SimpleNamespace(runtime="sdk")
    return SimpleNamespace(runtime=runtime, claude=claude)


def test_default_runtime_returns_sdk_runtime_instance():
    # Arrange
    config = _config()
    # Act
    runtime = _get_runtime(config)
    # Assert
    assert type(runtime).__name__ == "ClaudeSessionRuntime"


def test_explicit_sdk_runtime_returns_sdk_runtime_instance():
    # Arrange
    config = _config(claude_runtime="sdk")
    # Act
    runtime = _get_runtime(config)
    # Assert
    assert type(runtime).__name__ == "ClaudeSessionRuntime"


def test_tmux_runtime_returns_tmux_runtime_instance():
    # Arrange
    config = _config(claude_runtime="tmux")
    # Act
    runtime = _get_runtime(config)
    # Assert
    assert type(runtime).__name__ == "ClaudeCodeRuntime"


def test_tmux_runtime_returns_distinct_class_from_sdk_runtime():
    """The two paths must yield DIFFERENT runtime classes, not the same one."""
    # Arrange
    sdk_config = _config(claude_runtime="sdk")
    tmux_config = _config(claude_runtime="tmux")
    # Act
    sdk_rt = _get_runtime(sdk_config)
    tmux_rt = _get_runtime(tmux_config)
    # Assert
    assert type(sdk_rt) is not type(tmux_rt)


def test_unsupported_container_runtime_raises_when_sdk_path_selected():
    """If claude.runtime='sdk' but spec.runtime is unknown, ValueError."""
    # Arrange
    config = _config(claude_runtime="sdk", runtime="docker")
    # Act
    raised: Exception | None = None
    try:
        _get_runtime(config)
    except ValueError as exc:
        raised = exc
    # Assert
    assert raised is not None and "Unsupported runtime" in str(raised)


def test_tmux_runtime_bypasses_unsupported_container_runtime_check():
    """The tmux path is checked BEFORE the SDK's container guard.

    Operators on a non-apptainer host can still run the tmux driver
    because it doesn't depend on the apptainer engine. This must NOT
    raise the SDK-path "Unsupported runtime" diagnostic.
    """
    # Arrange
    config = _config(claude_runtime="tmux", runtime="docker")
    # Act
    runtime = _get_runtime(config)
    # Assert
    assert type(runtime).__name__ == "ClaudeCodeRuntime"
