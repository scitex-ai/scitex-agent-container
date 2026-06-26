"""Tests for the liveness-tick reconciler loop + graceful bus emit.

Card ``sac-card-anchored-stop-reconciler``. We exercise the asyncio task
the ``sac listen`` lifespan launches — ``liveness_tick_reconciler_loop``
— WITHOUT standing up the real Starlette app, by injecting a real
in-memory tasks doc + real :class:`AgentLiveness` map + a REAL local
consumer list. No mocks (STX-NM002): the "consumer" is an ordinary
module-level callable that appends to a list; the throwing consumer is an
ordinary callable that raises.

The IO resolvers (``_load_tasks_doc`` / ``_session_last_active_ts``) are
exercised against REAL files under ``tmp_path``.

STX-TQ002 AAA-markers + STX-TQ007 one-assert.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from scitex_agent_container._listen._liveness_tick import (
    DEFAULT_INTERVAL_S,
    DEFAULT_RENOTIFY_S,
    DEFAULT_STALE_S,
    HOOKS_ENTRY_POINT_GROUP,
    AgentLiveness,
    emit_anomaly,
    liveness_tick_reconciler_loop,
)
from scitex_agent_container._listen import _liveness_tick as mod

NOW = 2_000_000.0
STALE_S = 900.0


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _stuck_doc() -> dict:
    """A tasks doc with exactly one OPEN, unblocked, stale card."""
    return {
        "tasks": [
            {
                "id": "card-stuck",
                "status": "in_progress",
                "assignee": "agent-x",
                "last_activity": _iso(NOW - STALE_S - 60.0),
            }
        ]
    }


# ===========================================================================
# constants / wiring
# ===========================================================================


class TestDefaults:
    def test_default_interval_is_120s(self) -> None:
        # Arrange
        constant = DEFAULT_INTERVAL_S
        # Act
        observed = constant
        # Assert
        assert observed == 120.0

    def test_default_stale_is_900s(self) -> None:
        # Arrange
        constant = DEFAULT_STALE_S
        # Act
        observed = constant
        # Assert
        assert observed == 900.0

    def test_default_renotify_is_3600s(self) -> None:
        # Arrange
        constant = DEFAULT_RENOTIFY_S
        # Act
        observed = constant
        # Assert
        assert observed == 3600.0

    def test_entry_point_group_is_scitex_todo_hooks(self) -> None:
        # Arrange
        constant = HOOKS_ENTRY_POINT_GROUP
        # Act
        observed = constant
        # Assert
        assert observed == "scitex_todo.hooks"


# ===========================================================================
# emit_anomaly — graceful delivery (real callables, no mocks)
# ===========================================================================


class TestEmitAnomaly:
    def test_event_is_delivered_to_a_real_consumer(self) -> None:
        # Arrange — a real local consumer that records what it received.
        received: list[dict] = []

        def consumer(event: dict) -> None:
            received.append(event)

        event = {
            "agent": "agent-x",
            "card_id": "card-1",
            "reason": "owner-not-live",
            "severity": "warning",
            "ts": NOW,
        }
        # Act
        emit_anomaly(event, [consumer])
        # Assert
        assert received == [event]

    def test_empty_consumer_list_delivers_to_nobody(self) -> None:
        # Arrange — no consumer registered (sac is the first producer).
        # Act
        delivered = emit_anomaly({"agent": "a"}, [])
        # Assert — returns 0, does not raise.
        assert delivered == 0

    def test_throwing_consumer_does_not_propagate(self) -> None:
        # Arrange — one consumer raises, a later one must still receive.
        received: list[dict] = []

        def boom(event: dict) -> None:
            raise RuntimeError("consumer blew up")

        def ok(event: dict) -> None:
            received.append(event)

        # Act — must NOT raise.
        delivered = emit_anomaly({"agent": "a"}, [boom, ok])
        # Assert — the healthy consumer still got it (count == 1).
        assert delivered == 1


# ===========================================================================
# IO resolvers against REAL files under tmp_path
# ===========================================================================


class TestLoadTasksDoc:
    def test_reads_a_real_tasks_yaml(self, tmp_path) -> None:
        # Arrange — write a real YAML file.
        p = tmp_path / "tasks.yaml"
        p.write_text("tasks:\n  - id: c1\n    status: pending\n")
        # Act
        doc = mod._load_tasks_doc(p)
        # Assert
        assert doc["tasks"][0]["id"] == "c1"

    def test_missing_file_degrades_to_empty(self, tmp_path) -> None:
        # Arrange — path does not exist.
        # Act
        doc = mod._load_tasks_doc(tmp_path / "nope.yaml")
        # Assert
        assert doc == {}


@pytest.fixture
def home_at_tmp(tmp_path):
    """Point ``$HOME`` (which the production session-path resolver expands)
    at a real ``tmp_path`` and restore it on teardown — a real env swap,
    NOT a monkeypatch of any production internal."""
    import os

    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


class TestSessionLastActiveTs:
    def test_returns_last_record_timestamp_from_real_jsonl(
        self, home_at_tmp
    ) -> None:
        # Arrange — a real session.jsonl with two timestamped records under
        # the redirected $HOME runtime root (no internal function mocked).
        run_dir = (
            home_at_tmp / ".scitex" / "agent-container" / "runtime" / "agent-x"
        )
        run_dir.mkdir(parents=True)
        sess = run_dir / "session.jsonl"
        early = _iso(NOW - 500.0)
        late = _iso(NOW - 10.0)
        sess.write_text(
            json.dumps({"ts": early}) + "\n" + json.dumps({"ts": late}) + "\n"
        )
        # Act
        ts = mod._session_last_active_ts("agent-x")
        # Assert — the LAST record's timestamp wins.
        assert ts == pytest.approx(
            datetime.fromisoformat(late.replace("Z", "+00:00")).timestamp()
        )

    def test_missing_session_returns_none(self, home_at_tmp) -> None:
        # Arrange — runtime root exists but no session.jsonl for the agent.
        target = "ghost-agent"
        # Act
        ts = mod._session_last_active_ts(target)
        # Assert
        assert ts is None


# ===========================================================================
# loop — drives detection + emit through one tick (injected sources)
# ===========================================================================


async def _run_one_tick(*, consumers, doc, liveness, **kw) -> None:
    """Spin the loop briefly then cancel — forces ≥1 tick deterministically."""
    task = asyncio.create_task(
        liveness_tick_reconciler_loop(
            interval_s=0.05,
            stale_s=STALE_S,
            tasks_doc_source=doc,
            liveness_source=liveness,
            consumers_source=consumers,
            now_fn=lambda: NOW,
            **kw,
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
class TestLoopEmits:
    async def test_loop_emits_anomaly_for_a_stuck_card(self) -> None:
        # Arrange — a stuck card + a dead owner + a real consumer list.
        events: list[dict] = []
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        await _run_one_tick(
            consumers=[events.append], doc=_stuck_doc(), liveness=liveness
        )
        # Assert — exactly one anomaly landed.
        assert len(events) >= 1

    async def test_emitted_event_has_the_locked_shape(self) -> None:
        # Arrange
        events: list[dict] = []
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        await _run_one_tick(
            consumers=[events.append], doc=_stuck_doc(), liveness=liveness
        )
        # Assert — keys match the locked event shape.
        assert set(events[0]) == {"agent", "card_id", "reason", "severity", "ts"}

    async def test_emitted_reason_is_owner_not_live(self) -> None:
        # Arrange
        events: list[dict] = []
        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        await _run_one_tick(
            consumers=[events.append], doc=_stuck_doc(), liveness=liveness
        )
        # Assert
        assert events[0]["reason"] == "owner-not-live"

    async def test_progressing_card_emits_nothing(self) -> None:
        # Arrange — owner live + session moved 1s ago ⇒ no anomaly.
        events: list[dict] = []
        liveness = {"agent-x": AgentLiveness(is_live=True, last_active_ts=NOW - 1.0)}
        # Act
        await _run_one_tick(
            consumers=[events.append], doc=_stuck_doc(), liveness=liveness
        )
        # Assert
        assert events == []

    async def test_throwing_consumer_does_not_kill_the_loop(self) -> None:
        # Arrange — a throwing consumer FIRST, a recording one SECOND. If the
        # throw propagated it would crash the loop before the second runs,
        # so a recorded event proves the loop survived the bad consumer.
        recorded: list[dict] = []

        def boom(event: dict) -> None:
            raise RuntimeError("downstream consumer failure")

        liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
        # Act
        await _run_one_tick(
            consumers=[boom, recorded.append], doc=_stuck_doc(), liveness=liveness
        )
        # Assert — the healthy consumer still received the anomaly.
        assert len(recorded) >= 1


# ===========================================================================
# dedup / re-notify cooldown
# ===========================================================================


@pytest.mark.asyncio
async def test_loop_dedups_within_renotify_cooldown() -> None:
    # Arrange — many ticks at fixed NOW within a long cooldown.
    events: list[dict] = []
    liveness = {"agent-x": AgentLiveness(is_live=False, last_active_ts=None)}
    task = asyncio.create_task(
        liveness_tick_reconciler_loop(
            interval_s=0.02,
            stale_s=STALE_S,
            renotify_s=10_000.0,  # huge → never re-notify in this test
            tasks_doc_source=_stuck_doc(),
            liveness_source=liveness,
            consumers_source=[events.append],
            now_fn=lambda: NOW,  # frozen clock → all ticks share one "now"
        )
    )
    # Act — let several ticks run.
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Assert — despite many ticks, the (agent, card) fired exactly once.
    assert len(events) == 1


# ===========================================================================
# cancellation
# ===========================================================================


@pytest.mark.asyncio
async def test_loop_honours_cancellation_cleanly() -> None:
    # Arrange
    task = asyncio.create_task(
        liveness_tick_reconciler_loop(
            interval_s=0.05,
            tasks_doc_source={"tasks": []},
            liveness_source={},
            consumers_source=[],
            now_fn=lambda: NOW,
        )
    )
    # Act
    await asyncio.sleep(0.06)
    task.cancel()
    # Assert
    with pytest.raises(asyncio.CancelledError):
        await task


# EOF
