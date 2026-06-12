"""Day-2 (C) tests for the tmux heartbeat poller.

Best-effort emit of an SDK-compatible ``heartbeat.json`` derived from
``tmux capture-pane`` so the existing dashboard / ``sac fleet status``
can read it without runtime-awareness.

Test style (project standards — STX-TQ002 / STX-TQ007):

* Each test carries explicit ``# Arrange`` / ``# Act`` / ``# Assert``
  marker comments on their own lines.
* Each test has exactly ONE assertion. Multi-field shapes are split
  across one-assert-per-test functions so a single failure surfaces
  the precise field — "build_heartbeat_payload returns wrong ts" is
  a clearer signal than "build_heartbeat_payload payload is wrong".
* Real I/O on ``tmp_path``; the only stub is a tiny in-test
  ``_FakePane`` class implementing the pane-source duck-type protocol
  (this is not a mock — it's a small real implementation that records
  calls so the poller's wiring is observable).
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
    # Arrange
    pane = ""
    # Act
    state = classify_pane_state(pane)
    # Assert
    assert state == STATE_STARTING


def test_classify_whitespace_only_pane_returns_starting():
    # Arrange
    pane = "\n   \n\t\n"
    # Act
    state = classify_pane_state(pane)
    # Assert
    assert state == STATE_STARTING


def test_classify_working_marker_returns_working():
    # Arrange
    pane = "previous turn\nWorking… esc to interrupt"
    # Act
    state = classify_pane_state(pane)
    # Assert
    assert state == STATE_WORKING


def test_classify_ruminating_marker_returns_working():
    # Arrange
    pane = "previous turn\nRuminating… esc to interrupt"
    # Act
    state = classify_pane_state(pane)
    # Assert
    assert state == STATE_WORKING


def test_classify_ready_marker_returns_idle():
    # Arrange
    pane = "❯                  | bypass permissions  off"
    # Act
    state = classify_pane_state(pane)
    # Assert
    assert state == STATE_IDLE


def test_classify_ambiguous_returns_unknown():
    # Arrange
    pane = "random pane content with no markers\n\n"
    # Act
    state = classify_pane_state(pane)
    # Assert
    assert state == STATE_UNKNOWN


def test_classify_radio_selector_is_not_idle():
    """A pane showing a TUI radio prompt must NOT be classified idle."""
    # Arrange
    pane = "bypass permissions\nEnter to confirm · 1. yes"
    # Act
    state = classify_pane_state(pane)
    # Assert
    assert state != STATE_IDLE


# ---------------------------------------------------------------------------
# build_heartbeat_payload — required-field projections
# ---------------------------------------------------------------------------


def test_heartbeat_payload_pid_is_passed_through():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=4242, pane_content=pane, now=100.0)
    # Assert
    assert payload["pid"] == 4242


def test_heartbeat_payload_ts_reflects_now():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=4242, pane_content=pane, now=100.0)
    # Assert
    assert payload["ts"] == 100.0


def test_heartbeat_payload_state_reflects_classified_pane():
    # Arrange
    pane = "❯  | bypass permissions"  # idle marker
    # Act
    payload = build_heartbeat_payload(pid=4242, pane_content=pane, now=100.0)
    # Assert
    assert payload["state"] == STATE_IDLE


def test_heartbeat_payload_runtime_is_tmux():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=4242, pane_content=pane, now=100.0)
    # Assert
    assert payload["runtime"] == "tmux"


def test_heartbeat_payload_input_tokens_is_none():
    """The tmux driver has no SDK quota stream; mark token fields null."""
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=1, pane_content=pane)
    # Assert
    assert payload["input_tokens"] is None


def test_heartbeat_payload_output_tokens_is_none():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=1, pane_content=pane)
    # Assert
    assert payload["output_tokens"] is None


def test_heartbeat_payload_total_tokens_is_none():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=1, pane_content=pane)
    # Assert
    assert payload["total_tokens"] is None


def test_heartbeat_payload_started_at_is_preserved():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(
        pid=1, pane_content=pane, started_at=90.0, now=100.0
    )
    # Assert
    assert payload["started_at"] == 90.0


def test_heartbeat_payload_elapsed_is_now_minus_started_at():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(
        pid=1, pane_content=pane, started_at=90.0, now=100.0
    )
    # Assert
    assert payload["elapsed_s"] == 10.0


def test_heartbeat_payload_omits_elapsed_without_started_at():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=1, pane_content=pane)
    # Assert
    assert "elapsed_s" not in payload


def test_heartbeat_payload_omits_started_at_when_unspecified():
    # Arrange
    pane = "❯  | bypass permissions"
    # Act
    payload = build_heartbeat_payload(pid=1, pane_content=pane)
    # Assert
    assert "started_at" not in payload


# ---------------------------------------------------------------------------
# write_heartbeat
# ---------------------------------------------------------------------------


def test_write_heartbeat_returns_final_path(tmp_path: Path):
    # Arrange
    payload = {"pid": 1, "state": STATE_IDLE, "ts": 1.0, "runtime": "tmux"}
    # Act
    final = write_heartbeat(tmp_path, payload)
    # Assert
    assert final == tmp_path / "heartbeat.json"


def test_write_heartbeat_persists_payload_to_disk(tmp_path: Path):
    # Arrange
    payload = {"pid": 1, "state": STATE_IDLE, "ts": 1.0, "runtime": "tmux"}
    # Act
    final = write_heartbeat(tmp_path, payload)
    # Assert
    assert json.loads(final.read_text()) == payload


def test_write_heartbeat_second_write_replaces_first(tmp_path: Path):
    """A second write must not leave stale content in heartbeat.json."""
    # Arrange
    payload1 = {"pid": 1, "ts": 1.0, "state": STATE_IDLE, "runtime": "tmux"}
    payload2 = {"pid": 2, "ts": 2.0, "state": STATE_WORKING, "runtime": "tmux"}
    write_heartbeat(tmp_path, payload1)
    # Act
    write_heartbeat(tmp_path, payload2)
    # Assert
    assert json.loads((tmp_path / "heartbeat.json").read_text()) == payload2


def test_write_heartbeat_leaves_no_stale_tmp_sidecar(tmp_path: Path):
    """The atomic-replace pattern must clean up its rename source."""
    # Arrange
    payload = {"pid": 1, "ts": 1.0, "state": STATE_IDLE, "runtime": "tmux"}
    # Act
    write_heartbeat(tmp_path, payload)
    # Assert
    assert not (tmp_path / "heartbeat.json.tmp").exists()


# ---------------------------------------------------------------------------
# poll_once integration
# ---------------------------------------------------------------------------


class _FakePane:
    """In-test pane-source: a tiny real class implementing the duck-type
    protocol the poller exercises (``capture_pane(session) -> str``).

    Not a mock — it records inputs so the poller's session-name wiring
    is asserted via observed call args, and returns a fixed content
    string so the classifier gets a deterministic input.
    """

    def __init__(self, content: str):
        self.content = content
        self.calls: list[str] = []

    def capture_pane(self, session: str) -> str:
        self.calls.append(session)
        return self.content


class _BrokenPane:
    """Pane-source that simulates an exploded ``tmux capture-pane``."""

    def capture_pane(self, session: str) -> str:
        raise RuntimeError("simulated tmux explosion")


def test_poll_once_payload_state_is_classifier_output(tmp_path: Path):
    # Arrange
    pane = _FakePane("Working… please wait")
    # Act
    payload = poll_once(
        pane_source=pane, session="sac-demo", state_dir=tmp_path, pid=999
    )
    # Assert
    assert payload["state"] == STATE_WORKING


def test_poll_once_payload_pid_is_passed_through(tmp_path: Path):
    # Arrange
    pane = _FakePane("Working… please wait")
    # Act
    payload = poll_once(
        pane_source=pane, session="sac-demo", state_dir=tmp_path, pid=999
    )
    # Assert
    assert payload["pid"] == 999


def test_poll_once_writes_state_to_disk(tmp_path: Path):
    # Arrange
    pane = _FakePane("Working… please wait")
    # Act
    poll_once(pane_source=pane, session="sac-demo", state_dir=tmp_path, pid=999)
    # Assert
    on_disk = json.loads((tmp_path / "heartbeat.json").read_text())
    assert on_disk["state"] == STATE_WORKING


def test_poll_once_calls_capture_pane_with_session_name(tmp_path: Path):
    # Arrange
    pane = _FakePane("❯  | bypass permissions")
    # Act
    poll_once(pane_source=pane, session="sac-y", state_dir=tmp_path, pid=1)
    # Assert
    assert pane.calls == ["sac-y"]


def test_poll_once_emits_starting_when_capture_returns_empty(tmp_path: Path):
    # Arrange
    pane = _FakePane("")
    # Act
    payload = poll_once(pane_source=pane, session="sac-x", state_dir=tmp_path, pid=1)
    # Assert
    assert payload["state"] == STATE_STARTING


def test_poll_once_survives_pane_capture_failure_state(tmp_path: Path):
    """A best-effort poller must NEVER raise; degraded payload is fine."""
    # Arrange
    pane = _BrokenPane()
    # Act
    payload = poll_once(pane_source=pane, session="sac-bad", state_dir=tmp_path, pid=1)
    # Assert — capture failed → empty content → classifier returns starting.
    assert payload["state"] == STATE_STARTING


def test_poll_once_survives_pane_capture_failure_writes_file(tmp_path: Path):
    """File must still be written despite the inner capture failure."""
    # Arrange
    pane = _BrokenPane()
    # Act
    poll_once(pane_source=pane, session="sac-bad", state_dir=tmp_path, pid=1)
    # Assert
    assert (tmp_path / "heartbeat.json").exists()
