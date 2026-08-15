"""Tests for the centralized TUI heartbeat writer.

Exercises the asyncio task the listen lifespan launches — without real
tmux / a real registry — by injecting the ``agent_lister``, ``sessions_fn``,
``write_fn`` and ``tmux_check`` seams. Mirrors test__github_ci_poll_loop.py's
create-task → sleep → cancel pattern, and writes into real ``tmp_path`` state
dirs (no mocks).

``sessions_fn`` is the BATCHED fleet probe (one ``tmux list-sessions`` for
every session) that replaced the per-agent ``session_exists_fn`` /
``activity_fn`` pair — those cost 3 ``tmux`` spawns per agent, making the
tick O(N) subprocesses, which blew its budget at fleet scale and got the
tick ABANDONED (writing NO liveness data at all).

STX-TQ002 AAA-markers each on its own line + STX-TQ007 one-assert.
No mocks/monkeypatch — dependency-injection seams + tmp-dir fixtures.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._session_movement import heartbeat_iso
from scitex_agent_container._lifecycle._tui_heartbeat_loop import (
    DEFAULT_TUI_HEARTBEAT_INTERVAL_S,
    tui_heartbeat_loop,
)

ISO_8601_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$")

# Real heartbeat writer under test — the same function the SDK runner
# uses. Injected as the ``write_fn`` seam so the loop drives the actual
# atomic write into a tmp state dir (no mock).
from scitex_agent_container._runners._session_state import write_heartbeat

from tests.scitex_agent_container._helpers.loop_optin import loop_enabled


@pytest.fixture(autouse=True)
def _loop_enabled_for_this_file():
    """tests/conftest.py turns this loop OFF for the whole suite.

    It is a background stderr writer on a 30-second period, and no ACL /
    CLI / listen test asked for one. THIS file is the file that exercises
    the loop, so it is the file that opts back in.
    """
    with loop_enabled("SAC_TUI_HEARTBEAT_DISABLED"):
        yield


PINNED_ACTIVITY_TS = 1_750_000_000

# The loop looks the agent up under its ``tui-<name>`` session key.
LIVE_SNAPSHOT = {"tui-tui-demo": PINNED_ACTIVITY_TS}


def _one_tui_agent(state_dir: Path):
    """Build an ``agent_lister`` seam yielding a single TUI agent."""

    def _lister():
        return [{"name": "tui-demo", "state_dir": state_dir}]

    return _lister


async def _settle(predicate, timeout_s: float = 5.0) -> None:
    """Wait until ``predicate()`` holds (or the deadline passes).

    The tick body runs in an EXECUTOR THREAD, so a fixed sleep is a race: on
    a loaded host the thread has not finished writing when the assertion
    runs. Poll for the expected end-state instead of guessing a duration.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)


async def _run_one_tick(settle_for=None, **kwargs) -> None:
    """Start the loop, let one tick complete, then cancel cleanly.

    ``settle_for`` is a predicate describing the tick's expected end-state
    (e.g. "the heartbeat file exists"). When omitted — the cases that assert
    NOTHING was written — a short fixed wait is correct, since there is no
    positive end-state to poll for.
    """
    task = asyncio.create_task(tui_heartbeat_loop(interval_s=0.05, **kwargs))
    if settle_for is None:
        await asyncio.sleep(0.25)
    else:
        await _settle(settle_for)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Core: a live TUI agent gets a valid heartbeat.json (the operator ask).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_writes_heartbeat_json_for_live_tui_agent(tmp_path: Path):
    # Arrange — a temp state dir + a fresh pane-activity stamp; real writer.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        settle_for=lambda: (state_dir / "heartbeat.json").is_file(),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    # Assert
    assert (state_dir / "heartbeat.json").is_file()


