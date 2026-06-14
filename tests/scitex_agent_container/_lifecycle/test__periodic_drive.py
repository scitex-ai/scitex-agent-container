"""Tests for the unified periodic-drive lane (SDK + TUI autonomy).

Lead a2a ``4973264a`` / ``12a0d8f6`` / ``3afbc1bd`` (2026-06-14):
research agents need to keep working between driven turns. This
lane fires periodic system-message turns into each running
agent's inbox. The TUI runtime consumes them via
``send_turn``; the SDK runtime via its message loop. ONE
mechanism, both runtimes.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks; real
dataclasses + a list-appender for the emit side-effect.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._lifecycle._periodic_drive import (
    DEFAULT_INTERVAL_S,
    ENVELOPE_KIND,
    PeriodicDriveEnvelope,
    _AgentState,
    build_envelope,
    is_globally_disabled,
    should_drive,
    sweep,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env():
    """Save/restore the global disable env var so a leak doesn't
    cross tests."""
    saved = os.environ.get("SAC_PERIODIC_DRIVE_DISABLED")
    os.environ.pop("SAC_PERIODIC_DRIVE_DISABLED", None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_PERIODIC_DRIVE_DISABLED", None)
        else:
            os.environ["SAC_PERIODIC_DRIVE_DISABLED"] = saved


def _running_agent(
    *,
    name: str = "alpha",
    last_drive_at: float = 0.0,
    interval_s: float = 60.0,
    enabled: bool = True,
    is_running: bool = True,
) -> _AgentState:
    """Construct a minimal running-agent state for the predicate +
    sweep tests."""
    return _AgentState(
        name=name,
        is_running=is_running,
        workdir="/work/agents/" + name,
        branch="feat/" + name,
        last_commit_subject="wip: scaffolding",
        worktree_name="wt-" + name,
        standing_rules="Run TDD; no mocks; AAA markers.",
        mission="Monitor Spartan + resubmit chains.",
        last_drive_at=last_drive_at,
        interval_s=interval_s,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# build_envelope — content shape
# ---------------------------------------------------------------------------


class TestBuildEnvelopeShape:
    """The rendered envelope carries the spec + state.db fields the
    operator's doctrine mandates."""

    def test_envelope_kind_is_constant(self) -> None:
        # Arrange
        state = _running_agent()
        # Act
        env = build_envelope(state, now=1000.0)
        # Assert
        assert env.kind == ENVELOPE_KIND

    def test_envelope_body_contains_agent_name(self) -> None:
        # Arrange
        state = _running_agent(name="ripple-wm")
        # Act
        env = build_envelope(state, now=1000.0)
        # Assert
        assert "ripple-wm" in env.body

    def test_envelope_body_contains_branch(self) -> None:
        # Arrange
        state = _running_agent()
        state = _AgentState(**{**state.__dict__, "branch": "fix/cred-rotation"})
        # Act
        env = build_envelope(state, now=1000.0)
        # Assert
        assert "fix/cred-rotation" in env.body

    def test_envelope_body_contains_active_worktree(self) -> None:
        # Arrange
        state = _running_agent()
        state = _AgentState(**{**state.__dict__, "worktree_name": "wt-spartan"})
        # Act
        env = build_envelope(state, now=1000.0)
        # Assert
        assert "wt-spartan" in env.body

    def test_envelope_carries_generated_at_timestamp(self) -> None:
        # Arrange
        state = _running_agent()
        # Act
        env = build_envelope(state, now=12345.6)
        # Assert
        assert env.generated_at == 12345.6


# ---------------------------------------------------------------------------
# should_drive — predicate
# ---------------------------------------------------------------------------


class TestShouldDriveOptIn:
    """A spec-disabled agent is never driven."""

    def test_disabled_agent_not_driven(self) -> None:
        # Arrange
        state = _running_agent(enabled=False)
        # Act
        decision = should_drive(state, now=10_000.0)
        # Assert
        assert decision is False

    def test_stopped_agent_not_driven(self) -> None:
        # Arrange
        state = _running_agent(is_running=False)
        # Act
        decision = should_drive(state, now=10_000.0)
        # Assert
        assert decision is False


