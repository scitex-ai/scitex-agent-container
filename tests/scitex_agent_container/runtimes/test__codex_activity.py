"""Exact native thread activity, without transcript-content testimony."""

import json
import threading
from datetime import datetime, timezone

import pytest

from scitex_agent_container.runtimes._codex_activity import (
    MAX_ROLLOUT_BYTES,
    CodexActivityError,
    read_codex_activity,
    reduce_codex_activity,
)

THREAD = "01a0fdd8-24b2-7b23-a264-4ae60f30245b"
CHILD = "01a0fdd8-fe8c-7e33-9211-ad0679062e7e"


def _row(record_type, payload, timestamp=10):
    return (
        json.dumps(
            {
                "timestamp": datetime.fromtimestamp(
                    timestamp, timezone.utc
                ).isoformat(),
                "type": record_type,
                "payload": payload,
            }
        )
        + "\n"
    )


def _meta(thread=THREAD, session=THREAD):
    return _row("session_meta", {"id": thread, "session_id": session, "source": "cli"})


def _turn(kind, turn="turn-1", timestamp=11, **extra):
    return _row("event_msg", {"type": kind, "turn_id": turn, **extra}, timestamp)


def _tool(kind, call="call-1", timestamp=12, **extra):
    return _row("response_item", {"type": kind, "call_id": call, **extra}, timestamp)


def _reduce(lines):
    return reduce_codex_activity(lines, expected_thread_id=THREAD, observed_at=100)


def test_correlated_native_pairs_are_counted_once_without_transcript_content():
    # Arrange: live-format response items plus their paginated completion mirror.
    private = "private-tool-argument-result-and-prompt"
    lines = [
        _meta(),
        _turn("task_started"),
        _tool("function_call", arguments=private),
        _tool("function_call_output", timestamp=13, output=private),
        _row(
            "event_msg",
            {
                "type": "item_completed",
                "thread_id": THREAD,
                "turn_id": "turn-1",
                "item": {"type": "CommandExecution", "id": "call-1", "stdout": private},
            },
            13,
        ),
        _tool("custom_tool_call", call="call-2", timestamp=14, input=private),
        _tool("custom_tool_call_output", call="call-2", timestamp=15, output=private),
        _turn("task_complete", timestamp=16, last_agent_message=private),
    ]

    # Act: reduce only the independently selected exact thread.
    observation = _reduce(lines)

    # Assert: two lifecycle pairs, one turn and no private content in publication.
    assert observation.heartbeat_fields() == {
        "thread_id": THREAD,
        "observed_at": 100.0,
        "activity_at": 16.0,
        "event_seq": 6,
        "turns_accepted": 1,
        "turns_completed": 1,
        "tools_started": 2,
        "tools_completed": 2,
        "tools_inflight": 0,
        "inflight_tool_ids": [],
        "last_event_type": "task_complete",
        "last_turn_status": "complete",
        "last_error_code": "",
    }


@pytest.mark.parametrize("manager", ["hub", "stats", "app", "ui", "cards"])
def test_error_with_zero_tools_never_becomes_productive_activity(manager):
    # Arrange: five observed manager failure shapes have no native tool pair.
    lines = [
        _meta(),
        _turn("task_started"),
        _turn(
            "task_complete",
            timestamp=12,
            error={"message": f"private-{manager}-provider-or-filesystem-error"},
        ),
    ]

    # Act: expose terminal status without publishing the full error message.
    fields = _reduce(lines).heartbeat_fields()

    # Assert: process/turn completion cannot masquerade as tool work.
    assert (
        fields["last_turn_status"],
        fields["last_error_code"],
        fields["tools_started"],
        fields["tools_completed"],
        fields["tools_inflight"],
    ) == ("error", "turn_error", 0, 0, 0)