@pytest.mark.asyncio
async def test_heartbeat_json_is_valid_json(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        settle_for=lambda: (state_dir / "heartbeat.json").is_file(),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert
    assert payload["state"] == "running"


@pytest.mark.asyncio
async def test_heartbeat_ts_is_the_pane_activity_epoch(tmp_path: Path):
    # Arrange — the recorded ts must be the snapshot's pane-activity epoch.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        settle_for=lambda: (state_dir / "heartbeat.json").is_file(),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert
    assert payload["ts"] == float(PINNED_ACTIVITY_TS)


@pytest.mark.asyncio
async def test_heartbeat_at_renders_as_iso_8601_for_the_read_side(tmp_path: Path):
    # Arrange — the status read side (heartbeat_iso) must now resolve a
    # non-empty ISO-8601 stamp for a TUI agent.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        settle_for=lambda: (state_dir / "heartbeat.json").is_file(),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    iso = heartbeat_iso(state_dir)
    # Assert
    assert ISO_8601_UTC_RE.match(iso) is not None


@pytest.mark.asyncio
async def test_heartbeat_pid_is_zero_for_tui_agent(tmp_path: Path):
    # Arrange — a TUI agent has no meaningful host pid (managed via
    # tmux kill-session), so the writer records 0 rather than a stale
    # launcher pid.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        settle_for=lambda: (state_dir / "heartbeat.json").is_file(),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert
    assert payload["pid"] == 0


@pytest.mark.asyncio
async def test_looks_the_agent_up_under_its_tui_prefixed_session_key(tmp_path: Path):
    # Arrange — a snapshot keyed WITHOUT the ``tui-`` prefix must not match
    # (the loop owns the ``tui-<name>`` convention TuiSessionRuntime sets).
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: {"tui-demo": PINNED_ACTIVITY_TS},
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    # Assert
    assert not (state_dir / "heartbeat.json").exists()


# ---------------------------------------------------------------------------
# Skip cases: session not in the snapshot → no heartbeat written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_heartbeat_when_session_absent_from_snapshot(tmp_path: Path):
    # Arrange — agent is listed but has no live tmux session (probe
    # SUCCEEDED and returned an empty fleet — a confirmed absence).
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: {},
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    # Assert
    assert not (state_dir / "heartbeat.json").exists()


# ---------------------------------------------------------------------------
# SCALING: the tick must be O(1) subprocess probes, not O(N).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_batched_probe_serves_the_whole_fleet(tmp_path: Path):
    # Arrange — 50 agents; the batched probe must be called exactly ONCE
    # per tick. Serially this cost 3 tmux spawns PER AGENT (150 here),
    # which is what blew the tick budget and got it abandoned.
    calls: list[float] = []
    agents = []
    for idx in range(50):
        state_dir = tmp_path / f"agent-{idx}"
        state_dir.mkdir()
        agents.append({"name": f"agent-{idx}", "state_dir": state_dir})
    snapshot = {f"tui-agent-{i}": PINNED_ACTIVITY_TS for i in range(50)}

    def _sessions_fn():
        calls.append(time.time())
        return dict(snapshot)

    # Act — one tick (interval 30s, so no second tick can fire).
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=30.0,
            agent_lister=lambda: list(agents),
            sessions_fn=_sessions_fn,
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
        )
    )
    await _settle(
        lambda: all(
            (Path(a["state_dir"]) / "heartbeat.json").is_file() for a in agents
        )
    )
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Assert — ONE probe served all 50 agents (serially: 150 tmux spawns).
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_batched_probe_beats_every_agent_in_the_fleet(tmp_path: Path):
    # Arrange — all 50 agents must still get a beat from the ONE probe.
    agents = []
    for idx in range(50):
        state_dir = tmp_path / f"agent-{idx}"
        state_dir.mkdir()
        agents.append({"name": f"agent-{idx}", "state_dir": state_dir})
    snapshot = {f"tui-agent-{i}": PINNED_ACTIVITY_TS for i in range(50)}
    # Act
    await _run_one_tick(
        settle_for=lambda: all(
            (Path(a["state_dir"]) / "heartbeat.json").is_file() for a in agents
        ),
        agent_lister=lambda: list(agents),
        sessions_fn=lambda: dict(snapshot),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    written = sum(
        1 for a in agents if (Path(a["state_dir"]) / "heartbeat.json").is_file()
    )
    # Assert
    assert written == 50


# NOTE: the load-bearing "a failed/abandoned tick is UNKNOWN, never DEAD"
# rule — and the overlap guard that keeps abandoned ticks from stacking —
# are covered for BOTH heartbeat loops (plus a real state.db) in the
# sibling ``test__heartbeat_unknown_is_not_dead.py``.


# ---------------------------------------------------------------------------
# Resilience + lifecycle (mirror the CI poller's contract).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_agent_failure_does_not_block_a_second_agent(tmp_path: Path):
    # Arrange — first agent's write blows up; second must still be written.
    good_dir = tmp_path / "tui-good"
    good_dir.mkdir()

    def _lister():
        return [
            {"name": "boom", "state_dir": tmp_path / "tui-boom"},
            {"name": "good", "state_dir": good_dir},
        ]

    def _write(state_dir, **kwargs):
        if "boom" in str(state_dir):
            raise RuntimeError("simulated per-agent write failure")
        write_heartbeat(state_dir, **kwargs)

    # Act
    await _run_one_tick(
        settle_for=lambda: (good_dir / "heartbeat.json").is_file(),
        agent_lister=_lister,
        sessions_fn=lambda: {
            "tui-boom": PINNED_ACTIVITY_TS,
            "tui-good": PINNED_ACTIVITY_TS,
        },
        write_fn=_write,
        tmux_check=lambda: True,
    )
    # Assert
    assert (good_dir / "heartbeat.json").is_file()


@pytest.mark.asyncio
async def test_loop_disabled_when_tmux_missing_writes_nothing(tmp_path: Path):
    # Arrange — fail-loud preflight: no tmux → loop returns immediately.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act — returns at once (no infinite loop), so await directly.
    await tui_heartbeat_loop(
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: False,
    )
    # Assert
    assert not (state_dir / "heartbeat.json").exists()


@pytest.mark.asyncio
async def test_loop_disabled_via_env_var_writes_nothing(tmp_path: Path):
    # Arrange — explicit env save/restore (no monkeypatch, PA-306).
    import os as _os

    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    key = "SAC_TUI_HEARTBEAT_DISABLED"
    saved = _os.environ.get(key)
    _os.environ[key] = "1"
    # Act
    try:
        await tui_heartbeat_loop(
            agent_lister=_one_tui_agent(state_dir),
            sessions_fn=lambda: dict(LIVE_SNAPSHOT),
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
        )
    finally:
        if saved is None:
            _os.environ.pop(key, None)
        else:
            _os.environ[key] = saved
    # Assert
    assert not (state_dir / "heartbeat.json").exists()


@pytest.mark.asyncio
async def test_loop_survives_a_tick_exception_then_cancels_cleanly(tmp_path: Path):
    # Arrange — a lister that raises must NOT kill the loop (logged + retried).
    def boom():
        raise RuntimeError("transient registry blip")

    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=boom,
            sessions_fn=lambda: dict(LIVE_SNAPSHOT),
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
        )
    )
    # Act — let the failing tick run, then cancel.
    await asyncio.sleep(0.12)
    task.cancel()
    # Assert — the loop swallowed the tick error and stayed alive, so
    # cancellation (not the RuntimeError) is what surfaces.
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loop_honours_cancellation_cleanly(tmp_path: Path):
    # Arrange
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=lambda: [],
            sessions_fn=lambda: {},
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
        )
    )
    # Act
    await asyncio.sleep(0.06)
    task.cancel()
    # Assert — the finally must re-raise CancelledError, not swallow it.
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_default_interval_is_thirty_seconds():
    # Arrange — guard the documented cadence default.
    # Act
    value = DEFAULT_TUI_HEARTBEAT_INTERVAL_S
    # Assert
    assert value == 30.0


