"""Privacy-safe native activity from one independently bound Codex rollout.

The caller must establish the live process/thread/path binding. This reader
does not select a home, discover the newest file, or infer a thread from its
filename. Rollout response call/output pairs describe tool lifecycle, not tool
success. Pane movement, agent prose and subagent events are not tool evidence.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

MAX_ROLLOUT_BYTES = 64 * 1024 * 1024
_CALLS = {"function_call": "function", "custom_tool_call": "custom"}
_OUTPUTS = {
    "function_call_output": "function",
    "custom_tool_call_output": "custom",
}


class CodexActivityError(ValueError):
    """The exact source cannot establish a complete native activity scope."""


@dataclass(frozen=True)
class CodexActivityObservation:
    """Fixed identifiers and measurements; never transcript content."""

    thread_id: str
    observed_at: float
    activity_at: float
    event_seq: int
    turns_accepted: int
    turns_completed: int
    tools_started: int
    tools_completed: int
    tools_inflight: int
    inflight_tool_ids: tuple[str, ...]
    last_event_type: str
    last_turn_status: str
    last_error_code: str

    def heartbeat_fields(self) -> dict[str, object]:
        fields = asdict(self)
        fields["inflight_tool_ids"] = list(self.inflight_tool_ids)
        return fields


def _identifier(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 200:
        raise CodexActivityError(f"native activity has invalid {key}")
    # Identifiers are deliberately narrower than arbitrary text/error messages.
    if not all(c.isascii() and (c.isalnum() or c in "-_.:") for c in value):
        raise CodexActivityError(f"native activity has invalid {key}")
    return value


def _timestamp(row: dict, observed_at: float) -> float:
    try:
        value = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("naive time")
        timestamp = value.timestamp()
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise CodexActivityError("native activity has invalid event timestamp") from exc
    if timestamp < 0 or timestamp > observed_at:
        raise CodexActivityError("native activity event exceeds observation time")
    return timestamp


def reduce_codex_activity(
    lines: Iterable[str], *, expected_thread_id: str, observed_at: float
) -> CodexActivityObservation:
    """Reduce complete JSONL records from the explicitly selected thread.

    Foreign copied session metadata is UNKNOWN: unscoped response items in a
    copied history cannot prove work by this thread. Duplicate lifecycle records
    are idempotent; conflicting IDs, orphan results and stale timestamps refuse.
    """
    _identifier({"thread_id": expected_thread_id}, "thread_id")
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, (int, float))
        or not math.isfinite(observed_at)
        or observed_at < 0
    ):
        raise CodexActivityError("native activity has invalid observation time")
    accepted: set[str] = set()
    completed: dict[str, str] = {}
    calls: dict[str, str] = {}
    outputs: set[str] = set()
    event_seq = 0
    activity_at = 0.0
    last_event = last_status = last_error = ""
    saw_meta = False
    for line in lines:
        if not line.endswith("\n"):
            raise CodexActivityError("native activity source has an incomplete record")
        try:
            row = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise CodexActivityError(
                "native activity source has malformed JSON"
            ) from exc
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            raise CodexActivityError("native activity source has malformed record")
        payload = row["payload"]
        if row.get("type") == "session_meta":
            if _identifier(payload, "id") != expected_thread_id:
                raise CodexActivityError(
                    "native activity contains another thread's history"
                )
            saw_meta = True
            continue
        if not saw_meta:
            raise CodexActivityError("native activity source has no thread identity")
        explicit_thread = payload.get("thread_id")
        if explicit_thread is not None and explicit_thread != expected_thread_id:
            # Typed items from a child/peer are not activity by the target.
            continue
        kind = payload.get("type")
        if row.get("type") in {"response_item", "event_msg"} and not isinstance(
            kind, str
        ):
            raise CodexActivityError("native activity has malformed event type")
        is_call = row.get("type") == "response_item" and kind in _CALLS
        is_output = row.get("type") == "response_item" and kind in _OUTPUTS
        is_turn = row.get("type") == "event_msg" and kind in {
            "task_started",
            "task_complete",
            "turn_aborted",
        }
        if not (is_call or is_output or is_turn):
            continue
        timestamp = _timestamp(row, float(observed_at))
        if timestamp < activity_at:
            raise CodexActivityError("native activity event timestamp regressed")
        changed = False
        if is_call or is_output:
            call_id = _identifier(payload, "call_id")
            family = (_CALLS if is_call else _OUTPUTS)[kind]
            if is_call:
                if call_id in calls and calls[call_id] != family:
                    raise CodexActivityError("native activity has conflicting tool IDs")
                changed = call_id not in calls
                calls[call_id] = family
            else:
                if calls.get(call_id) != family:
                    raise CodexActivityError(
                        "native activity has an uncorrelated tool result"
                    )
                changed = call_id not in outputs
                outputs.add(call_id)
        else:
            turn_id = _identifier(payload, "turn_id")
            if kind == "task_started":
                changed = turn_id not in accepted
                accepted.add(turn_id)
            else:
                if turn_id not in accepted:
                    raise CodexActivityError(
                        "native activity has an uncorrelated terminal turn"
                    )
                if payload.get("error") is not None and not isinstance(
                    payload["error"], dict
                ):
                    raise CodexActivityError(
                        "native activity has malformed terminal error"
                    )
                status = (
                    "interrupted"
                    if kind == "turn_aborted"
                    else ("error" if payload.get("error") is not None else "complete")
                )
                if turn_id in completed and completed[turn_id] != status:
                    raise CodexActivityError(
                        "native activity has conflicting terminal turns"
                    )
                changed = turn_id not in completed
                completed[turn_id] = status
                if changed:
                    last_status = status
                    last_error = "turn_error" if status == "error" else ""
        if changed:
            event_seq += 1
            activity_at = timestamp
            last_event = str(kind)
    if not saw_meta:
        raise CodexActivityError("native activity source has no thread identity")
    return CodexActivityObservation(
        thread_id=expected_thread_id,
        observed_at=float(observed_at),
        activity_at=activity_at,
        event_seq=event_seq,
        turns_accepted=len(accepted),
        turns_completed=len(completed),
        tools_started=len(calls),
        tools_completed=len(outputs),
        tools_inflight=len(calls.keys() - outputs),
        inflight_tool_ids=tuple(sorted(calls.keys() - outputs)),
        last_event_type=last_event,
        last_turn_status=last_status,
        last_error_code=last_error,
    )


def read_codex_activity(
    path: Path,
    *,
    expected_thread_id: str,
    observed_at: float,
    expected_file_identity: tuple[int, int],
) -> CodexActivityObservation:
    """Read the bound device/inode, refusing replacement or concurrent change."""
    try:
        with Path(path).open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != expected_file_identity:
                raise CodexActivityError("native activity source identity changed")
            if before.st_size > MAX_ROLLOUT_BYTES:
                raise CodexActivityError(
                    "native activity source exceeds bounded replay"
                )
            # Read the captured extent only: a growing rollout must never turn
            # this bounded observer into an unbounded follower of a live writer.
            raw = stream.read(before.st_size + 1)
            if len(raw) != before.st_size:
                raise CodexActivityError("native activity source changed during read")
            result = reduce_codex_activity(
                raw.decode("utf-8").splitlines(keepends=True),
                expected_thread_id=expected_thread_id,
                observed_at=observed_at,
            )
            after = os.fstat(stream.fileno())
        current = Path(path).stat()
    except (OSError, UnicodeError) as exc:
        raise CodexActivityError("native activity source is unavailable") from exc
    snapshot = (before.st_size, before.st_mtime_ns)
    if (
        snapshot != (after.st_size, after.st_mtime_ns)
        or snapshot != (current.st_size, current.st_mtime_ns)
        or (current.st_dev, current.st_ino) != expected_file_identity
    ):
        raise CodexActivityError("native activity source changed during observation")
    return result