def test_text_echo_subscriber_and_foreign_thread_events_are_not_tool_evidence():
    # Arrange: mechanical delivery, prose and a child's explicit event.
    lines = [
        _meta(),
        _row(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": "202 subscriber active tools_completed=9 nonce=abc",
            },
        ),
        _tool("function_call", thread_id=CHILD),
        _tool("function_call_output", thread_id=CHILD),
        _row(
            "event_msg",
            {
                "type": "item_completed",
                "thread_id": CHILD,
                "turn_id": "child-turn",
                "item": {"type": "McpToolCall", "id": "child-call"},
            },
        ),
    ]

    # Act: read evidence for the target only.
    observation = _reduce(lines)

    # Assert: these records provide no target progress or native tool lifecycle.
    assert (
        observation.event_seq,
        observation.tools_started,
        observation.tools_completed,
        observation.activity_at,
    ) == (0, 0, 0, 0)


def test_duplicate_native_records_are_idempotent_and_inflight_stays_visible():
    # Arrange: a repeated persisted pair and a separate still-open call.
    start = _turn("task_started")
    call = _tool("function_call")
    result = _tool("function_call_output", timestamp=13)
    lines = [
        _meta(),
        start,
        start,
        call,
        call,
        result,
        result,
        _tool("custom_tool_call", call="call-2", timestamp=14),
    ]

    # Act: replay the complete exact source.
    observation = _reduce(lines)

    # Assert: duplicate records do not advance the cursor or count twice.
    assert (
        observation.event_seq,
        observation.turns_accepted,
        observation.tools_started,
        observation.tools_completed,
        observation.tools_inflight,
        observation.inflight_tool_ids,
    ) == (4, 1, 2, 1, 1, ("call-2",))


def test_thread_identity_is_not_the_shared_conversation_session_id():
    # Arrange: native metadata can name a different root conversation session.
    lines = [_meta(THREAD, CHILD), _turn("task_started")]

    # Act: bind to the exact thread, not session_meta.session_id.
    observation = _reduce(lines)

    # Assert: the declared thread remains the sole activity identity.
    assert (observation.thread_id, observation.turns_accepted) == (THREAD, 1)


def test_subagent_activity_item_in_parent_thread_is_not_parent_tool_work():
    # Arrange: the parent stores a typed child completion notification.
    lines = [
        _meta(),
        _row(
            "event_msg",
            {
                "type": "item_completed",
                "thread_id": THREAD,
                "turn_id": "turn-1",
                "item": {
                    "type": "SubAgentActivity",
                    "agent_thread_id": CHILD,
                    "kind": "completed",
                    "id": "child-item",
                },
            },
        ),
    ]

    # Act: reduce native parent activity rather than aggregating descendants.
    observation = _reduce(lines)

    # Assert: a child completing work does not create tools in the parent.
    assert (
        observation.tools_started,
        observation.tools_completed,
        observation.event_seq,
    ) == (0, 0, 0)


@pytest.mark.parametrize(
    "lines",
    [
        [_meta(CHILD)],
        [_meta(THREAD, CHILD), _meta(CHILD), _tool("function_call")],
        [_tool("function_call")],
        [_meta(), _tool("function_call_output")],
        [_meta(), _tool("function_call"), _tool("custom_tool_call_output")],
        [_meta(), _tool("function_call"), _tool("custom_tool_call")],
        [_meta(), _turn("task_complete")],
        [_meta(), _turn("task_started"), _turn("task_complete", error=False)],
        [
            _meta(),
            _turn("task_started"),
            _turn("task_complete", timestamp=12),
            _turn("task_complete", timestamp=13, error={"message": "secret"}),
        ],
    ],
)
def test_wrong_copied_or_inconsistent_history_is_unknown(lines):
    # Arrange: malformed ownership/correlation must not create trusted counters.
    # Act and assert: the entire observation refuses instead of returning zero.
    # Assert
    with pytest.raises(CodexActivityError):
        _reduce(lines)


@pytest.mark.parametrize(
    "lines",
    [
        [_meta(), '{"type":"response_item"'],
        [_meta(), "invalid-json\n"],
        [_meta(), "[]\n"],
        [_meta(), _row("response_item", {"type": []})],
        [_meta(), _turn("task_started", timestamp=101)],
        [
            _meta(),
            _turn("task_started", timestamp=12),
            _tool("function_call", timestamp=11),
        ],
    ],
)
def test_partial_malformed_future_or_stale_records_refuse(lines):
    # Arrange: source completeness and time ordering are authority requirements.
    # Act and assert: an incomplete/stale source remains UNKNOWN.
    # Assert
    with pytest.raises(CodexActivityError):
        _reduce(lines)


