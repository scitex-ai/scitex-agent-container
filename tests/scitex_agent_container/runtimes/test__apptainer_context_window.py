#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""THE CONTEXT-WINDOW ENV COMES FROM THE HARNESS, NOT FROM A VENDOR TEST.

The launch path used to decide which context-window variable to emit by
asking "is this the DEFAULT harness?" — so an engine's declared
``max_context_tokens`` reached one program's own knob and silently
reached nobody else's. That is a privilege granted by an ``if``, not by
a measurement, and it is one of the three the harness/engine split
removes.

These tests assert the REPLACEMENT is genuinely a lookup: the answer for
each harness equals that harness's declared ``context_window_env``
column, and a harness that declares ``None`` gets nothing — an explicit
answer, not an omission.
"""

from __future__ import annotations

from scitex_agent_container.config._harness_lookup import context_window_env_for
from scitex_agent_container.config._harness_registry import (
    CLAUDE_AGENT_SDK,
    CLAUDE_CODE_TUI,
    CODEX_SDK,
    CODEX_TUI,
    HARNESS_DESCRIPTORS,
    OPENAI_AGENTS,
)
from scitex_agent_container.runtimes._apptainer_context_window import (
    context_window_env,
)


class _Config:
    """Only the field the resolver reads — the launch mode."""

    def __init__(self, runtime: str) -> None:
        self.runtime = runtime


def test_the_claude_tui_declares_its_own_context_window_variable():
    # Arrange
    key = CLAUDE_CODE_TUI
    # Act
    name = context_window_env_for(key)
    # Assert
    assert name == "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def test_the_claude_sdk_declares_the_same_variable():
    # Arrange
    key = CLAUDE_AGENT_SDK
    # Act
    name = context_window_env_for(key)
    # Assert
    assert name == "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def test_the_codex_tui_declares_no_env_variable():
    """Codex takes its window as ``-c model_context_window`` on the argv;
    ``None`` says so, rather than leaving the question unasked."""
    # Arrange
    key = CODEX_TUI
    # Act
    name = context_window_env_for(key)
    # Assert
    assert name is None


def test_the_codex_sdk_declares_no_env_variable():
    # Arrange
    key = CODEX_SDK
    # Act
    name = context_window_env_for(key)
    # Assert
    assert name is None


def test_the_openai_agents_harness_declares_no_env_variable():
    # Arrange
    key = OPENAI_AGENTS
    # Act
    name = context_window_env_for(key)
    # Assert
    assert name is None


def test_an_unknown_registry_key_answers_none_rather_than_guessing():
    # Arrange
    key = "no-such-harness"
    # Act
    name = context_window_env_for(key)
    # Assert
    assert name is None


def test_the_launch_resolves_the_claude_tui_variable_for_an_anthropic_agent():
    # Arrange
    config = _Config("tui")
    # Act
    name = context_window_env(config, "anthropic")
    # Assert
    assert name == "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def test_the_launch_resolves_nothing_for_a_codex_agent():
    # Arrange
    config = _Config("tui")
    # Act
    name = context_window_env(config, "codex")
    # Assert
    assert name is None


def test_an_unmappable_harness_runtime_pair_answers_none_not_a_vendor_default():
    """The refusal for an unmappable pair belongs to the launch guard.
    Manufacturing a vendor's variable here would be a guess dressed as a
    default."""
    # Arrange
    config = _Config("claude-agent-sdk")
    # Act
    name = context_window_env(config, "codex")
    # Assert
    assert name is None


def test_every_registry_entry_answers_the_context_window_question():
    """A column, not a special case: adding a harness cannot leave the
    question unasked, because the descriptor field has no None-means-
    unknown state — ``None`` means "takes no env var"."""
    # Arrange
    answered = {
        key: hasattr(descriptor, "context_window_env")
        for key, descriptor in HARNESS_DESCRIPTORS.items()
    }
    # Act
    unanswered = [key for key, ok in answered.items() if not ok]
    # Assert
    assert unanswered == []
