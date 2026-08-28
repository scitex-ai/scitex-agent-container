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
import os
import time
from datetime import datetime, timezone

import pytest

from scitex_agent_container._state.state_db_instances import record_instance_start

from scitex_agent_container._listen._liveness_tick import (
    DEFAULT_INTERVAL_S,
    DEFAULT_RENOTIFY_S,
    DEFAULT_STALE_S,
    HOOKS_ENTRY_POINT_GROUP,
    AgentLiveness,
    emit_anomaly,
    liveness_tick_reconciler_loop,
)

# The blocking IO resolvers live beside the loop glue (``_liveness_tick`` is
# the loop + bus emit; ``_liveness_tick_resolve`` is every FS/registry read).
from scitex_agent_container._listen import _liveness_tick_resolve as mod


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield

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

    def test_final_record_larger_than_the_tail_window_is_still_found(
        self, home_at_tmp
    ) -> None:
        # Arrange — the read is a bounded O(1) TAIL (it now runs for every
        # owner every tick). A final record bigger than that window must fall
        # back to the full scan rather than report "no activity" — reporting
        # none for a busy agent is exactly the false-death this fix removes.
        run_dir = (
            home_at_tmp / ".scitex" / "agent-container" / "runtime" / "agent-x"
        )
        run_dir.mkdir(parents=True)
        late = _iso(NOW - 5.0)
        huge = json.dumps({"ts": late, "payload": "x" * (2 * mod._SESSION_TAIL_BYTES)})
        (run_dir / "session.jsonl").write_text(
            json.dumps({"ts": _iso(NOW - 900.0)}) + "\n" + huge + "\n"
        )
        # Act
        ts = mod._session_last_active_ts("agent-x")
        # Assert
        assert ts == pytest.approx(
            datetime.fromisoformat(late.replace("Z", "+00:00")).timestamp()
        )


class TestHeartbeatSignals:
    def test_reads_the_beat_and_the_activity_ts_from_a_real_heartbeat(
        self, home_at_tmp
    ) -> None:
        # Arrange — a real heartbeat.json. The TUI runtimes that make up the
        # fleet write NO session.jsonl at all, so this is their only record.
        run_dir = (
            home_at_tmp / ".scitex" / "agent-container" / "runtime" / "agent-x"
        )
        run_dir.mkdir(parents=True)
        (run_dir / "heartbeat.json").write_text(
            json.dumps({"ts": NOW - 300.0, "pid": 0, "state": "running"})
        )
        # Act
        _beat_ts, activity_ts = mod._heartbeat_signals("agent-x")
        # Assert — the record's own ``ts`` is the PROGRESS signal.
        assert activity_ts == pytest.approx(NOW - 300.0)

    def test_beat_ts_tracks_the_file_mtime(self, home_at_tmp) -> None:
        # Arrange — the MTIME is the PROCESS-ALIVE signal: the heartbeat writer
        # runs inside the live agent, so a fresh beat proves it exists even
        # when the registry has no pid for it.
        run_dir = (
            home_at_tmp / ".scitex" / "agent-container" / "runtime" / "agent-x"
        )
        run_dir.mkdir(parents=True)
        hb = run_dir / "heartbeat.json"
        hb.write_text(json.dumps({"ts": NOW - 300.0}))
        # Act
        beat_ts, _activity_ts = mod._heartbeat_signals("agent-x")
        # Assert
        assert beat_ts == pytest.approx(hb.stat().st_mtime)

    def test_missing_heartbeat_is_no_record_at_all(self, home_at_tmp) -> None:
        # Arrange — no heartbeat file ⇒ (None, None) ⇒ UNKNOWN, not "dead".
        # Act
        signals = mod._heartbeat_signals("ghost-agent")
        # Assert
        assert signals == (None, None)


class TestFleetLastBeatTs:
    def test_returns_the_newest_beat_across_the_whole_fleet(
        self, home_at_tmp
    ) -> None:
        # Arrange — REAL runtime dirs. The fleet reading must see the agent
        # that is STILL being beaten for, even though it owns no card: that is
        # how the rule tells "the writer died" from "the agents died".
        runtime = home_at_tmp / ".scitex" / "agent-container" / "runtime"
        for name in ("stale-agent", "beating-agent"):
            (runtime / name).mkdir(parents=True)
            (runtime / name / "heartbeat.json").write_text(json.dumps({"ts": 1.0}))
        fresh = runtime / "beating-agent" / "heartbeat.json"
        os.utime(fresh, (NOW, NOW))
        os.utime(runtime / "stale-agent" / "heartbeat.json", (NOW - 99_999, NOW - 99_999))
        # Act
        newest = mod.fleet_last_beat_ts()
        # Assert
        assert newest == pytest.approx(NOW)

    def test_no_heartbeat_anywhere_is_no_reading(self, home_at_tmp) -> None:
        # Arrange — an empty runtime root ⇒ no fleet reading available.
        (home_at_tmp / ".scitex" / "agent-container" / "runtime").mkdir(parents=True)
        # Act
        newest = mod.fleet_last_beat_ts()
        # Assert
        assert newest is None