def test_interruption_is_a_terminal_status_without_success_or_tools():
    # Arrange: one accepted turn is interrupted by the native harness.
    lines = [
        _meta(),
        _turn("task_started"),
        _turn("turn_aborted", timestamp=12, reason="interrupted"),
    ]

    # Act: project native terminal lifecycle.
    observation = _reduce(lines)

    # Assert: interruption remains visible separately from successful work.
    assert (
        observation.turns_completed,
        observation.last_turn_status,
        observation.tools_completed,
    ) == (1, "interrupted", 0)


@pytest.mark.parametrize("observed_at", [True, "100", float("nan"), -1])
def test_invalid_observation_time_cannot_bless_a_native_source(observed_at):
    # Arrange: the owner must supply an actual finite observation timestamp.
    # Act and assert: malformed timing remains UNKNOWN.
    # Assert
    with pytest.raises(CodexActivityError, match="observation time"):
        reduce_codex_activity(
            [_meta()], expected_thread_id=THREAD, observed_at=observed_at
        )


def test_reader_uses_exact_bound_inode_and_never_discovers_other_files(tmp_path):
    # Arrange: both an authoritative rollout and a newer unrelated file exist.
    path = tmp_path / "bound.jsonl"
    path.write_text(_meta() + _turn("task_started"))
    (tmp_path / "newest.jsonl").write_text(_meta(CHILD))
    stat = path.stat()

    # Act: read only the independently supplied path and file identity.
    observation = read_codex_activity(
        path,
        expected_thread_id=THREAD,
        observed_at=100,
        expected_file_identity=(stat.st_dev, stat.st_ino),
    )

    # Assert: newer unrelated activity does not influence this thread.
    assert (observation.thread_id, observation.turns_accepted) == (THREAD, 1)


def test_reader_refuses_replaced_source_identity(tmp_path):
    # Arrange: a same-thread file replaced the inode captured by the owner.
    path = tmp_path / "bound.jsonl"
    path.write_text(_meta())
    old = path.stat()
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(_meta())
    replacement.replace(path)

    # Act and assert: matching textual UUID cannot bless a replaced source.
    # Assert
    with pytest.raises(CodexActivityError, match="identity changed"):
        read_codex_activity(
            path,
            expected_thread_id=THREAD,
            observed_at=100,
            expected_file_identity=(old.st_dev, old.st_ino),
        )


def test_reader_refuses_a_real_concurrently_appended_source(tmp_path):
    # Arrange: the native source is growing while the complete replay is read.
    path = tmp_path / "bound.jsonl"
    noise = _row("response_item", {"type": "message", "content": "irrelevant"})
    path.write_text(_meta() + noise * 100000)
    stat = path.stat()
    stop = threading.Event()
    started = threading.Event()

    def append():
        with path.open("a") as stream:
            while not stop.is_set():
                stream.write(noise)
                stream.flush()
                started.set()
                stop.wait(0.001)

    writer = threading.Thread(target=append)
    writer.start()
    started.wait(1)

    # Act and assert: preserve prior authority until a stable complete read.
    # Assert
    try:
        with pytest.raises(CodexActivityError):
            read_codex_activity(
                path,
                expected_thread_id=THREAD,
                observed_at=100,
                expected_file_identity=(stat.st_dev, stat.st_ino),
            )
    finally:
        stop.set()
        writer.join(1)


def test_reader_refuses_an_oversize_source_without_partial_tail_inference(tmp_path):
    # Arrange: bounded replay cannot establish full history for this file.
    path = tmp_path / "bound.jsonl"
    with path.open("wb") as stream:
        stream.truncate(MAX_ROLLOUT_BYTES + 1)
    stat = path.stat()

    # Act and assert: no tail-only count or fabricated zero is returned.
    # Assert
    with pytest.raises(CodexActivityError, match="bounded replay"):
        read_codex_activity(
            path,
            expected_thread_id=THREAD,
            observed_at=100,
            expected_file_identity=(stat.st_dev, stat.st_ino),
        )
