"""Tests for the TUI outbound completion plumbing (``runtimes._tui_outbound``).

The symmetric outbound half of the TUI a2a loop: record a requester-bearing
inbound (DB), summarise the reply from the transcript, and push a
dispatch-correlated completion report to the requester (SDK parity).

Real seams (no mocks): a tmp ``state.db`` for the inbound ledger, a real
JSONL transcript file for summary extraction, and a recording ``push_fn``
in place of the network push. STX-TQ002 AAA + STX-TQ007 one assert +
STX-TQ003 descriptive names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._state import inbound_ledger as ledger
from scitex_agent_container.runtimes import _tui_outbound as outbound


def _write_transcript(path: Path, *records: dict) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_record_dispatch_noop_without_from_agent(tmp_path: Path, pg_schema: str) -> None:
    # Arrange
    # Act
    row_id = outbound.record_dispatch(
        agent="a", from_agent="", dispatch_id="d1"
    )
    # Assert
    assert row_id is None


def test_record_dispatch_records_pending_inbound(tmp_path: Path, pg_schema: str) -> None:
    # Arrange
    # Act
    outbound.record_dispatch(agent="a", from_agent="lead", dispatch_id="d1")
    # Assert
    assert len(ledger.list_inbound(agent="a")) == 1


def test_summarize_transcript_returns_last_assistant_text(tmp_path: Path) -> None:
    # Arrange
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "hi"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "first"}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "ACK-OK"}]},
        },
    )
    # Act
    status, summary = outbound.summarize_transcript(transcript)
    # Assert
    assert (status, summary) == ("success", "ACK-OK")


def test_summarize_transcript_unknown_when_no_assistant_reply(tmp_path: Path) -> None:
    # Arrange
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, {"type": "user", "message": {"content": "hi"}})
    # Act
    status, _summary = outbound.summarize_transcript(transcript)
    # Assert
    assert status == "unknown"


def test_flush_one_completion_false_when_queue_empty(tmp_path: Path, pg_schema: str) -> None:
    # Arrange — nothing recorded.
    # Act
    flushed = outbound.flush_one_completion(
        agent="a",
        transcript_path=None,
        listen_url="http://127.0.0.1:7878",
        bearer=None,
        push_fn=lambda **_kw: None,
    )
    # Assert
    assert flushed is False


def test_flush_one_completion_pushes_to_requester(tmp_path: Path, pg_schema: str) -> None:
    # Arrange — one queued dispatch + a transcript with a reply.
    outbound.record_dispatch(agent="a", from_agent="lead", dispatch_id="d1")
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "done"}]},
        },
    )
    captured: dict[str, Any] = {}

    def push(**kw: Any) -> None:
        captured.update(kw)

    # Act
    outbound.flush_one_completion(
        agent="a",
        transcript_path=transcript,
        listen_url="http://127.0.0.1:7878",
        bearer="tok",
        push_fn=push,
    )
    # Assert
    assert captured["requester"] == "lead"


def test_flush_one_completion_marks_reported_on_success(tmp_path: Path, pg_schema: str) -> None:
    # Arrange
    outbound.record_dispatch(agent="a", from_agent="lead", dispatch_id="d1")
    # Act
    outbound.flush_one_completion(
        agent="a",
        transcript_path=None,
        listen_url="http://127.0.0.1:7878",
        bearer=None,
        push_fn=lambda **_kw: None,
    )
    # Assert
    rows = ledger.list_inbound(agent="a")
    assert rows[0]["status"] == ledger.STATUS_REPORTED


def test_flush_one_completion_marks_failed_and_raises_on_push_error(
    tmp_path: Path,
    pg_schema: str,
) -> None:
    # Arrange — a push that fails loud (no live subscriber).
    outbound.record_dispatch(agent="a", from_agent="lead", dispatch_id="d1")

    def boom(**_kw: Any) -> None:
        raise RuntimeError("no live subscriber")

    # Act
    # Assert
    with pytest.raises(RuntimeError):
        outbound.flush_one_completion(
                agent="a",
            transcript_path=None,
            listen_url="http://127.0.0.1:7878",
            bearer=None,
            push_fn=boom,
        )


def test_summarize_transcript_unknown_when_file_absent(tmp_path: Path) -> None:
    # Arrange
    missing = tmp_path / "nope.jsonl"
    # Act
    status, summary = outbound.summarize_transcript(missing)
    # Assert
    assert (status, summary) == ("unknown", "")


def test_summarize_transcript_truncates_to_cap(tmp_path: Path) -> None:
    # Arrange — an assistant reply longer than the cap.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x" * 50}]},
        },
    )
    # Act
    _status, summary = outbound.summarize_transcript(transcript, cap=10)
    # Assert
    assert summary == "x" * 10 + "…"


def _run_main_with(env: dict[str, str], stdin_text: str) -> int:
    """Invoke ``outbound.main`` with a real (restored) env + stdin — no mocks."""
    import io
    import os
    import sys

    keys = (
        "SCITEX_AGENT_CONTAINER_AGENT",
        "SCITEX_AGENT_CONTAINER_STATE_DB",
        "SAC_LISTEN_BASE_URL",
        "SAC_LISTEN_BEARER",
    )
    saved = {k: os.environ.get(k) for k in keys}
    saved_stdin = sys.stdin
    for k in keys:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    sys.stdin = io.StringIO(stdin_text)
    try:
        return outbound.main([])
    finally:
        sys.stdin = saved_stdin
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_main_returns_zero_when_queue_empty(tmp_path: Path, pg_schema: str) -> None:
    """Wired env, empty ledger, a transcript path on stdin.

    ``SCITEX_AGENT_CONTAINER_STATE_DB`` IS DELIBERATELY ABSENT, and its absence
    is now the interesting half — see the sibling test below. The two env vars
    here are the two ``main`` still needs: who to report as, and where the bus
    is.
    """
    # Arrange
    env = {
        "SCITEX_AGENT_CONTAINER_AGENT": "a",
        "SAC_LISTEN_BASE_URL": "http://127.0.0.1:7878",
    }
    # Act
    rc = _run_main_with(env, json.dumps({"transcript_path": str(tmp_path / "x.jsonl")}))
    # Assert
    assert rc == 0


def test_main_still_flushes_without_the_retired_state_db_env(
    tmp_path: Path, pg_schema: str
) -> None:
    """The ledger moved to PostgreSQL, so a local path must not gate the flush.

    ``main`` used to refuse unless ``SCITEX_AGENT_CONTAINER_STATE_DB`` was set,
    which was right while the ledger was a file and became a trap the moment it
    was not: an agent without that variable would have silently stopped
    reporting completions, with the refusal resting on a fact that no longer
    bears on whether the work can be done.

    Pinned as a POSITIVE outcome rather than "does not crash": a pending row is
    recorded first, so a `main` that still gated on the retired variable would
    return before flushing it and leave the row pending.
    """
    # Arrange — one pending dispatch, and no STATE_DB anywhere in the env.
    ledger.record_inbound(agent="a", from_agent="lead", dispatch_id="d1")
    env = {
        "SCITEX_AGENT_CONTAINER_AGENT": "a",
        "SAC_LISTEN_BASE_URL": "http://127.0.0.1:7878",
    }
    _run_main_with(env, json.dumps({"transcript_path": str(tmp_path / "x.jsonl")}))
    # Act
    rows = ledger.list_inbound(agent="a")
    # Assert
    assert rows and rows[0]["status"] != ledger.STATUS_PENDING


def test_main_noop_when_agent_env_absent(tmp_path: Path) -> None:
    # Arrange — no sac agent env → main cannot resolve context.
    # Act
    rc = _run_main_with({}, "{}")
    # Assert
    assert rc == 0
