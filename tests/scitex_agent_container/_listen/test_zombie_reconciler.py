"""Tests for the zombie-agent reconciler (detect + clear stale leases).

Operator directive (2026-07-01): after a SIF rebuild / listen restart an
agent can be a ZOMBIE — the registry marks it ``running`` but no live
apptainer container backs it. The reconciler must DETECT + CLEAR those so
a normal restart relaunches them.

No mocks (STX-NM002): every collaborator seam is injected as a plain
lambda / dict-backed closure. The pure rule (``find_zombie_agents``) is
exercised with a dict oracle; ``reconcile_zombies`` records its
``clear_lease`` calls via an ordinary list closure. The async loop wiring
is exercised with a plain synchronous ``reconcile_once`` callable.

STX-TQ002 AAA-markers + STX-TQ007 one-assert.
"""

from __future__ import annotations

import asyncio

import pytest

from scitex_agent_container._listen._zombie_reconciler import (
    find_zombie_agents,
    reconcile_zombies,
    zombie_reconciler_loop,
)


# ---------------------------------------------------------------------------
# find_zombie_agents — pure rule
# ---------------------------------------------------------------------------


class TestFindZombieAgents:
    def test_live_container_agent_is_not_flagged(self) -> None:
        # Arrange — one running agent whose container the oracle reports alive.
        alive = {"agent-live": True}
        # Act
        zombies = find_zombie_agents(
            ["agent-live"], is_container_alive=lambda n: alive.get(n, False)
        )
        # Assert
        assert zombies == []

    def test_no_container_running_agent_is_flagged(self) -> None:
        # Arrange — a running agent whose container the oracle reports dead.
        alive = {"agent-dead": False}
        # Act
        zombies = find_zombie_agents(
            ["agent-dead"], is_container_alive=lambda n: alive.get(n, False)
        )
        # Assert
        assert zombies == ["agent-dead"]

    def test_agent_not_in_running_set_is_never_flagged(self) -> None:
        # Arrange — the oracle would say dead, but the name is not running.
        running: list[str] = []
        # Act
        zombies = find_zombie_agents(running, is_container_alive=lambda n: False)
        # Assert
        assert zombies == []

    def test_empty_input_yields_empty(self) -> None:
        # Arrange
        running: list[str] = []
        # Act
        zombies = find_zombie_agents(running, is_container_alive=lambda n: True)
        # Assert
        assert zombies == []

    def test_only_the_dead_container_agent_is_flagged_among_many(self) -> None:
        # Arrange — mixed set: one live container, one dead container.
        alive = {"agent-live": True, "agent-dead": False}
        # Act
        zombies = find_zombie_agents(
            ["agent-live", "agent-dead"],
            is_container_alive=lambda n: alive.get(n, False),
        )
        # Assert
        assert zombies == ["agent-dead"]


# ---------------------------------------------------------------------------
# reconcile_zombies — detect + clear, collaborators injected as seams
# ---------------------------------------------------------------------------


class TestReconcileZombies:
    def test_returns_cleared_zombie_names(self) -> None:
        # Arrange — one running agent with a dead container.
        alive = {"agent-dead": False}
        # Act
        cleared = reconcile_zombies(
            running_oracle=lambda: ["agent-dead"],
            is_container_alive=lambda n: alive.get(n, False),
            clear_lease=lambda n: None,
        )
        # Assert
        assert cleared == ["agent-dead"]

    def test_clear_lease_called_exactly_for_zombies(self) -> None:
        # Arrange — record every clear_lease call; one live + one dead agent.
        calls: list[str] = []
        alive = {"agent-live": True, "agent-dead": False}
        # Act
        reconcile_zombies(
            running_oracle=lambda: ["agent-live", "agent-dead"],
            is_container_alive=lambda n: alive.get(n, False),
            clear_lease=calls.append,
        )
        # Assert
        assert calls == ["agent-dead"]

    def test_live_container_agent_is_not_cleared(self) -> None:
        # Arrange — the only running agent has a live container.
        # Act
        cleared = reconcile_zombies(
            running_oracle=lambda: ["agent-live"],
            is_container_alive=lambda n: True,
            clear_lease=lambda n: None,
        )
        # Assert
        assert cleared == []

    def test_empty_running_set_clears_nothing(self) -> None:
        # Arrange
        calls: list[str] = []
        # Act
        reconcile_zombies(
            running_oracle=lambda: [],
            is_container_alive=lambda n: False,
            clear_lease=calls.append,
        )
        # Assert
        assert calls == []

    def test_failed_clear_lease_is_skipped_not_reported_cleared(self) -> None:
        # Arrange — clear_lease raises; the name must NOT count as cleared.
        def boom(_name: str) -> None:
            raise RuntimeError("state.db locked")

        # Act
        cleared = reconcile_zombies(
            running_oracle=lambda: ["agent-dead"],
            is_container_alive=lambda n: False,
            clear_lease=boom,
        )
        # Assert
        assert cleared == []


# ---------------------------------------------------------------------------
# zombie_reconciler_loop — async wiring (test seam: plain reconcile_once)
# ---------------------------------------------------------------------------


class TestZombieReconcilerLoop:
    def test_loop_runs_the_injected_reconcile_pass_each_tick(self) -> None:
        # Arrange — a plain sync reconcile_once that records it ran.
        ran: list[str] = []

        async def drive() -> None:
            task = asyncio.create_task(
                zombie_reconciler_loop(
                    interval_s=100.0,
                    reconcile_once=lambda: ran.append("tick") or [],
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Act
        asyncio.run(drive())
        # Assert
        assert ran == ["tick"]

    def test_loop_cancellation_is_propagated_cleanly(self) -> None:
        # Arrange — a loop with a never-elapsing interval, then cancel it.
        async def drive() -> None:
            task = asyncio.create_task(
                zombie_reconciler_loop(interval_s=100.0, reconcile_once=lambda: [])
            )
            await asyncio.sleep(0.05)
            task.cancel()
            await task

        # Act
        run = lambda: asyncio.run(drive())
        # Assert
        with pytest.raises(asyncio.CancelledError):
            run()
