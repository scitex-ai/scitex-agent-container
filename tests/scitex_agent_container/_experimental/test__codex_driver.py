"""Tests for the codex app-server driver helpers (``_experimental._codex_driver``).

No mocks (PA-306): the per-event extraction helpers are pure functions over the
app-server notification dicts, so each test drives them with a real in-memory
event dict — no ``codex`` subprocess, no network. AAA markers (TQ002),
descriptive names (TQ003), one assertion per test (TQ007).

The live ``iter_codex_events`` / ``run_codex_turn`` paths spawn a real
``codex app-server`` and are exercised by the module's ``stream-smoke`` / ``smoke``
entry points against the operator's ``~/.codex`` login, not from unit tests.
"""
from __future__ import annotations

from scitex_agent_container._experimental._codex_driver import (
    _estimate_tokens,
    _event_completed_text,
    _event_delta_text,
    _event_output_tokens,
)


class TestEventDeltaText:
    def test_reads_delta_field(self):
        # Arrange
        msg = {"method": "item/agentMessage/delta", "params": {"delta": "po"}}
        # Act
        text = _event_delta_text(msg)
        # Assert
        assert text == "po"

    def test_falls_back_to_text_field(self):
        # Arrange
        msg = {"method": "item/agentMessage/delta", "params": {"text": "ng"}}
        # Act
        text = _event_delta_text(msg)
        # Assert
        assert text == "ng"

    def test_missing_delta_yields_empty_string(self):
        # Arrange
        msg = {"method": "item/agentMessage/delta", "params": {}}
        # Act
        text = _event_delta_text(msg)
        # Assert
        assert text == ""


class TestEventCompletedText:
    def test_extracts_agent_message_text(self):
        # Arrange
        msg = {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "pong"}},
        }
        # Act
        text = _event_completed_text(msg)
        # Assert
        assert text == "pong"

    def test_ignores_non_agent_message_item(self):
        # Arrange
        msg = {
            "method": "item/completed",
            "params": {"item": {"type": "reasoning", "text": "..."}},
        }
        # Act
        text = _event_completed_text(msg)
        # Assert
        assert text is None


class TestEventOutputTokens:
    def test_reads_usage_output_tokens(self):
        # Arrange
        msg = {"method": "turn/completed", "params": {"usage": {"output_tokens": 7}}}
        # Act
        tokens = _event_output_tokens(msg)
        # Assert
        assert tokens == 7

    def test_returns_none_when_usage_absent(self):
        # Arrange
        msg = {"method": "turn/completed", "params": {}}
        # Act
        tokens = _event_output_tokens(msg)
        # Assert
        assert tokens is None


class TestEstimateTokens:
    def test_empty_text_is_zero_tokens(self):
        # Arrange
        text = ""
        # Act
        tokens = _estimate_tokens(text)
        # Assert
        assert tokens == 0

    def test_non_empty_text_is_at_least_one_token(self):
        # Arrange
        text = "hello world"
        # Act
        tokens = _estimate_tokens(text)
        # Assert
        assert tokens >= 1
