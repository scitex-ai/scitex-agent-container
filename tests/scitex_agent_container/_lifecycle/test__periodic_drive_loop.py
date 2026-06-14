"""Tests for the listen-loop integration of the periodic-drive lane.

Lead a2a ``7916f486929f44fb92d3aa1571cfa1d0`` (2026-06-14): the
runtime-agnostic core landed in PR #404. This test suite exercises
the asyncio-task that the listen server lifespan launches at boot
— `periodic_drive_loop` — without standing up the real Starlette
app. We inject an in-memory ``app_state`` carrying a stub Broker
and a one-shot ``agents_source`` so the loop body runs through
deterministically + cancellation is honoured.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from scitex_agent_container._lifecycle._periodic_drive import _AgentState
from scitex_agent_container._lifecycle._periodic_drive_loop import (
    DEFAULT_TICK_INTERVAL_S,
    periodic_drive_loop,
)

# ---------------------------------------------------------------------------
# Stub Broker — captures published events so the test can assert
# the loop's emit hook landed something. Real Broker is async (see
# ``a2a/_inbox_bus.py``); we match its ``publish(name, event)`` shape.
# ---------------------------------------------------------------------------


class _StubBroker:
    """In-memory replacement for ``a2a._inbox_bus.Broker``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, agent: str, event: dict[str, Any]) -> int:
        self.events.append((agent, event))
        return 1


class _AppState:
    """Minimal Starlette-state-equivalent the loop reads.

    Real ``app.state`` is a starlette.datastructures.State; the loop
    only touches ``.inbox`` and ``.registry`` so a bare class works.
    """

    def __init__(self, inbox: _StubBroker | None = None) -> None:
        self.inbox = inbox or _StubBroker()
        self.registry = None  # source disabled — tests inject agents_source


# ---------------------------------------------------------------------------
# default tick interval
# ---------------------------------------------------------------------------


class TestDefaultTickInterval:
    """The polling cadence default is documented in the module."""

    def test_default_tick_interval_is_60s(self) -> None:
        # Arrange / Act
        # (no setup — assert on the module constant)
        # Assert
        assert DEFAULT_TICK_INTERVAL_S == 60.0


# ---------------------------------------------------------------------------
# Loop body — one tick emits to the inbox for a due agent
# ---------------------------------------------------------------------------


def _due_agent(name: str = "alpha") -> _AgentState:
    return _AgentState(
        name=name,
        is_running=True,
        workdir="/work/" + name,
        branch="feat/" + name,
        last_commit_subject="wip",
        worktree_name="wt-" + name,
        standing_rules="rules",
        mission="mission",
        last_drive_at=0.0,  # fresh → due
        interval_s=60.0,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_loop_emits_drive_envelope_to_inbox() -> None:
    # Arrange
    state = _AppState()
    agents = [_due_agent("alpha")]
    # Act — spin the loop once via cancellation after one tick.
    task = asyncio.create_task(
        periodic_drive_loop(
            state,
            tick_interval_s=0.05,
            agents_source=agents,
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # The publish is scheduled via ``asyncio.create_task`` inside the
    # emit hook; give it one more loop iteration to drain.
    await asyncio.sleep(0.05)
    # Assert — broker received the envelope.
    assert len(state.inbox.events) >= 1


@pytest.mark.asyncio
async def test_loop_emit_envelope_kind_is_periodic_drive() -> None:
    # Arrange
    state = _AppState()
    agents = [_due_agent("alpha")]
    # Act
    task = asyncio.create_task(
        periodic_drive_loop(
            state,
            tick_interval_s=0.05,
            agents_source=agents,
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.05)
    # Assert — payload discriminator carries the canonical kind.
    _, event = state.inbox.events[0]
    assert event["kind"] == "periodic_drive"


@pytest.mark.asyncio
async def test_loop_emit_targets_the_due_agent_name() -> None:
    # Arrange
    state = _AppState()
    agents = [_due_agent("ripple-wm")]
    # Act
    task = asyncio.create_task(
        periodic_drive_loop(
            state,
            tick_interval_s=0.05,
            agents_source=agents,
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.05)
    # Assert — recipient was the named agent.
    agent_name, _ = state.inbox.events[0]
    assert agent_name == "ripple-wm"


# ---------------------------------------------------------------------------
# Cancellation — SIGTERM-equivalent propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_honours_cancellation_cleanly() -> None:
    # Arrange
    state = _AppState()
    agents = [_due_agent("alpha")]
    task = asyncio.create_task(
        periodic_drive_loop(
            state,
            tick_interval_s=0.05,
            agents_source=agents,
        )
    )
    # Act — cancel + await; the loop's ``finally`` must not raise.
    await asyncio.sleep(0.06)
    task.cancel()
    # Assert
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Global disable — env escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_skips_emit_when_globally_disabled(monkeypatch) -> None:
    # Arrange — the sweep helper short-circuits when the env is set.
    monkeypatch.setenv("SAC_PERIODIC_DRIVE_DISABLED", "1")
    state = _AppState()
    agents = [_due_agent("alpha")]
    # Act
    task = asyncio.create_task(
        periodic_drive_loop(
            state,
            tick_interval_s=0.05,
            agents_source=agents,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.05)
    # Assert — no events landed in the broker.
    assert state.inbox.events == []


# EOF
