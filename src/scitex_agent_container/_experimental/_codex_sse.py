#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Anthropic Server-Sent-Events mapping for the codex adapter (experimental).

Pure translation layer: given a sequence of ``codex app-server`` notification
dicts (the same objects :func:`.._codex_driver.iter_codex_events` yields), emit
the ordered Anthropic Messages *streaming* event sequence

    message_start
    content_block_start        (index 0, type text)
    content_block_delta x N    (type text_delta — ONE per codex delta chunk)
    content_block_stop
    message_delta              (stop_reason=end_turn + final usage)
    message_stop

Deliberately network-free: :func:`codex_stream_to_anthropic_sse` is a generator
over whatever iterable of codex events it is handed. The live path feeds it the
real (lazy) ``iter_codex_events`` generator so frames stream in real time; a
unit test feeds it a canned in-memory delta sequence and asserts the mapping —
a real test of the translation with no mocked network.

See the Anthropic Messages streaming spec for the frame wire format
(``event: <type>\\ndata: <json>\\n\\n``).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Iterator

from ._codex_driver import (
    _estimate_tokens,
    _event_completed_text,
    _event_delta_text,
    _event_output_tokens,
)


def format_sse_frame(event_type: str, data: dict[str, Any]) -> str:
    """Serialise one Anthropic SSE frame: ``event: <type>\\ndata: <json>\\n\\n``."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def parse_sse_stream(text: str) -> list[tuple[str, Any]]:
    """Parse concatenated SSE frames back into ``(event_type, data)`` pairs.

    Inverse of :func:`format_sse_frame`, used by the streaming smoke test and
    unit tests to assert the emitted frame sequence. Blank-line-separated
    frames; each frame carries an ``event:`` line and a JSON ``data:`` line.
    """
    events: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event_type: str | None = None
        data: Any = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        if event_type is not None:
            events.append((event_type, data))
    return events


def codex_stream_to_anthropic_sse(
    codex_events: Iterable[dict[str, Any]],
    *,
    model: str,
    prompt: str = "",
    message_id: str | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Map codex app-server events to ordered Anthropic streaming events.

    Yields ``(event_type, data)`` tuples in the canonical Anthropic order. The
    caller serialises each with :func:`format_sse_frame`. ``codex_events`` may
    be a live generator (real-time streaming) or a canned list (unit testing);
    the mapping logic is identical either way.

    Mapping rules:
    * ``item/agentMessage/delta`` -> one ``content_block_delta`` (text_delta).
    * ``item/completed`` with agent text but no prior deltas -> emit that full
      text as a single ``content_block_delta`` (non-streaming-codex fallback).
    * ``turn/completed`` -> real output-token count if codex reports one, else
      the estimate from the reassembled text is used in ``message_delta``.
    """
    message_id = message_id or f"msg_{uuid.uuid4().hex[:24]}"

    # message_start — envelope with empty content + placeholder usage.
    yield (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": _estimate_tokens(prompt),
                    "output_tokens": 0,
                },
            },
        },
    )
    yield (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )

    acc: list[str] = []
    saw_delta = False
    reported_out_tokens: int | None = None

    for msg in codex_events:
        method = msg.get("method")
        if method == "item/agentMessage/delta":
            chunk = _event_delta_text(msg)
            if chunk:
                saw_delta = True
                acc.append(chunk)
                yield (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                )
        elif method in ("item/completed", "item/updated"):
            # Fallback: codex that did NOT stream deltas still carries the full
            # reply on the completed item — surface it as one delta so the
            # client always receives the text.
            if not saw_delta:
                txt = _event_completed_text(msg)
                if txt:
                    saw_delta = True
                    acc.append(txt)
                    yield (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": txt},
                        },
                    )
        elif method == "turn/completed":
            tok = _event_output_tokens(msg)
            if tok is not None:
                reported_out_tokens = tok

    yield ("content_block_stop", {"type": "content_block_stop", "index": 0})

    out_tokens = (
        reported_out_tokens
        if reported_out_tokens is not None
        else _estimate_tokens("".join(acc))
    )
    yield (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": out_tokens},
        },
    )
    yield ("message_stop", {"type": "message_stop"})