# ===========================================================================
# registry availability — UNREADABLE (None) must not look like EMPTY ({})
# ===========================================================================


class TestRegistryAvailability:
    def test_readable_but_empty_registry_is_an_empty_dict(self, tmp_path) -> None:
        # Arrange — a REAL sqlite state.db, created empty by the real schema
        # init. The read SUCCEEDS and lists nobody.
        # Act
        pids = mod._live_agent_pids(db_path=tmp_path / "state.db")
        # Assert
        assert pids == {}

    def test_unreadable_registry_is_none(self, tmp_path) -> None:
        # Arrange — a REAL failure, no mock: the db's parent path is a regular
        # FILE, so the OS refuses to create the directory sqlite needs.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("i am a file, not a directory")
        # Act
        pids = mod._live_agent_pids(db_path=blocker / "state.db")
        # Assert — distinctly "we could not read it", NOT "nobody is alive".
        assert pids is None

    def test_pidless_rows_contribute_no_pid(self, tmp_path) -> None:
        # Arrange — EXACTLY what the live fleet writes: an active instances row
        # with pid=NULL. The read succeeds; it simply proves nothing.
        db = tmp_path / "state.db"
        record_instance_start("agent-x")
        # Act
        pids = mod._live_agent_pids(db_path=db)
        # Assert
        assert pids == {}

    def test_a_recorded_live_pid_is_returned(self, tmp_path) -> None:
        # Arrange — a row carrying a REAL, alive pid (this test process).
        db = tmp_path / "state.db"
        record_instance_start("agent-x", pid=os.getpid(), db_path=db)
        # Act
        pids = mod._live_agent_pids(db_path=db)
        # Assert
        assert pids["agent-x"] == os.getpid()


# ===========================================================================
# resolve_liveness — the POSITIVE signals are read UNCONDITIONALLY
#
# The flood's IO-level cause: the session timestamp used to be read ONLY when
# the registry had already voted "live" (``... if is_live else None``). Since
# the fleet's registry records pid=NULL on every row, it never voted live, so
# the strongest positive signal was never consulted for ANY agent.
# ===========================================================================


class TestResolveLiveness:
    def test_activity_is_read_even_when_the_registry_proves_nothing(
        self, home_at_tmp
    ) -> None:
        # Arrange — a REAL empty registry (nobody vouched for) + a REAL session
        # record written 10s ago. The owner is plainly alive and working.
        run_dir = (
            home_at_tmp / ".scitex" / "agent-container" / "runtime" / "agent-x"
        )
        run_dir.mkdir(parents=True)
        late = _iso(NOW - 10.0)
        (run_dir / "session.jsonl").write_text(json.dumps({"ts": late}) + "\n")
        # Act
        out = mod.resolve_liveness(["agent-x"], db_path=home_at_tmp / "state.db")
        # Assert — THE regression: this used to be None (never even read).
        assert out["agent-x"].last_active_ts == pytest.approx(
            datetime.fromisoformat(late.replace("Z", "+00:00")).timestamp()
        )

    def test_heartbeat_beat_is_read_for_an_owner_with_no_registry_pid(
        self, home_at_tmp
    ) -> None:
        # Arrange — the fleet's real shape: a pid-less active row + a fresh
        # heartbeat. The heartbeat is the proof of life the registry lost.
        db = home_at_tmp / "state.db"
        record_instance_start("agent-x")
        run_dir = (
            home_at_tmp / ".scitex" / "agent-container" / "runtime" / "agent-x"
        )
        run_dir.mkdir(parents=True)
        (run_dir / "heartbeat.json").write_text(json.dumps({"ts": NOW - 300.0}))
        # Act
        out = mod.resolve_liveness(["agent-x"], db_path=db)
        # Assert
        assert out["agent-x"].last_beat_ts is not None

    def test_a_live_registry_pid_still_resolves_live(self, home_at_tmp) -> None:
        # Arrange — the registry CAN still vouch for an agent; when it does,
        # that remains corroborating positive evidence.
        db = home_at_tmp / "state.db"
        record_instance_start("agent-x", pid=os.getpid(), db_path=db)
        # Act
        out = mod.resolve_liveness(["agent-x"], db_path=db)
        # Assert
        assert out["agent-x"].is_live is True

    def test_unreadable_registry_makes_the_owner_unknown(self, home_at_tmp) -> None:
        # Arrange — a REAL unreadable registry (parent path is a file).
        blocker = home_at_tmp / "not-a-dir"
        blocker.write_text("i am a file, not a directory")
        # Act
        out = mod.resolve_liveness(["agent-x"], db_path=blocker / "state.db")
        # Assert — UNKNOWN, so the rule stays silent instead of guessing dead.
        assert out["agent-x"].known is False

    def test_owner_with_no_pid_and_no_heartbeat_is_unknown(
        self, home_at_tmp
    ) -> None:
        # Arrange — registry readable, but this owner has NO channel that would
        # have shown life (no recorded pid, no heartbeat file). E.g. the
        # pseudo-owners on the real board ("operator", "lead"), which are not
        # sac-managed processes at all and must never be called dead.
        db = home_at_tmp / "state.db"
        record_instance_start("someone-else")
        # Act
        out = mod.resolve_liveness(["operator"], db_path=db)
        # Assert
        assert out["operator"].known is False

    def test_a_dead_agent_that_left_a_heartbeat_behind_stays_known(
        self, home_at_tmp
    ) -> None:
        # Arrange — a crashed agent's heartbeat.json PERSISTS with a frozen
        # mtime. That is a channel that would have shown life and does not, so
        # the owner stays KNOWN and real death is still detectable.
        db = home_at_tmp / "state.db"
        record_instance_start("agent-x")
        run_dir = (
            home_at_tmp / ".scitex" / "agent-container" / "runtime" / "agent-x"
        )
        run_dir.mkdir(parents=True)
        (run_dir / "heartbeat.json").write_text(json.dumps({"ts": NOW - 99_999.0}))
        # Act
        out = mod.resolve_liveness(["agent-x"], db_path=db)
        # Assert
        assert out["agent-x"].known is True


