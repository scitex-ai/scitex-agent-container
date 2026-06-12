"""Tests for ``_lifecycle/_runtime_select._get_runtime``.

Operator directive 12870 (lead a2a ``b58dd5d3b4d640d2a7f31f16c710e839``):
``spec.runtime`` is repurposed from container-engine selector to
LAUNCH-MODE selector. ``claude-agent-sdk`` (default) → the SDK runner;
``tui`` → the new tmux-backed TUI runner. Legacy ``apptainer`` / ``""``
values are accepted for back-compat and map to ``claude-agent-sdk``
with a one-line deprecation log.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — uses a tiny stub
``SimpleNamespace`` config (the existing test pattern for
``_get_runtime`` callers; the runtime selector reads ONE attribute).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle._runtime_select import (
    _get_runtime,
    warn_if_legacy_apptainer_runtime,
)
from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime
from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

# ---------------------------------------------------------------------------
# claude-agent-sdk (the canonical default)
# ---------------------------------------------------------------------------


def test_get_runtime_returns_claude_session_for_claude_agent_sdk():
    # Arrange
    config = SimpleNamespace(name="alpha", runtime="claude-agent-sdk")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, ClaudeSessionRuntime)


def test_get_runtime_returns_claude_session_for_empty_runtime():
    # Arrange — historical default; existing specs may omit the field.
    config = SimpleNamespace(name="alpha", runtime="")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, ClaudeSessionRuntime)


# ---------------------------------------------------------------------------
# tui (June-15 SDK-pool-cutoff pivot)
# ---------------------------------------------------------------------------


def test_get_runtime_returns_tui_session_for_runtime_tui():
    # Arrange
    config = SimpleNamespace(name="alpha", runtime="tui")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, TuiSessionRuntime)


# ---------------------------------------------------------------------------
# apptainer back-compat — maps to claude-agent-sdk + deprecation log
# ---------------------------------------------------------------------------


def test_get_runtime_maps_apptainer_to_claude_session() -> None:
    # Arrange
    config = SimpleNamespace(name="alpha", runtime="apptainer")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, ClaudeSessionRuntime)


def test_get_runtime_does_not_emit_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — the deprecation log lives on the START path now (lead a2a
    # f468a6d2, fix-pattern #364 v3) so status / list / discovery walks
    # that hit ``_get_runtime`` don't contaminate CLI output streams.
    config = SimpleNamespace(name="alpha", runtime="apptainer")
    caplog.set_level(logging.WARNING)
    # Act
    _get_runtime(config)
    # Assert
    deprecation_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "deprecated" in r.getMessage()
    ]
    assert deprecation_warnings == []


def test_warn_if_legacy_apptainer_runtime_fires_for_apptainer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    config = SimpleNamespace(name="alpha", runtime="apptainer")
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_apptainer_runtime(config)
    # Assert
    deprecation_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "deprecated" in r.getMessage()
    ]
    assert len(deprecation_warnings) >= 1


def test_warn_if_legacy_apptainer_runtime_names_the_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    config = SimpleNamespace(name="my-research-agent", runtime="apptainer")
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_apptainer_runtime(config)
    # Assert
    deprecation_messages = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )
    assert "my-research-agent" in deprecation_messages


def test_warn_if_legacy_apptainer_runtime_silent_for_empty_runtime(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — empty is the historical default, not a deprecation case.
    config = SimpleNamespace(name="alpha", runtime="")
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_apptainer_runtime(config)
    # Assert
    deprecation_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "deprecated" in r.getMessage()
    ]
    assert deprecation_warnings == []


def test_warn_if_legacy_apptainer_runtime_silent_for_claude_agent_sdk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    config = SimpleNamespace(name="alpha", runtime="claude-agent-sdk")
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_apptainer_runtime(config)
    # Assert
    deprecation_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "deprecated" in r.getMessage()
    ]
    assert deprecation_warnings == []


# ---------------------------------------------------------------------------
# Unknown runtime — loud rejection
# ---------------------------------------------------------------------------


def test_get_runtime_rejects_unknown_value_with_value_error():
    # Arrange
    config = SimpleNamespace(name="alpha", runtime="claude-xtreme")
    raised: BaseException | None = None
    # Act
    try:
        _get_runtime(config)
    except ValueError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; selector contract is to raise ValueError on unknown.)
        raised = exc
    # Assert
    assert isinstance(raised, ValueError)


def test_get_runtime_rejects_unknown_value_names_accepted_set():
    # Arrange
    config = SimpleNamespace(name="alpha", runtime="claude-xtreme")
    raised: BaseException | None = None
    # Act
    try:
        _get_runtime(config)
    except ValueError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert raised is not None and "claude-agent-sdk" in str(raised)
