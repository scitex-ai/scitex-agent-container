"""Tests for the codex -> Anthropic Messages adapter translation + streaming.

No mocks (PA-306): the request-flattening and response-building translation are
pure functions, and the streaming pipeline is driven end-to-end with a canned
in-memory codex event sequence (no ``codex`` subprocess, no network). AAA
markers (TQ002), descriptive names (TQ003), one assertion per test (TQ007).

The live HTTP round-trip (spawning a real ``codex app-server``) is exercised by
the module's ``smoke`` / ``stream-smoke`` entry points, not from unit tests.
"""
from __future__ import annotations

from scitex_agent_container._experimental import codex_anthropic_adapter as adapter
from scitex_agent_container._experimental._codex_sse import (
    codex_stream_to_anthropic_sse,
    format_sse_frame,
    parse_sse_stream,
)


class TestFlattenRequest:
    def test_user_message_is_role_tagged(self):
        # Arrange
        body = {"messages": [{"role": "user", "content": "hi"}]}
        # Act
        prompt = adapter.flatten_request(body)
        # Assert
        assert "User: hi" in prompt

    def test_system_prompt_is_prepended(self):
        # Arrange
        body = {"system": "be terse", "messages": [{"role": "user", "content": "hi"}]}
        # Act
        prompt = adapter.flatten_request(body)
        # Assert
        assert prompt.startswith("System: be terse")

    def test_transcript_ends_with_assistant_cue(self):
        # Arrange
        body = {"messages": [{"role": "user", "content": "hi"}]}
        # Act
        prompt = adapter.flatten_request(body)
        # Assert
        assert prompt.rstrip().endswith("Assistant:")

    def test_content_block_list_is_flattened_to_text(self):
        # Arrange
        body = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "blockish"}]}
            ]
        }
        # Act
        prompt = adapter.flatten_request(body)
        # Assert
        assert "blockish" in prompt


class TestBuildAnthropicResponse:
    def test_wraps_reply_as_text_content_block(self):
        # Arrange
        reply = "pong"
        # Act
        resp = adapter.build_anthropic_response(reply, model="gpt-5.5", prompt="ping")
        # Assert
        assert resp["content"] == [{"type": "text", "text": "pong"}]

    def test_stop_reason_is_end_turn(self):
        # Arrange
        reply = "pong"
        # Act
        resp = adapter.build_anthropic_response(reply, model="gpt-5.5", prompt="ping")
        # Assert
        assert resp["stop_reason"] == "end_turn"


class TestStreamingPipeline:
    def test_stream_true_maps_to_ordered_sse_wire(self):
        # Arrange — a canned codex event stream flattened through the SSE mapper
        codex_events = [
            {"method": "item/agentMessage/delta", "params": {"delta": "po"}},
            {"method": "item/agentMessage/delta", "params": {"delta": "ng"}},
            {"method": "turn/completed", "params": {}},
        ]
        # Act
        wire = "".join(
            format_sse_frame(t, d)
            for t, d in codex_stream_to_anthropic_sse(codex_events, model="gpt-5.5")
        )
        parsed = [etype for etype, _ in parse_sse_stream(wire)]
        # Assert
        assert parsed[0] == "message_start" and parsed[-1] == "message_stop"

    def test_stream_reassembles_reply_text(self):
        # Arrange
        codex_events = [
            {"method": "item/agentMessage/delta", "params": {"delta": "po"}},
            {"method": "item/agentMessage/delta", "params": {"delta": "ng"}},
            {"method": "turn/completed", "params": {}},
        ]
        # Act
        wire = "".join(
            format_sse_frame(t, d)
            for t, d in codex_stream_to_anthropic_sse(codex_events, model="gpt-5.5")
        )
        text = "".join(
            data["delta"]["text"]
            for etype, data in parse_sse_stream(wire)
            if etype == "content_block_delta"
        )
        # Assert
        assert text == "pong"


class TestPublicApiReexports:
    def test_run_codex_turn_is_reexported(self):
        # Arrange
        name = "run_codex_turn"
        # Act
        has_it = hasattr(adapter, name)
        # Assert
        assert has_it is True

    def test_codex_error_is_reexported(self):
        # Arrange
        name = "CodexError"
        # Act
        has_it = hasattr(adapter, name)
        # Assert
        assert has_it is True
