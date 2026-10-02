"""Observe a challenge computation without exporting native transcript text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ._codex_activity import (
    CodexActivityError,
    _read_codex_snapshot,
    _timestamp,
    reduce_codex_activity,
)


@dataclass(frozen=True)
class NativeToolProof:
    """A native lifecycle completion, not nested tool success."""

    call_id: str
    event_seq: int
    completed_at: float


def reduce_codex_tool_proof(
    lines,
    *,
    thread_id: str,
    observed_at: float,
    cursor: int,
    issued_at: float,
    answer: str,
) -> NativeToolProof | None:
    """Require one new native call/output containing the independent digest.

    Full rollout validation runs first. Foreign histories, truncated records,
    or uncorrelated outputs cannot become proof even when they contain a hash.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", answer):
        raise CodexActivityError("native challenge answer is not a SHA256 digest")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise CodexActivityError("native challenge cursor is invalid")
    lines = tuple(lines)
    observation = reduce_codex_activity(
        lines, expected_thread_id=thread_id, observed_at=observed_at
    )
    if observation.event_seq < cursor:
        raise CodexActivityError("native challenge source regressed")
    seen = set()
    calls = {}
    seq = 0
    proof = None
    for line in lines:
        row = json.loads(line)
        payload = row["payload"]
        if payload.get("thread_id", thread_id) != thread_id:
            continue
        kind = payload.get("type")
        family = None
        if row.get("type") == "response_item":
            if kind in ("function_call", "custom_tool_call"):
                family = "call"
            elif kind in ("function_call_output", "custom_tool_call_output"):
                family = "output"
        elif row.get("type") == "event_msg" and kind in (
            "task_started",
            "task_complete",
            "turn_aborted",
        ):
            family = "started" if kind == "task_started" else "terminal"
        if family is None:
            continue
        identity = payload["call_id" if family in ("call", "output") else "turn_id"]
        key = (family, identity)
        if key in seen:
            continue
        seen.add(key)
        seq += 1
        timestamp = _timestamp(row, observed_at)
        if family == "call":
            calls[identity] = (seq, timestamp)
        elif family == "output":
            call_seq, call_at = calls[identity]
            output = payload.get("output")
            # Read only in memory. Return fixed call IDs/time, never output text.
            contains_answer = (
                isinstance(output, str)
                and re.search(rf"(?<![0-9a-f]){answer}(?![0-9a-f])", output) is not None
            )
            if call_seq > cursor and call_at >= issued_at and contains_answer:
                proof = NativeToolProof(identity, seq, timestamp)
    return proof


def read_codex_tool_proof(binding, *, observed_at, cursor, issued_at, answer):
    """Use the already fenced owner's exact open FD, never a discovery path."""
    return _read_codex_snapshot(
        binding.rollout_path,
        expected_file_identity=binding.rollout_identity,
        reduce_fn=lambda lines: reduce_codex_tool_proof(
            lines,
            thread_id=binding.thread_id,
            observed_at=observed_at,
            cursor=cursor,
            issued_at=issued_at,
            answer=answer,
        ),
    )
