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
def test_digest_in_correlated_new_native_output_proves_lifecycle(family):
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


@pytest.mark.parametrize("family", ["function", "custom_tool"])
def test_digest_proof_excludes_private_native_output(family):
    # Arrange
    lines = [
        _meta(),
        _tool(f"{family}_call", 21),
        _tool(f"{family}_call_output", 22, output=f"private-result {ANSWER}"),
    ]
    # Act
    proof = _proof(lines)
    # Assert
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
    "call_at,outputs,cursor,extra",
    [
        pytest.param(19, [ANSWER], 0, {}, id="old"),
        pytest.param(21, [ANSWER], 1, {}, id="pre_cursor"),
        pytest.param(21, [ANSWER], 0, {"thread_id": "foreign-child"}, id="foreign"),
        pytest.param(21, [], 0, {}, id="inflight"),
        pytest.param(21, ["unrelated-output"], 0, {}, id="arguments"),
        pytest.param(21, ["wrong-hash"], 0, {}, id="wrong_hash"),
        pytest.param(21, ["a" + ANSWER + "b"], 0, {}, id="embedded_hash"),
    ],
)
def test_nonproof_shapes_never_satisfy_fresh_computation(
    call_at, outputs, cursor, extra
):
    # Arrange
    lines = [_meta(), _tool("function_call", call_at, arguments=ANSWER, **extra)]
    lines.extend(
        _tool("function_call_output", 22, output=output, **extra) for output in outputs
    )
    # Act
    # Assert
    assert _proof(lines, cursor=cursor) is None


@pytest.mark.parametrize(
    "mutate,cursor",
    [
        pytest.param(
            lambda lines: lines.insert(1, _meta("foreign-child")), 0, id="child_history"
        ),
        pytest.param(lambda lines: lines.pop(1), 0, id="orphan"),
        pytest.param(
            lambda lines: lines.__setitem__(-1, lines[-1].rstrip("\n")), 0, id="partial"
        ),
        pytest.param(lambda lines: None, 3, id="regressed"),
    ],
)
def test_unknown_or_unbound_rollout_is_refused_even_with_the_right_hash(mutate, cursor):
    # Arrange
    lines = [
        _meta(),
        _tool("function_call", 21),
        _tool("function_call_output", 22, output=ANSWER),
    ]
    mutate(lines)
    # Act
    # Assert
    with pytest.raises(CodexActivityError):
        _proof(lines, cursor=cursor)


def test_duplicate_records_preserve_the_authoritative_cursor():
    # Arrange
    call = _tool("function_call", 21)
    output = _tool("function_call_output", 22, output=ANSWER)
    # Act
    proof = _proof([_meta(), call, call, output, output])
    # Assert
    assert proof.event_seq == 2
