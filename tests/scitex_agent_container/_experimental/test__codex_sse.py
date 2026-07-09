"""Tests for the pure Anthropic-SSE mapping (``_experimental._codex_sse``).

No mocks (PA-306): the SSE mapper is a pure function over a sequence of codex
app-server events, so every test drives it with an in-memory canned delta
sequence — a real test of the mapping logic with zero network. AAA markers
(TQ002), descriptive names (TQ003), one assertion per test (TQ007).

The canned sequences use the exact app-server notification shapes the live
driver (``_codex_driver.iter_codex_events``) yields: ``item/agentMessage/delta``
(with ``params.delta``), ``item/completed`` (agentMessage item ``.text``), then
``turn/completed`` (optionally carrying a real ``usage.output_tokens``).
"""
from __future__ import annotations

import pytest

from scitex_agent_container._experimental._codex_sse import (
    codex_stream_to_anthropic_sse,
    format_sse_frame,
    parse_sse_stream,
)


def _delta(text: str) -> dict:
    return {"method": "item/agentMessage/delta", "params": {"delta": text}}


def _completed(text: str) -> dict:
    return {
        "method": "item/completed",
        "params": {"item": {"type": "agentMessage", "text": text}},
    }


def _turn_completed(usage: dict | None = None) -> dict:
    params = {"usage": usage} if usage is not None else {}
    return {"method": "turn/completed", "params": params}


@pytest.fixture
def two_chunk_stream() -> list[dict]:
    """A canned codex stream: 'po' + 'ng' deltas, completed item, turn done."""
    return [
        _delta("po"),
        _delta("ng"),
        _completed("pong"),
        _turn_completed(),
    ]


class TestEventOrder:
    def test_emits_canonical_anthropic_event_sequence(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        order = [etype for etype, _ in events]
        # Assert
        assert order == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

    def test_one_content_block_delta_per_codex_delta(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        deltas = [e for e in events if e[0] == "content_block_delta"]
        # Assert
        assert len(deltas) == 2

    def test_first_event_is_message_start(self, two_chunk_stream):
        # Arrange
        stream = codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        # Act
        first_type = next(iter(stream))[0]
        # Assert
        assert first_type == "message_start"


class TestReassembledText:
    def test_content_block_deltas_reassemble_to_full_reply(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        text = "".join(
            data["delta"]["text"]
            for etype, data in events
            if etype == "content_block_delta"
        )
        # Assert
        assert text == "pong"

    def test_content_block_delta_uses_text_delta_type(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        first_delta = next(
            data for etype, data in events if etype == "content_block_delta"
        )
        # Assert
        assert first_delta["delta"]["type"] == "text_delta"


class TestMessageEnvelope:
    def test_message_start_carries_assistant_role(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        start = next(d for t, d in events if t == "message_start")
        # Assert
        assert start["message"]["role"] == "assistant"

    def test_message_start_reports_requested_model(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        start = next(d for t, d in events if t == "message_start")
        # Assert
        assert start["message"]["model"] == "gpt-5.5"

    def test_message_start_has_empty_content(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        start = next(d for t, d in events if t == "message_start")
        # Assert
        assert start["message"]["content"] == []

    def test_message_delta_stop_reason_is_end_turn(self, two_chunk_stream):
        # Arrange
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        delta = next(d for t, d in events if t == "message_delta")
        # Assert
        assert delta["delta"]["stop_reason"] == "end_turn"


class TestUsageAccounting:
    def test_real_output_tokens_from_turn_completed_are_used(self):
        # Arrange
        stream = [_delta("pong"), _turn_completed({"output_tokens": 42})]
        # Act
        events = list(codex_stream_to_anthropic_sse(stream, model="gpt-5.5"))
        delta = next(d for t, d in events if t == "message_delta")
        # Assert
        assert delta["usage"]["output_tokens"] == 42

    def test_output_tokens_estimated_when_codex_reports_none(self):
        # Arrange
        stream = [_delta("hello world"), _turn_completed()]
        # Act
        events = list(codex_stream_to_anthropic_sse(stream, model="gpt-5.5"))
        delta = next(d for t, d in events if t == "message_delta")
        # Assert
        assert delta["usage"]["output_tokens"] >= 1


class TestCompletedFallback:
    def test_completed_item_text_emitted_when_no_deltas_streamed(self):
        # Arrange — codex that did not stream deltas, only a completed item
        stream = [_completed("pong"), _turn_completed()]
        # Act
        events = list(codex_stream_to_anthropic_sse(stream, model="gpt-5.5"))
        text = "".join(
            d["delta"]["text"] for t, d in events if t == "content_block_delta"
        )
        # Assert
        assert text == "pong"

    def test_completed_item_not_double_emitted_after_deltas(self, two_chunk_stream):
        # Arrange — deltas already streamed the text; completed carries the full
        # text too, which must NOT be re-emitted as an extra delta.
        events = list(
            codex_stream_to_anthropic_sse(two_chunk_stream, model="gpt-5.5")
        )
        # Act
        text = "".join(
            d["delta"]["text"] for t, d in events if t == "content_block_delta"
        )
        # Assert
        assert text == "pong"


class TestFrameSerialisation:
    def test_frame_has_event_and_data_lines(self):
        # Arrange
        event_type, data = "message_stop", {"type": "message_stop"}
        # Act
        frame = format_sse_frame(event_type, data)
        # Assert
        assert frame == 'event: message_stop\ndata: {"type": "message_stop"}\n\n'

    def test_serialise_then_parse_roundtrips_event_order(self, two_chunk_stream):
        # Arrange
        wire = "".join(
            format_sse_frame(t, d)
            for t, d in codex_stream_to_anthropic_sse(
                two_chunk_stream, model="gpt-5.5"
            )
        )
        # Act
        parsed = [etype for etype, _ in parse_sse_stream(wire)]
        # Assert
        assert parsed == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

    def test_serialise_then_parse_roundtrips_reply_text(self, two_chunk_stream):
        # Arrange
        wire = "".join(
            format_sse_frame(t, d)
            for t, d in codex_stream_to_anthropic_sse(
                two_chunk_stream, model="gpt-5.5"
            )
        )
        # Act
        text = "".join(
            data["delta"]["text"]
            for etype, data in parse_sse_stream(wire)
            if etype == "content_block_delta"
        )
        # Assert
        assert text == "pong"
