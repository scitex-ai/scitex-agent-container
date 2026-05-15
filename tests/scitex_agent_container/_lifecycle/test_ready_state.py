"""Tests for ready_state.wait_for_ready (todo#291)."""

from __future__ import annotations

import subprocess

import pytest

from scitex_agent_container._lifecycle.ready_state import wait_for_ready


class FakeClock:
    """Deterministic clock: every call to now() advances by ``step``."""

    def __init__(self, step: float = 0.5):
        self.t = 0.0
        self.step = step

    def now(self) -> float:
        return self.t

    def sleep(self, amount: float) -> None:
        self.t += amount


class SeqCapture:
    """Return a predefined sequence of captures, then stick on the last."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def __call__(self, pane: str) -> str:
        self.calls += 1
        if self.seq:
            item = self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
            return item
        return ""


def _run(
    captures,
    patterns,
    *,
    idle_ticks=3,
    timeout=10.0,
    poll_interval=0.5,
    capture_callback=None,
):
    clock = FakeClock(step=poll_interval)
    cap = SeqCapture(captures)
    ok = wait_for_ready(
        agent_name="test",
        pane_target="cld-test",
        patterns=patterns,
        idle_ticks=idle_ticks,
        poll_interval=poll_interval,
        timeout=timeout,
        capture_callback=capture_callback,
        capture_fn=cap,
        time_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    return ok, cap, clock


READY_PANE = "Welcome to Claude Code!\ncwd: /tmp\n\n> "
READY_PATTERNS = ["Welcome to Claude Code", r"^> $"]


def test_empty_ready_patterns_skips_wait():
    # Arrange
    no_patterns: list[str] = []
    # Act
    ok = wait_for_ready(
        agent_name="a",
        pane_target="p",
        patterns=no_patterns,
        capture_fn=lambda p: "",
        time_fn=lambda: 0.0,
        sleep_fn=lambda s: None,
    )
    # Assert
    assert ok is True


def test_ready_detected_when_all_patterns_match_and_idle_returns_true():
    # Arrange
    captures = [READY_PANE, READY_PANE, READY_PANE]
    # Act
    ok, _cap, _clock = _run(captures=captures, patterns=READY_PATTERNS, idle_ticks=3)
    # Assert
    assert ok is True


def test_ready_detected_when_all_patterns_match_and_idle_consumes_three_captures():
    # Arrange
    captures = [READY_PANE, READY_PANE, READY_PANE]
    # Act
    _ok, cap, _clock = _run(captures=captures, patterns=READY_PATTERNS, idle_ticks=3)
    # Assert
    assert cap.calls == 3


def test_ready_detected_waits_for_idle_window_returns_true():
    # Arrange
    boot1 = "starting...\n"
    boot2 = "loading mcp...\n"
    captures = [boot1, boot2, READY_PANE, READY_PANE, READY_PANE]
    # Act
    ok, _cap, _clock = _run(
        captures=captures,
        patterns=READY_PATTERNS,
        idle_ticks=3,
        timeout=60.0,
    )
    # Assert
    assert ok is True


def test_ready_detected_waits_for_idle_window_polls_until_idle_window_complete():
    # Arrange: first 2 captures change, then 3 identical ready captures.
    boot1 = "starting...\n"
    boot2 = "loading mcp...\n"
    captures = [boot1, boot2, READY_PANE, READY_PANE, READY_PANE]
    # Act
    _ok, cap, _clock = _run(
        captures=captures,
        patterns=READY_PATTERNS,
        idle_ticks=3,
        timeout=60.0,
    )
    # Assert
    assert cap.calls == 5


def test_timeout_when_patterns_never_match_returns_false():
    # Arrange
    calls: list[str] = []
    # Act
    ok, _cap, _clock = _run(
        captures=["still booting\n"],
        patterns=["Welcome to Claude Code"],
        timeout=2.0,
        poll_interval=0.5,
        capture_callback=lambda tail: calls.append(tail),
    )
    # Assert
    assert ok is False


def test_timeout_when_patterns_never_match_invokes_capture_callback():
    # Arrange
    calls: list[str] = []
    # Act
    _run(
        captures=["still booting\n"],
        patterns=["Welcome to Claude Code"],
        timeout=2.0,
        poll_interval=0.5,
        capture_callback=lambda tail: calls.append(tail),
    )
    # Assert
    assert calls and "still booting" in calls[0]


@pytest.mark.parametrize(
    "case_id, captures, timeout",
    [
        (
            "partial_banner_only_no_prompt",
            ["Welcome to Claude Code!\nloading...\n"] * 3,
            3.0,
        ),
        (
            "banner_scrolled_off_tail",
            ["\n".join([f"line {i}" for i in range(100)]) + "\n> "] * 3,
            5.0,
        ),
    ],
)
def test_incomplete_pattern_match_does_not_fire_ready(case_id, captures, timeout):
    # Arrange
    patterns = READY_PATTERNS
    # Act
    ok, _cap, _clock = _run(captures=captures, patterns=patterns, timeout=timeout)
    # Assert
    assert ok is False


def _make_bad_capture(counter: dict):
    def bad_capture(pane: str) -> str:
        counter["n"] += 1
        if counter["n"] == 1:
            raise subprocess.CalledProcessError(1, ["tmux"])
        return READY_PANE

    return bad_capture


def test_subprocess_error_treated_as_no_match_returns_true_once_recovered():
    # Arrange
    clock = FakeClock(step=0.5)
    errors = {"n": 0}
    bad_capture = _make_bad_capture(errors)
    # Act
    ok = wait_for_ready(
        agent_name="t",
        pane_target="p",
        patterns=READY_PATTERNS,
        idle_ticks=3,
        poll_interval=0.5,
        timeout=10.0,
        capture_fn=bad_capture,
        time_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    # Assert
    assert ok is True


def test_subprocess_error_treated_as_no_match_continues_polling_after_error():
    # Arrange
    clock = FakeClock(step=0.5)
    errors = {"n": 0}
    bad_capture = _make_bad_capture(errors)
    # Act
    wait_for_ready(
        agent_name="t",
        pane_target="p",
        patterns=READY_PATTERNS,
        idle_ticks=3,
        poll_interval=0.5,
        timeout=10.0,
        capture_fn=bad_capture,
        time_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    # Assert: first error plus 3 idle matches
    assert errors["n"] >= 4
