"""Tests for ``_lifecycle/_runtime_select._get_runtime``.

Operator directive 12870 (lead a2a ``b58dd5d3b4d640d2a7f31f16c710e839``):
``spec.runtime`` is repurposed from container-engine selector to
LAUNCH-MODE selector. ``tui`` (the DEFAULT since 2026-06-15; ``""`` /
unset maps here) → the in-apptainer TUI runner; ``claude-agent-sdk`` →
the headless SDK runner. Legacy ``apptainer`` maps to ``claude-agent-sdk``
with a one-line deprecation log.

scitex-todo card ``openai-compat-2``: when ``spec.provider: openai``,
``_get_runtime`` returns ``OpenAISessionRuntime`` regardless of
``spec.runtime``.

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
    warn_if_legacy_harness_key,
)
from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime
from scitex_agent_container.runtimes.openai_session import OpenAISessionRuntime
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


def test_get_runtime_returns_tui_session_for_empty_runtime():
    # Arrange — empty / unset is the DEFAULT and now maps to TUI.
    config = SimpleNamespace(name="alpha", runtime="")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, TuiSessionRuntime)


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
# Legacy spec.provider key — the harness axis' deprecated spelling.
# Placed on the START path for the same reason as the runtime deprecation
# above: a load-time warning would fire once per spec on every list walk.
# ---------------------------------------------------------------------------


def test_warn_if_legacy_harness_key_fires_for_a_legacy_spec(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    config = SimpleNamespace(
        name="alpha", harness="anthropic", harness_key_is_legacy=True
    )
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_harness_key(config)
    # Assert
    deprecations = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "DEPRECATED" in r.getMessage()
    ]
    assert len(deprecations) >= 1


def test_warn_if_legacy_harness_key_names_the_old_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    config = SimpleNamespace(
        name="alpha", harness="anthropic", harness_key_is_legacy=True
    )
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_harness_key(config)
    # Assert
    messages = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )
    assert "spec.provider" in messages


def test_warn_if_legacy_harness_key_names_the_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    config = SimpleNamespace(
        name="my-research-agent", harness="anthropic", harness_key_is_legacy=True
    )
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_harness_key(config)
    # Assert
    messages = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )
    assert "my-research-agent" in messages


def test_warn_if_legacy_harness_key_silent_for_the_canonical_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    config = SimpleNamespace(
        name="alpha", harness="anthropic", harness_key_is_legacy=False
    )
    caplog.set_level(logging.WARNING)
    # Act
    warn_if_legacy_harness_key(config)
    # Assert
    deprecations = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "DEPRECATED" in r.getMessage()
    ]
    assert deprecations == []


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


# ---------------------------------------------------------------------------
# OpenAI provider — dispatches OpenAISessionRuntime (openai-compat-2)
# ---------------------------------------------------------------------------


def test_get_runtime_returns_openai_session_for_openai_provider():
    # Arrange — spec.provider: openai selects the OpenAI SDK path
    # regardless of spec.runtime.
    config = SimpleNamespace(name="beta", runtime="", provider="openai")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, OpenAISessionRuntime)


def test_get_runtime_returns_openai_session_even_with_claude_runtime():
    # Arrange — provider: openai WINS over runtime: claude-agent-sdk.
    config = SimpleNamespace(name="gamma", runtime="claude-agent-sdk", provider="openai")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, OpenAISessionRuntime)


def test_get_runtime_returns_openai_session_even_with_apptainer_runtime():
    # Arrange — provider: openai WINS over runtime: apptainer (back-compat).
    config = SimpleNamespace(name="delta", runtime="apptainer", provider="openai")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, OpenAISessionRuntime)


# ---------------------------------------------------------------------------
# Default (anthropic provider) — unchanged behaviour
# ---------------------------------------------------------------------------


def test_get_runtime_returns_tui_session_for_default_provider():
    # Arrange — default provider is "anthropic" (DEFAULT_AGENT_PROVIDER);
    # with no provider set and runtime="", we still get TUI.
    config = SimpleNamespace(name="epsilon")  # runtime defaults to "", provider defaults to None
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, TuiSessionRuntime)


def test_get_runtime_returns_claude_session_for_anthropic_provider():
    # Arrange — explicit provider: anthropic with runtime: claude-agent-sdk.
    config = SimpleNamespace(name="zeta", runtime="claude-agent-sdk", provider="anthropic")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, ClaudeSessionRuntime)
