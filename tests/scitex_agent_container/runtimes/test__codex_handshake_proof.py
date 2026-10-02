"""A fresh digest must be observed in a new, correlated primary-thread tool."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from scitex_agent_container.runtimes._codex_activity import CodexActivityError
from scitex_agent_container.runtimes._codex_handshake_proof import (
    reduce_codex_tool_proof,
)

THREAD = "01a0fdd8-24b2-7b23-a264-4ae60f30245b"
ANSWER = hashlib.sha256(b"server-nonce:server-payload").hexdigest()


def _row(record_type, payload, timestamp=10):
    return (
        json.dumps(
            {
                "type": record_type,
                "payload": payload,
                "timestamp": datetime.fromtimestamp(
                    timestamp, timezone.utc
                ).isoformat(),
            }
        )
        + "\n"
    )


def _meta(thread=THREAD):
    return _row("session_meta", {"id": thread, "source": "cli"})


def _tool(kind, timestamp, call_id="call-live", **extra):
    return _row("response_item", {"type": kind, "call_id": call_id, **extra}, timestamp)


def _proof(lines, cursor=0):
    return reduce_codex_tool_proof(
        lines,
        thread_id=THREAD,
        observed_at=100,
        cursor=cursor,
        issued_at=20,
        answer=ANSWER,
    )


@pytest.mark.parametrize("family", ["function", "custom_tool"])
def test_digest_in_correlated_new_native_output_proves_lifecycle_without_private_text(
    family,
):
    # Arrange: exact native call/output format, with private surrounding output.
    lines = [
        _meta(),
        _tool(f"{family}_call", 21),
        _tool(f"{family}_call_output", 22, output=f"private-result {ANSWER}"),
    ]
    # Act
    proof = _proof(lines)
    # Assert: no arguments/results/private text leave the reducer.
    assert asdict(proof) == {
        "call_id": "call-live",
        "event_seq": 2,
        "completed_at": 22.0,
    }
    assert "private-result" not in repr(proof)


@pytest.mark.parametrize("manager", ["hub", "stats", "app", "ui", "cards"])
def test_manager_terminal_error_zero_tools_cannot_prove_handshake(manager):
    # Arrange: accepted then failed, while prose claims a hash and subscriber ACK.
    lines = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "turn-1"}, 21),
        _row("response_item", {"type": "message", "content": f"202 echo {ANSWER}"}, 22),
        _row(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "turn-1",
                "error": {"message": f"private-{manager}"},
            },
            23,
        ),
    ]
    # Act
    # Assert
    assert _proof(lines) is None


@pytest.mark.parametrize(
    "shape",
    [
        "old",
        "pre_cursor",
        "foreign",
        "inflight",
        "arguments",
        "wrong_hash",
        "embedded_hash",
    ],
)
def test_nonproof_shapes_never_satisfy_fresh_computation(shape):
    # Arrange
    call_at = 19 if shape == "old" else 21
    extra = {"thread_id": "foreign-child"} if shape == "foreign" else {}
    lines = [_meta(), _tool("function_call", call_at, arguments=ANSWER, **extra)]
    if shape != "inflight":
        output = "unrelated-output" if shape in {"arguments", "wrong_hash"} else ANSWER
        if shape == "embedded_hash":
            output = "a" + ANSWER + "b"
        lines.append(_tool("function_call_output", 22, output=output, **extra))
    # Act
    # Assert
    assert _proof(lines, cursor=1 if shape == "pre_cursor" else 0) is None


@pytest.mark.parametrize("shape", ["child_history", "orphan", "partial", "regressed"])
def test_unknown_or_unbound_rollout_is_refused_even_with_the_right_hash(shape):
    # Arrange
    lines = [
        _meta(),
        _tool("function_call", 21),
        _tool("function_call_output", 22, output=ANSWER),
    ]
    if shape == "child_history":
        lines.insert(1, _meta("foreign-child"))
    elif shape == "orphan":
        lines.pop(1)
    elif shape == "partial":
        lines[-1] = lines[-1].rstrip("\n")
    # Act
    # Assert
    with pytest.raises(CodexActivityError):
        _proof(lines, cursor=3 if shape == "regressed" else 0)


def test_duplicate_records_preserve_the_authoritative_cursor():
    # Arrange
    call = _tool("function_call", 21)
    output = _tool("function_call_output", 22, output=ANSWER)
    # Act
    proof = _proof([_meta(), call, call, output, output])
    # Assert
    assert proof.event_seq == 2