# ===========================================================================
# loop — drives detection + emit through one tick (injected sources)
# ===========================================================================


#: How long a barrier waits for the loop to make progress before failing.
#: Generous on purpose: it is not a timing assertion, it is the point at
#: which we stop believing the loop is merely slow.
_BARRIER_TIMEOUT_S = 10.0


async def _await_ticks(counter: "list[int]", wanted: int) -> None:
    """Block until the loop has COMPLETED ``wanted`` ticks, or fail loudly.

    ``counter[0]`` is bumped by the loop's own ``now_fn`` seam, which the
    loop calls exactly once per tick — after the interval sleep, before
    detection and emit. So the (N+1)-th call is proof that tick N ran all
    the way through its emit: the loop cannot reach the next ``now_fn()``
    without having finished the previous iteration.
    """
    deadline = time.monotonic() + _BARRIER_TIMEOUT_S
    while counter[0] <= wanted:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"loop completed {max(counter[0] - 1, 0)} of {wanted} ticks "
                f"in {_BARRIER_TIMEOUT_S}s — it is not running, not slow"
            )
        await asyncio.sleep(0.001)


async def _run_one_tick(*, consumers, doc, liveness, ticks: int = 1, **kw) -> None:
    """Run the loop until ``ticks`` FULL ticks have completed, then cancel.

    The barrier is the loop's own progress (see :func:`_await_ticks`), NOT
    the wall clock. The previous version slept a fixed 0.15s against a
    0.05s interval and called that "deterministic"; it is not. Wall time
    keeps moving while the event loop is starved, so on a loaded CI runner
    the whole budget can elapse before the loop is ever scheduled — which
    is exactly what happened in #967: 151ms of wall time, zero ticks,
    ``assert 0 >= 1``, on py3.12 only, while the same commit passed on
    3.11 and 3.13.

    This also makes the ``emits_nothing`` tests mean something. Under a
    fixed sleep they passed whenever the loop had not run at all — an
    empty list proves silence only once a tick has demonstrably happened.
    """
    counter = [0]

    def _now() -> float:
        counter[0] += 1
        return NOW

    task = asyncio.create_task(
        liveness_tick_reconciler_loop(
            interval_s=0.001,
            stale_s=STALE_S,
            tasks_doc_source=doc,
            liveness_source=liveness,
            consumers_source=consumers,
            now_fn=_now,
            **kw,
        )
    )
    try:
        await _await_ticks(counter, ticks)
    finally:
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

    async def test_live_owner_the_registry_missed_emits_nothing(self) -> None:
        # Arrange — THE FLOOD, end to end through the loop: the registry
        # vouches for nobody (pid=NULL rows), but the owner wrote an activity
        # record 5s ago. Every one of the ~100 false criticals looked like this.
        events: list[dict] = []
        liveness = {
            "agent-x": AgentLiveness(
                is_live=False, last_active_ts=NOW - 5.0, known=True
            )
        }
        # Act
        await _run_one_tick(
            consumers=[events.append], doc=_stuck_doc(), liveness=liveness
        )
        # Assert — the bus stays silent.
        assert events == []

    async def test_unknown_owner_emits_nothing(self) -> None:
        # Arrange — liveness could not be determined at all (registry
        # unavailable, no heartbeat) ⇒ never guess "dead".
        events: list[dict] = []
        liveness = {
            "agent-x": AgentLiveness(
                is_live=False, last_active_ts=None, known=False
            )
        }
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
    # Act — let SEVERAL ticks complete. Counted, not timed: "assert it fired
    # once" is only a dedup claim if more than one tick actually ran, and a
    # fixed sleep cannot promise that on a loaded runner (#967).
    await _run_one_tick(
        consumers=[events.append],
        doc=_stuck_doc(),
        liveness=liveness,
        ticks=3,
        renotify_s=10_000.0,  # huge → never re-notify in this test
    )
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