# ---------------------------------------------------------------------------
# Turn-bridge supervision rides on the SAME tick (2026-08-11 incident)
# ---------------------------------------------------------------------------
# The host-side /v1/turn bridge is spawned once at start and was supervised by
# nothing, so 14 of 15 on the host were dead PIDs while their agents still read
# healthy and every pushed wake was refused. This tick already enumerates every
# TUI agent with a fresh liveness snapshot, so it re-asserts the bridge too.


class RecordingSupervisor:
    """Real callable with ``supervise_bridges``' shape (the DI seam)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, agents, *, snapshot):
        self.calls.append((list(agents), dict(snapshot)))
        return {}


class ExplodingSupervisor:
    """A supervisor that fails, to prove it cannot cost the heartbeat."""

    def __call__(self, agents, *, snapshot):
        raise RuntimeError("supervision blew up")


@pytest.mark.asyncio
async def test_the_tick_supervises_the_turn_bridge(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    supervisor = RecordingSupervisor()
    # Act
    await _run_one_tick(
        settle_for=lambda: bool(supervisor.calls),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
        supervise_fn=supervisor,
    )
    # Assert
    assert len(supervisor.calls) >= 1


@pytest.mark.asyncio
async def test_supervision_reuses_this_ticks_snapshot(tmp_path: Path):
    # Arrange — reusing the batched snapshot is what keeps supervision free of
    # extra tmux subprocesses (the cost that got this tick abandoned once).
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    supervisor = RecordingSupervisor()
    # Act
    await _run_one_tick(
        settle_for=lambda: bool(supervisor.calls),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
        supervise_fn=supervisor,
    )
    # Assert
    assert supervisor.calls[0][1] == LIVE_SNAPSHOT


@pytest.mark.asyncio
async def test_no_supervision_when_the_tmux_probe_failed(tmp_path: Path):
    # Arrange — a failed probe means liveness is UNKNOWN, and respawning a
    # bridge on a guess is the same "infer dead from unknown" mistake the
    # heartbeat writer already refuses to make.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    supervisor = RecordingSupervisor()
    # Act
    await _run_one_tick(
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: None,
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
        supervise_fn=supervisor,
    )
    # Assert
    assert supervisor.calls == []


@pytest.mark.asyncio
async def test_a_failing_supervisor_still_leaves_the_heartbeat_written(
    tmp_path: Path,
):
    # Arrange — liveness data is the tick's primary duty and must survive a
    # supervision failure.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        settle_for=lambda: (state_dir / "heartbeat.json").is_file(),
        agent_lister=_one_tui_agent(state_dir),
        sessions_fn=lambda: dict(LIVE_SNAPSHOT),
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
        supervise_fn=ExplodingSupervisor(),
    )
    # Assert
    assert (state_dir / "heartbeat.json").is_file()
