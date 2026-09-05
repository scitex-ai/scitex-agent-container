"""Tests for ``_lifecycle/_runtime_select._get_runtime``.

Operator directive 12870 (lead a2a ``b58dd5d3b4d640d2a7f31f16c710e839``):
``spec.runtime`` is repurposed from container-engine selector to
LAUNCH-MODE selector. ``tui`` (the DEFAULT since 2026-06-15; ``""`` /
unset maps here) → the in-apptainer TUI runner; ``claude-agent-sdk`` →
the headless SDK runner. Legacy ``apptainer`` maps to ``claude-agent-sdk``
with a one-line deprecation log.

v4 step-2 loudness (card
``sac-v4-layering-refactor-harness-runtime-inference-20260813``): every
runtime ``_get_runtime`` can return launches the CLAUDE harness, so a
non-Anthropic ``config.harness`` raises ``HarnessRuntimeMismatchError``
instead of silently falling through to the Claude/TUI runner. (The old
``spec.provider: openai -> OpenAISessionRuntime`` tests pinned a DEAD
branch: they stubbed a ``provider`` attribute that the harness rename
removed from the real ``AgentConfig``, so production configs never took
it — verified 2026-08-14, ``hasattr(AgentConfig("x"), "provider")`` is
False.)

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — a tiny stub
``SimpleNamespace`` config for the single-attribute selector paths, and
the REAL ``AgentConfig`` for the harness-refusal contract (the stub is
exactly what masked the dead branch).
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
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._harness_types import (
    V4_HARNESS_DISPATCH_CARD,
    HarnessRuntimeMismatchError,
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
# Non-Anthropic harness — LOUD refusal, never a silent Claude launch
# (v4 step 2; real AgentConfig on purpose: the old tests stubbed a
# ``provider`` attribute the harness rename removed, pinning a dead branch)
# ---------------------------------------------------------------------------


def test_get_runtime_never_hands_an_openai_harness_the_claude_runner():
    # Arrange — the pre-fix bug verbatim: harness: openai silently got
    # TuiSessionRuntime because the dead ``config.provider`` read fell
    # through. Whatever else happens (today: a refusal raise; step 4: a
    # real OpenAI dispatch), the Claude-family runner must never come back.
    config = AgentConfig(name="beta", runtime="tui", harness="openai")
    rt: object | None = None
    # Act
    try:
        rt = _get_runtime(config)
    except Exception:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; a raise is a PASS for this pin — only a returned Claude runner fails it.)
        pass
    # Assert
    assert not isinstance(rt, (TuiSessionRuntime, ClaudeSessionRuntime))


def test_get_runtime_raises_harness_mismatch_for_openai_harness():
    # Arrange
    config = AgentConfig(name="beta", runtime="tui", harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        _get_runtime(config)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert isinstance(raised, HarnessRuntimeMismatchError)


def test_get_runtime_openai_harness_refusal_names_what_the_spec_asked():
    # Arrange
    config = AgentConfig(name="beta", runtime="tui", harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        _get_runtime(config)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert raised is not None and "harness='openai'" in str(raised)


def test_get_runtime_openai_harness_refusal_names_what_would_launch():
    # Arrange — runtime: tui, so the wrong-vendor launch it refuses is
    # the interactive Claude TUI.
    config = AgentConfig(name="beta", runtime="tui", harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        _get_runtime(config)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert raised is not None and "TuiSessionRuntime" in str(raised)


def test_get_runtime_openai_harness_refusal_names_the_decision_site():
    # Arrange
    config = AgentConfig(name="beta", runtime="claude-agent-sdk", harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        _get_runtime(config)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert — file:line of the decision, so the reader lands on the guard.
    assert raised is not None and "_runtime_select.py:" in str(raised)


def test_get_runtime_openai_harness_refusal_names_the_v4_card():
    # Arrange
    config = AgentConfig(name="beta", runtime="tui", harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        _get_runtime(config)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert raised is not None and V4_HARNESS_DISPATCH_CARD in str(raised)


# ---------------------------------------------------------------------------
# Default (anthropic harness) — unchanged behaviour, byte-identical selection
# ---------------------------------------------------------------------------


def test_get_runtime_returns_tui_session_for_default_harness():
    # Arrange — default harness is "anthropic" (DEFAULT_AGENT_HARNESS);
    # with no harness stated and runtime="", we still get TUI.
    config = SimpleNamespace(name="epsilon")  # runtime defaults to "", harness defaults to anthropic
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, TuiSessionRuntime)


def test_get_runtime_returns_claude_session_for_anthropic_harness():
    # Arrange — a REAL config with the explicit canonical value.
    config = AgentConfig(name="zeta", runtime="claude-agent-sdk", harness="anthropic")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, ClaudeSessionRuntime)


# ---------------------------------------------------------------------------
# codex-tui (2026-09-05): the selector maps the codex pane to the TUI runtime
# ---------------------------------------------------------------------------


def test_get_runtime_returns_tui_session_for_harness_codex():
    # Arrange -- a spec that only flipped harness: anthropic -> codex.
    config = AgentConfig(name="hm", runtime="", workdir="/tmp/hm", harness="codex")
    # Act
    rt = _get_runtime(config)
    # Assert
    assert isinstance(rt, TuiSessionRuntime)


def test_get_runtime_still_refuses_the_headless_codex_runner():
    # Arrange -- registered but without a lifecycle adapter, the headless
    # runner claims no runtime spelling, so its name is unmappable: loud.
    config = AgentConfig(
        name="hm", runtime="codex-sdk", workdir="/tmp/hm", harness="codex"
    )
    # Act
    try:
        _get_runtime(config)
        message = ""
    except ValueError as exc:
        message = str(exc)
    # Assert
    assert "codex-sdk" in message


def test_get_runtime_still_refuses_the_openai_harness():
    # Arrange -- the vendor guard is untouched for every other family.
    config = AgentConfig(name="oa", runtime="", workdir="/tmp/oa", harness="openai")
    # Act
    try:
        _get_runtime(config)
        raised = None
    except HarnessRuntimeMismatchError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, HarnessRuntimeMismatchError)
