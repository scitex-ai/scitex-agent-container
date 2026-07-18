"""Tests for the deploy-freshness reconciler (checkout-vs-origin staleness).

No mocks / no monkeypatch — every collaborator is a hand-rolled plain seam
(a lambda, a ``list.append`` recorder, a dict-lookup closure). AAA markers,
one assert per test.
"""

from __future__ import annotations

import asyncio

from scitex_agent_container._listen._deploy_freshness import (
    CRITICAL_BEHIND,
    build_staleness_alarm,
    deploy_freshness_loop,
    reconcile_deploy_freshness,
)


class TestBuildStalenessAlarm:
    def test_fresh_checkout_yields_no_alarm(self) -> None:
        # Arrange
        behind = 0
        # Act
        alarm = build_staleness_alarm(behind, [])
        # Assert
        assert alarm is None

    def test_negative_count_yields_no_alarm(self) -> None:
        # Arrange — a degraded git read resolved to a negative sentinel.
        behind = -3
        # Act
        alarm = build_staleness_alarm(behind, [])
        # Assert
        assert alarm is None

    def test_behind_reports_the_count(self) -> None:
        # Arrange
        behind = 4
        # Act
        alarm = build_staleness_alarm(behind, ["abc fix", "def feat"])
        # Assert
        assert alarm["commits_behind"] == 4

    def test_behind_carries_the_subjects(self) -> None:
        # Arrange
        subjects = ["abc fix a", "def feat b"]
        # Act
        alarm = build_staleness_alarm(2, subjects)
        # Assert
        assert alarm["newest_subjects"] == subjects

    def test_below_threshold_is_warning(self) -> None:
        # Arrange
        behind = CRITICAL_BEHIND - 1
        # Act
        alarm = build_staleness_alarm(behind, [])
        # Assert
        assert alarm["severity"] == "warning"

    def test_at_threshold_is_critical(self) -> None:
        # Arrange
        behind = CRITICAL_BEHIND
        # Act
        alarm = build_staleness_alarm(behind, [])
        # Assert
        assert alarm["severity"] == "critical"


class TestReconcileDeployFreshness:
    def test_fresh_returns_none(self) -> None:
        # Arrange — a fresh checkout, an emit recorder that must stay empty.
        emitted: list = []
        # Act
        result = reconcile_deploy_freshness(
            count_behind=lambda: (0, []), emit=emitted.append
        )
        # Assert
        assert result is None

    def test_fresh_never_emits(self) -> None:
        # Arrange
        emitted: list = []
        # Act
        reconcile_deploy_freshness(count_behind=lambda: (0, []), emit=emitted.append)
        # Assert
        assert emitted == []

    def test_stale_emits_exactly_once(self) -> None:
        # Arrange
        emitted: list = []
        # Act
        reconcile_deploy_freshness(
            count_behind=lambda: (5, ["a", "b"]), emit=emitted.append
        )
        # Assert
        assert len(emitted) == 1

    def test_stale_emits_the_alarm_payload(self) -> None:
        # Arrange
        emitted: list = []
        # Act
        reconcile_deploy_freshness(
            count_behind=lambda: (5, ["a", "b"]), emit=emitted.append
        )
        # Assert
        assert emitted[0]["commits_behind"] == 5

    def test_stale_returns_the_alarm(self) -> None:
        # Arrange
        emitted: list = []
        # Act
        result = reconcile_deploy_freshness(
            count_behind=lambda: (5, ["a"]), emit=emitted.append
        )
        # Assert
        assert result["commits_behind"] == 5


class TestDeployFreshnessLoop:
    def test_loop_runs_a_tick_then_cancels_cleanly(self) -> None:
        # Arrange — a recording pass; a driver that lets one tick fire.
        ticks: list = []

        async def _drive() -> None:
            task = asyncio.create_task(
                deploy_freshness_loop(
                    interval_s=0.01, reconcile_once=lambda: ticks.append(1)
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Act
        asyncio.run(_drive())
        # Assert
        assert ticks
