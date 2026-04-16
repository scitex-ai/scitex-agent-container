"""Tests for ready_state.wait_for_ready (todo#291)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from scitex_agent_container.ready_state import wait_for_ready


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


READY_PANE = (
    "Welcome to Claude Code!\n"
    "cwd: /tmp\n"
    "\n"
    "> "
)


def test_empty_ready_patterns_skips_wait():
    ok = wait_for_ready(
        agent_name="a",
        pane_target="p",
        patterns=[],
        capture_fn=lambda p: "",
        time_fn=lambda: 0.0,
        sleep_fn=lambda s: None,
    )
    assert ok is True


def test_ready_detected_when_all_patterns_match_and_idle():
    ok, cap, _ = _run(
        captures=[READY_PANE, READY_PANE, READY_PANE],
        patterns=["Welcome to Claude Code", r"^> $"],
        idle_ticks=3,
    )
    assert ok is True
    assert cap.calls == 3


def test_ready_detected_waits_for_idle_window():
    boot1 = "starting...\n"
    boot2 = "loading mcp...\n"
    ok, cap, _ = _run(
        captures=[boot1, boot2, READY_PANE, READY_PANE, READY_PANE],
        patterns=["Welcome to Claude Code", r"^> $"],
        idle_ticks=3,
        timeout=60.0,
    )
    assert ok is True
    # First 2 captures changed, then 3 identical ready captures.
    assert cap.calls == 5


def test_timeout_when_patterns_never_match():
    calls: list[str] = []
    ok, cap, clock = _run(
        captures=["still booting\n"],
        patterns=["Welcome to Claude Code"],
        timeout=2.0,
        poll_interval=0.5,
        capture_callback=lambda tail: calls.append(tail),
    )
    assert ok is False
    assert calls, "capture_callback must fire on timeout"
    assert "still booting" in calls[0]


def test_partial_match_does_not_fire():
    # Only the banner matches, not the prompt
    partial = "Welcome to Claude Code!\nloading...\n"
    ok, _, _ = _run(
        captures=[partial, partial, partial],
        patterns=["Welcome to Claude Code", r"^> $"],
        timeout=3.0,
    )
    assert ok is False


def test_tail_only_matching():
    # Banner was in an OLD capture but scrolled off by the time pane is idle.
    scrolled = "\n".join([f"line {i}" for i in range(100)]) + "\n> "
    ok, _, _ = _run(
        captures=[scrolled, scrolled, scrolled],
        patterns=["Welcome to Claude Code", r"^> $"],
        timeout=5.0,
    )
    # Banner not in last 40 lines → not ready.
    assert ok is False


def test_subprocess_error_treated_as_no_match():
    clock = FakeClock(step=0.5)

    errors = {"n": 0}

    def bad_capture(pane: str) -> str:
        errors["n"] += 1
        if errors["n"] == 1:
            raise subprocess.CalledProcessError(1, ["tmux"])
        return READY_PANE

    ok = wait_for_ready(
        agent_name="t",
        pane_target="p",
        patterns=["Welcome to Claude Code", r"^> $"],
        idle_ticks=3,
        poll_interval=0.5,
        timeout=10.0,
        capture_fn=bad_capture,
        time_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    assert ok is True
    assert errors["n"] >= 4  # first error plus 3 idle matches


def test_on_timeout_capture_and_fail_raises_or_returns_failure(tmp_path, monkeypatch):
    """Integration: claude_code._wait_for_ready_state returns False on strict timeout."""
    from scitex_agent_container.config._types import (
        AgentConfig,
        ReadyPattern,
        StartupSpec,
    )
    from scitex_agent_container.runtimes.claude_code import ClaudeCodeRuntime

    cfg = AgentConfig(name="timeout-fail")
    cfg.startup = StartupSpec(
        ready_patterns=[ReadyPattern(regex="NEVER_MATCHES")],
        ready_idle_ticks=1,
        ready_poll_interval_seconds=0.01,
        ready_timeout_seconds=0.05,
        on_timeout="capture_and_fail",
    )

    runtime = ClaudeCodeRuntime()
    fake_mux = MagicMock()
    fake_mux.capture_content.return_value = "nothing useful"
    monkeypatch.setattr(runtime, "_get_mux", lambda c: fake_mux)

    # Redirect log dir to tmp
    monkeypatch.setenv("HOME", str(tmp_path))

    proceed = runtime._wait_for_ready_state(cfg)
    assert proceed is False
    # Boot capture written to tmp HOME
    log_dir = tmp_path / ".scitex" / "agent-container" / "logs" / "timeout-fail"
    assert log_dir.exists()
    captures = list(log_dir.glob("boot-capture-*.txt"))
    assert captures, "expected a boot capture file"


def test_on_timeout_capture_and_proceed_continues(tmp_path, monkeypatch):
    from scitex_agent_container.config._types import (
        AgentConfig,
        ReadyPattern,
        StartupSpec,
    )
    from scitex_agent_container.runtimes.claude_code import ClaudeCodeRuntime

    cfg = AgentConfig(name="timeout-proceed")
    cfg.startup = StartupSpec(
        ready_patterns=[ReadyPattern(regex="NEVER_MATCHES")],
        ready_idle_ticks=1,
        ready_poll_interval_seconds=0.01,
        ready_timeout_seconds=0.05,
        on_timeout="capture_and_proceed",
    )

    runtime = ClaudeCodeRuntime()
    fake_mux = MagicMock()
    fake_mux.capture_content.return_value = "nothing"
    monkeypatch.setattr(runtime, "_get_mux", lambda c: fake_mux)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert runtime._wait_for_ready_state(cfg) is True


def test_wait_for_ready_state_no_patterns_is_noop(monkeypatch):
    """Legacy configs (no spec.startup block) skip the poll loop entirely."""
    from scitex_agent_container.config._types import AgentConfig
    from scitex_agent_container.runtimes.claude_code import ClaudeCodeRuntime

    cfg = AgentConfig(name="legacy")
    # Default StartupSpec has no patterns.
    runtime = ClaudeCodeRuntime()

    called = {"n": 0}

    def should_not_be_called(c):
        called["n"] += 1
        raise AssertionError("mux should not be consulted when patterns empty")

    monkeypatch.setattr(runtime, "_get_mux", should_not_be_called)
    assert runtime._wait_for_ready_state(cfg) is True
    assert called["n"] == 0
