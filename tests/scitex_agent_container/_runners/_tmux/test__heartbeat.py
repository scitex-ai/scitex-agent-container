"""Day-2 (C) tests for the tmux heartbeat poller.

Best-effort emit of an SDK-compatible ``heartbeat.json`` derived from
``tmux capture-pane`` so the existing dashboard / ``sac fleet
status`` can read it without runtime-awareness.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._runners._tmux._heartbeat import (
    STATE_IDLE,
    STATE_STARTING,
    STATE_UNKNOWN,
    STATE_WORKING,
    build_heartbeat_payload,
    classify_pane_state,
    poll_once,
    write_heartbeat,
)

# ---------------------------------------------------------------------------
# classify_pane_state
# ---------------------------------------------------------------------------


def test_classify_empty_pane_returns_starting():
    assert classify_pane_state("") == STATE_STARTING


def test_classify_whitespace_only_pane_returns_starting():
    assert classify_pane_state("\n   \n\t\n") == STATE_STARTING


def test_classify_working_marker_returns_working():
    pane = "previous turn\nWorking… esc to interrupt"
    assert classify_pane_state(pane) == STATE_WORKING


def test_classify_ruminating_marker_returns_working():
    pane = "previous turn\nRuminating… esc to interrupt"
    assert classify_pane_state(pane) == STATE_WORKING


def test_classify_ready_marker_returns_idle():
    pane = "❯                  | bypass permissions  off"
    assert classify_pane_state(pane) == STATE_IDLE


def test_classify_ambiguous_returns_unknown():
    pane = "random pane content with no markers\n\n"
    assert classify_pane_state(pane) == STATE_UNKNOWN


def test_classify_radio_selector_is_not_idle():
    """A pane showing a TUI radio prompt must NOT be classified idle."""
    pane = "bypass permissions\nEnter to confirm · 1. yes"
    assert classify_pane_state(pane) != STATE_IDLE


# ---------------------------------------------------------------------------
# build_heartbeat_payload
# ---------------------------------------------------------------------------


def test_heartbeat_payload_includes_required_fields():
    payload = build_heartbeat_payload(
        pid=4242, pane_content="❯  | bypass permissions", now=100.0
    )
    assert payload["pid"] == 4242
    assert payload["ts"] == 100.0
    assert payload["state"] == STATE_IDLE
    assert payload["runtime"] == "tmux"


def test_heartbeat_payload_token_fields_are_null_for_tmux():
    """The tmux driver has no SDK quota stream; mark token fields null."""
    payload = build_heartbeat_payload(pid=1, pane_content="❯  | bypass permissions")
    assert payload["input_tokens"] is None
    assert payload["output_tokens"] is None
    assert payload["total_tokens"] is None


def test_heartbeat_payload_includes_elapsed_when_started_at_given():
    payload = build_heartbeat_payload(
        pid=1,
        pane_content="❯  | bypass permissions",
        started_at=90.0,
        now=100.0,
    )
    assert payload["started_at"] == 90.0
    assert payload["elapsed_s"] == 10.0


def test_heartbeat_payload_omits_elapsed_without_started_at():
    payload = build_heartbeat_payload(pid=1, pane_content="❯  | bypass permissions")
    assert "elapsed_s" not in payload
    assert "started_at" not in payload


# ---------------------------------------------------------------------------
# write_heartbeat
# ---------------------------------------------------------------------------


def test_write_heartbeat_creates_json_file(tmp_path: Path):
    payload = {"pid": 1, "state": STATE_IDLE, "ts": 1.0, "runtime": "tmux"}
    final = write_heartbeat(tmp_path, payload)
    assert final == tmp_path / "heartbeat.json"
    loaded = json.loads(final.read_text())
    assert loaded == payload


def test_write_heartbeat_uses_atomic_replace_pattern(tmp_path: Path):
    """A second write must not leave a stale ``.tmp`` sidecar."""
    payload1 = {"pid": 1, "ts": 1.0, "state": STATE_IDLE, "runtime": "tmux"}
    payload2 = {"pid": 2, "ts": 2.0, "state": STATE_WORKING, "runtime": "tmux"}
    write_heartbeat(tmp_path, payload1)
    write_heartbeat(tmp_path, payload2)
    assert json.loads((tmp_path / "heartbeat.json").read_text()) == payload2
    assert not (tmp_path / "heartbeat.json.tmp").exists()


# ---------------------------------------------------------------------------
# poll_once integration
# ---------------------------------------------------------------------------


class _FakePane:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[str] = []

    def capture_pane(self, session: str) -> str:
        self.calls.append(session)
        return self.content


def test_poll_once_writes_heartbeat_with_classified_state(tmp_path: Path):
    pane = _FakePane("Working… please wait")
    payload = poll_once(
        pane_source=pane,
        session="sac-demo",
        state_dir=tmp_path,
        pid=999,
    )
    assert payload["state"] == STATE_WORKING
    assert payload["pid"] == 999
    on_disk = json.loads((tmp_path / "heartbeat.json").read_text())
    assert on_disk["state"] == STATE_WORKING


def test_poll_once_calls_capture_pane_with_session_name(tmp_path: Path):
    pane = _FakePane("❯  | bypass permissions")
    poll_once(
        pane_source=pane,
        session="sac-y",
        state_dir=tmp_path,
        pid=1,
    )
    assert pane.calls == ["sac-y"]


def test_poll_once_emits_starting_when_capture_returns_empty(tmp_path: Path):
    pane = _FakePane("")
    payload = poll_once(pane_source=pane, session="sac-x", state_dir=tmp_path, pid=1)
    assert payload["state"] == STATE_STARTING


class _BrokenPane:
    def capture_pane(self, session: str) -> str:
        raise RuntimeError("simulated tmux explosion")


def test_poll_once_survives_pane_capture_failure(tmp_path: Path):
    """A best-effort poller must NEVER raise; degraded payload is fine."""
    pane = _BrokenPane()
    payload = poll_once(pane_source=pane, session="sac-bad", state_dir=tmp_path, pid=1)
    # capture failed → empty content → classifier returns starting.
    assert payload["state"] == STATE_STARTING
    # File still written despite the inner failure.
    assert (tmp_path / "heartbeat.json").exists()
