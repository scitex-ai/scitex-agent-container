"""A fresh digest must be observed in a new, correlated primary-thread tool."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scitex_agent_container._listen._agent_handshake import _observe_proof
from scitex_agent_container.runtimes._codex_activity import CodexActivityError
from scitex_agent_container.runtimes._codex_handshake_proof import (
    read_codex_tool_proof,
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


@pytest.mark.parametrize("family", ["function", "custom_tool"])
@pytest.mark.parametrize("block_type", ["input_text", "text"])
def test_correlated_typed_text_block_proves_without_exporting_private_text(
    family, block_type
):
    # Arrange: actual native input_text shape and the declared MCP text shape.
    lines = [
        _meta(),
        _tool(f"{family}_call", 21),
        _tool(
            f"{family}_call_output",
            22,
            output=[{"type": block_type, "text": f"private-result\n{ANSWER}\n"}],
        ),
    ]
    # Act
    proof = _proof(lines)
    # Assert
    assert asdict(proof) == {
        "call_id": "call-live",
        "event_seq": 2,
        "completed_at": 22.0,
    }


@pytest.mark.parametrize(
    "output",
    [
        pytest.param({"type": "input_text", "text": ANSWER}, id="untyped_container"),
        pytest.param([ANSWER], id="untyped_list_member"),
        pytest.param([[{"type": "input_text", "text": ANSWER}]], id="nested_list"),
        pytest.param(
            [{"content": [{"type": "input_text", "text": ANSWER}]}], id="nested_content"
        ),
        pytest.param([{"text": ANSWER}], id="missing_type"),
        pytest.param([{"type": ["input_text"], "text": ANSWER}], id="malformed_type"),
        pytest.param([{"type": "image", "text": ANSWER}], id="nontext_type"),
        pytest.param([{"type": "output_text", "text": ANSWER}], id="unsupported_type"),
        pytest.param(
            [{"type": "input_text", "text": {"value": ANSWER}}], id="nested_text"
        ),
        pytest.param([{"type": "input_text", "text": [ANSWER]}], id="text_list"),
        pytest.param([{"type": "input_text", "metadata": ANSWER}], id="metadata_only"),
        pytest.param(
            [{"type": "input_text", "text": "unrelated", "metadata": ANSWER}],
            id="nontext_digest",
        ),
        pytest.param([None, 42, True], id="malformed_members"),
        pytest.param(
            [
                {"type": "input_text", "text": ANSWER[:32]},
                {"type": "input_text", "text": ANSWER[32:]},
            ],
            id="digest_split_across_blocks",
        ),
    ],
)
def test_nontext_or_malformed_block_data_cannot_supply_digest(output):
    # Arrange
    lines = [
        _meta(),
        _tool("function_call", 21),
        _tool("function_call_output", 22, output=output),
    ]
    # Act
    proof = _proof(lines)
    # Assert
    assert proof is None


@pytest.mark.parametrize("prefix,suffix", [("A", ""), ("", "F"), ("0", ""), ("", "a")])
@pytest.mark.parametrize("block_type", ["input_text", "text"])
def test_text_block_digest_must_be_a_complete_hex_token(prefix, suffix, block_type):
    # Arrange
    lines = [
        _meta(),
        _tool("function_call", 21),
        _tool(
            "function_call_output",
            22,
            output=[{"type": block_type, "text": prefix + ANSWER + suffix}],
        ),
    ]
    # Act
    proof = _proof(lines)
    # Assert
    assert proof is None


@pytest.mark.parametrize(
    "call_at,cursor,extra",
    [(19, 0, {}), (21, 1, {}), (21, 0, {"thread_id": "foreign-child"})],
)
def test_typed_digest_cannot_cross_issue_cursor_or_thread_fence(call_at, cursor, extra):
    # Arrange
    lines = [
        _meta(),
        _tool("function_call", call_at, **extra),
        _tool(
            "function_call_output",
            22,
            output=[{"type": "input_text", "text": ANSWER}],
            **extra,
        ),
    ]
    # Act
    proof = _proof(lines, cursor=cursor)
    # Assert
    assert proof is None


def test_later_repeated_digest_does_not_erase_first_timely_native_completion():
    # Arrange: both are genuine calls; the later reply exceeds a lease ending 30.
    lines = [
        _meta(),
        _tool("function_call", 21, call_id="call-timely"),
        _tool(
            "function_call_output",
            22,
            call_id="call-timely",
            output=[{"type": "input_text", "text": ANSWER}],
        ),
        _tool("function_call", 31, call_id="call-late"),
        _tool(
            "function_call_output",
            32,
            call_id="call-late",
            output=[{"type": "input_text", "text": ANSWER}],
        ),
    ]
    # Act
    proof = _proof(lines)
    # Assert
    assert asdict(proof) == {
        "call_id": "call-timely",
        "event_seq": 2,
        "completed_at": 22.0,
    }


def test_replaced_source_cannot_prove_even_with_typed_digest(tmp_path):
    # Arrange: the existing fenced reader sees a different inode at the same path.
    source = tmp_path / "owned.jsonl"
    source.write_text(
        _meta()
        + _tool("function_call", 21)
        + _tool(
            "function_call_output", 22, output=[{"type": "input_text", "text": ANSWER}]
        )
    )
    stat = source.stat()
    binding = SimpleNamespace(
        rollout_path=source,
        rollout_identity=(stat.st_dev, stat.st_ino),
        thread_id=THREAD,
    )
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(source.read_bytes())
    replacement.replace(source)
    # Act
    # Assert
    with pytest.raises(CodexActivityError):
        read_codex_tool_proof(
            binding, observed_at=100, cursor=0, issued_at=20, answer=ANSWER
        )


@pytest.mark.parametrize(
    "times,expected_completion",
    [([(21, 22)], 22), ([(31, 32)], None), ([(21, 22), (31, 32)], 22)],
)
def test_existing_server_lease_fence_keeps_timely_typed_proof_only(
    tmp_path, times, expected_completion
):
    # Arrange: real source bytes pass through the unchanged server observation seam.
    target = {
        "agent": "synthetic",
        "host": "synthetic-host",
        "instance_id": "owned-instance",
        "boot_id": "owned-instance:123:456",
        "session_id": THREAD,
    }
    lines = [_meta()]
    for index, (call_at, output_at) in enumerate(times):
        call_id = f"call-{index}"
        lines.extend(
            [
                _tool("function_call", call_at, call_id=call_id),
                _tool(
                    "function_call_output",
                    output_at,
                    call_id=call_id,
                    output=[{"type": "input_text", "text": ANSWER}],
                ),
            ]
        )
    source = tmp_path / "owned.jsonl"
    source.write_text("".join(lines))
    stat = source.stat()
    binding = SimpleNamespace(
        rollout_path=source,
        rollout_identity=(stat.st_dev, stat.st_ino),
        thread_id=THREAD,
    )
    contract = {
        "target": target,
        "cursor": 0,
        "issued_at": 20,
        "deadline": 30,
        "nonce": "server-nonce",
        "payload": "server-payload",
        "source_identity": list(binding.rollout_identity),
    }
    # Act
    proof = _observe_proof(
        contract,
        capture=lambda *args: (target, len(times) * 2, binding),
        clock=lambda: 100,
    )
    # Assert
    assert (proof.completed_at if proof else None) == expected_completion
