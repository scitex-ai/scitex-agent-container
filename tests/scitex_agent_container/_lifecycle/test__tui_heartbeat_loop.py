"""Tests for the centralized TUI heartbeat writer.

Exercises the asyncio task the listen lifespan launches — without real
tmux / a real registry / state.db — by injecting the ``agent_lister``,
``session_exists_fn``, ``activity_fn``, ``write_fn`` and ``tmux_check``
seams. Mirrors test__github_ci_poll_loop.py's create-task → sleep →
cancel pattern, and writes into real ``tmp_path`` state dirs (no mocks).

STX-TQ002 AAA-markers each on its own line + STX-TQ007 one-assert.
No mocks/monkeypatch — dependency-injection seams + tmp-dir fixtures.
"""

from __future__ import annotations

import asyncio
import json
import re
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

PINNED_ACTIVITY_TS = 1_750_000_000


def _one_tui_agent(state_dir: Path):
    """Build an ``agent_lister`` seam yielding a single TUI agent."""

    def _lister():
        return [{"name": "tui-demo", "state_dir": state_dir}]

    return _lister


async def _run_one_tick(**kwargs) -> None:
    """Start the loop, let exactly one tick run, then cancel cleanly."""
    task = asyncio.create_task(tui_heartbeat_loop(interval_s=0.05, **kwargs))
    await asyncio.sleep(0.12)
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
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert
    assert payload["state"] == "running"


@pytest.mark.asyncio
async def test_heartbeat_ts_is_the_pane_activity_epoch(tmp_path: Path):
    # Arrange — the recorded ts must be the injected pane-activity epoch.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert
    assert payload["pid"] == 0


# ---------------------------------------------------------------------------
# Skip cases: no session / no activity → no heartbeat written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_heartbeat_when_session_absent(tmp_path: Path):
    # Arrange — agent is listed but its tmux session does not exist.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: False,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    # Assert
    assert not (state_dir / "heartbeat.json").exists()


@pytest.mark.asyncio
async def test_no_heartbeat_when_activity_is_none(tmp_path: Path):
    # Arrange — session exists but has no readable activity stamp.
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: None,
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    # Assert
    assert not (state_dir / "heartbeat.json").exists()


@pytest.mark.asyncio
async def test_probes_the_tui_prefixed_session_name(tmp_path: Path):
    # Arrange — capture the session name the loop probes; it must be
    # ``tui-<name>`` (the convention TuiSessionRuntime owns).
    state_dir = tmp_path / "tui-demo"
    state_dir.mkdir()
    probed: list = []
    # Act
    await _run_one_tick(
        agent_lister=_one_tui_agent(state_dir),
        session_exists_fn=lambda s: probed.append(s) or True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
        write_fn=write_heartbeat,
        tmux_check=lambda: True,
    )
    # Assert
    assert "tui-tui-demo" in probed


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
        agent_lister=_lister,
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
        session_exists_fn=lambda s: True,
        activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
            session_exists_fn=lambda s: True,
            activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
            session_exists_fn=lambda s: True,
            activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
            session_exists_fn=lambda s: True,
            activity_fn=lambda s: PINNED_ACTIVITY_TS,
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