class TestShouldDriveRateLimit:
    """Per-agent rate-limit via ``last_drive_at + interval_s``."""

    def test_fresh_agent_driven_immediately(self) -> None:
        # Arrange — last_drive_at=0 means never driven; due now.
        state = _running_agent(last_drive_at=0.0, interval_s=60.0)
        # Act
        decision = should_drive(state, now=10_000.0)
        # Assert
        assert decision is True

    def test_recently_driven_agent_skipped(self) -> None:
        # Arrange — driven 30s ago with interval 60s → not due.
        state = _running_agent(last_drive_at=9_970.0, interval_s=60.0)
        # Act
        decision = should_drive(state, now=10_000.0)
        # Assert
        assert decision is False

    def test_due_agent_driven(self) -> None:
        # Arrange — driven 61s ago with interval 60s → due.
        state = _running_agent(last_drive_at=9_939.0, interval_s=60.0)
        # Act
        decision = should_drive(state, now=10_000.0)
        # Assert
        assert decision is True


# ---------------------------------------------------------------------------
# is_globally_disabled — env escape hatch
# ---------------------------------------------------------------------------


class TestGlobalDisable:
    """Operator-side fleet-emergency pause."""

    def test_default_env_not_disabled(self) -> None:
        # Arrange
        env: dict[str, str] = {}
        # Act
        result = is_globally_disabled(env)
        # Assert
        assert result is False

    def test_env_set_to_1_is_disabled(self) -> None:
        # Arrange
        env = {"SAC_PERIODIC_DRIVE_DISABLED": "1"}
        # Act
        result = is_globally_disabled(env)
        # Assert
        assert result is True

    def test_env_set_to_other_value_not_disabled(self) -> None:
        # Arrange — only "1" disables; anything else (including "0",
        # "true", "yes") is honoured as still-enabled to keep the
        # escape hatch unambiguous.
        env = {"SAC_PERIODIC_DRIVE_DISABLED": "true"}
        # Act
        result = is_globally_disabled(env)
        # Assert
        assert result is False


# ---------------------------------------------------------------------------
# sweep — fleet iteration + emit
# ---------------------------------------------------------------------------


class TestSweepEmits:
    """``sweep`` iterates the fleet and calls ``emit`` per due
    agent. Per-agent failures don't break the fleet sweep."""

    def test_sweep_emits_for_due_agent(self) -> None:
        # Arrange
        states = [_running_agent(name="alpha", last_drive_at=0.0)]
        captured: list[PeriodicDriveEnvelope] = []
        # Act
        sweep(states, emit=captured.append, now=10_000.0)
        # Assert
        assert len(captured) == 1

    def test_sweep_skips_not_due_agent(self) -> None:
        # Arrange
        states = [_running_agent(name="alpha", last_drive_at=9_990.0, interval_s=60.0)]
        captured: list[PeriodicDriveEnvelope] = []
        # Act
        sweep(states, emit=captured.append, now=10_000.0)
        # Assert
        assert captured == []

    def test_sweep_continues_when_one_emit_raises(self) -> None:
        # Arrange — first emit raises; second must still fire.
        states = [
            _running_agent(name="alpha"),
            _running_agent(name="beta"),
        ]
        bad_call_count = {"n": 0}

        def emit(env: PeriodicDriveEnvelope) -> None:
            bad_call_count["n"] += 1
            if env.agent_name == "alpha":
                raise RuntimeError("simulated emit failure")

        # Act
        sweep(states, emit=emit, now=10_000.0)
        # Assert — both agents were ATTEMPTED (alpha raised, beta succeeded).
        assert bad_call_count["n"] == 2

    def test_sweep_no_emit_when_globally_disabled(self) -> None:
        # Arrange
        os.environ["SAC_PERIODIC_DRIVE_DISABLED"] = "1"
        states = [_running_agent(name="alpha")]
        captured: list[PeriodicDriveEnvelope] = []
        # Act
        sweep(states, emit=captured.append, now=10_000.0)
        # Assert
        assert captured == []


class TestSweepReturnsEmittedList:
    """Caller gets the actual emitted envelopes for telemetry."""

    def test_return_list_length_matches_emit_count(self) -> None:
        # Arrange — 2 due agents.
        states = [
            _running_agent(name="alpha"),
            _running_agent(name="beta"),
        ]
        captured: list[PeriodicDriveEnvelope] = []
        # Act
        result = sweep(states, emit=captured.append, now=10_000.0)
        # Assert
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaultInterval:
    """Default per-agent interval is operator-tunable; the module
    constant is the documented default."""

    def test_default_interval_is_600s(self) -> None:
        # Arrange / Act
        # (no setup; assert on the module constant)
        # Assert — 10 minutes as documented in the module docstring.
        assert DEFAULT_INTERVAL_S == 600.0


# EOF
