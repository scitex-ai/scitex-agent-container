"""Tests for the centralized SDK/claude-session heartbeat writer.

Sibling of test__tui_heartbeat_loop.py: exercises the asyncio task the
listen lifespan launches for the NON-TUI runtimes — without a real
runtime / registry / state.db — by injecting the ``agent_lister``,
``is_running_fn`` and ``write_fn`` seams. Writes into real ``tmp_path``
state dirs (no mocks).

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
from scitex_agent_container._lifecycle._sdk_heartbeat_loop import (
    DEFAULT_SDK_HEARTBEAT_INTERVAL_S,
    sdk_heartbeat_loop,
)
from scitex_agent_container._runners._session_state import write_heartbeat

from tests.scitex_agent_container._helpers.loop_optin import loop_enabled


@pytest.fixture(autouse=True)
def _loop_enabled_for_this_file():
    """tests/conftest.py turns this loop OFF for the whole suite.

    Same reason as its TUI twin: a periodic background writer no other
    test asked for. THIS file exercises it, so THIS file opts back in.
    """
    with loop_enabled("SAC_SDK_HEARTBEAT_DISABLED"):
        yield


ISO_8601_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$")


class _Cfg:
    """Minimal stand-in for AgentConfig — the seam only needs an object
    to hand to ``is_running_fn`` (injected in these tests)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.runtime = "claude-agent-sdk"


def _one_sdk_agent(state_dir: Path):
    """Build an ``agent_lister`` seam yielding a single SDK agent."""

    def _lister():
        return [{"name": "sdk-demo", "config": _Cfg("sdk-demo"), "state_dir": state_dir}]

    return _lister


async def _run_one_tick(**kwargs) -> None:
    """Start the loop, let exactly one tick run, then cancel cleanly."""
    task = asyncio.create_task(sdk_heartbeat_loop(interval_s=0.05, **kwargs))
    await asyncio.sleep(0.12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Core: a live SDK agent gets a FRESH heartbeat.json (the freeze fix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_writes_heartbeat_json_for_live_sdk_agent(tmp_path: Path):
    # Arrange — a temp state dir + a runtime that reports the agent alive.
    state_dir = tmp_path / "sdk-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_sdk_agent(state_dir),
        is_running_fn=lambda cfg: True,
        write_fn=write_heartbeat,
    )
    # Assert
    assert (state_dir / "heartbeat.json").is_file()


@pytest.mark.asyncio
async def test_heartbeat_state_is_running(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "sdk-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_sdk_agent(state_dir),
        is_running_fn=lambda cfg: True,
        write_fn=write_heartbeat,
    )
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert
    assert payload["state"] == "running"


@pytest.mark.asyncio
async def test_heartbeat_ts_is_fresh_wall_clock(tmp_path: Path):
    # Arrange — unlike the TUI loop (which stamps the stale pane-activity
    # epoch), the SDK loop stamps a FRESH wall-clock ts so a quiet-but-
    # live agent's heartbeat_at moves. Pin ``now`` via the seam.
    import time as _time

    state_dir = tmp_path / "sdk-demo"
    state_dir.mkdir()
    before = _time.time()
    # Act
    await _run_one_tick(
        agent_lister=_one_sdk_agent(state_dir),
        is_running_fn=lambda cfg: True,
        write_fn=write_heartbeat,
    )
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert
    assert payload["ts"] >= before


@pytest.mark.asyncio
async def test_heartbeat_at_renders_as_iso_8601_for_the_read_side(tmp_path: Path):
    # Arrange
    state_dir = tmp_path / "sdk-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_sdk_agent(state_dir),
        is_running_fn=lambda cfg: True,
        write_fn=write_heartbeat,
    )
    iso = heartbeat_iso(state_dir)
    # Assert
    assert ISO_8601_UTC_RE.match(iso) is not None


# ---------------------------------------------------------------------------
# Skip cases: not running → no heartbeat written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_heartbeat_when_runtime_reports_stopped(tmp_path: Path):
    # Arrange — the declared runtime says the agent is NOT running.
    state_dir = tmp_path / "sdk-demo"
    state_dir.mkdir()
    # Act
    await _run_one_tick(
        agent_lister=_one_sdk_agent(state_dir),
        is_running_fn=lambda cfg: False,
        write_fn=write_heartbeat,
    )
    # Assert
    assert not (state_dir / "heartbeat.json").exists()


@pytest.mark.asyncio
async def test_no_heartbeat_when_state_dir_is_none(tmp_path: Path):
    # Arrange — a listed agent with no resolvable state dir is skipped.
    def _lister():
        return [{"name": "sdk-demo", "config": _Cfg("sdk-demo"), "state_dir": None}]

    # Act — must not raise even though there is nowhere to write.
    await _run_one_tick(
        agent_lister=_lister,
        is_running_fn=lambda cfg: True,
        write_fn=write_heartbeat,
    )
    # Assert — no crash reaching here is the contract.
    assert True


# ---------------------------------------------------------------------------
# Resilience + lifecycle (mirror the TUI loop's contract).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_agent_failure_does_not_block_a_second_agent(tmp_path: Path):
    # Arrange — first agent's write blows up; second must still be written.
    good_dir = tmp_path / "sdk-good"
    good_dir.mkdir()

    def _lister():
        return [
            {"name": "boom", "config": _Cfg("boom"), "state_dir": tmp_path / "sdk-boom"},
            {"name": "good", "config": _Cfg("good"), "state_dir": good_dir},
        ]

    def _write(state_dir, **kwargs):
        if "boom" in str(state_dir):
            raise RuntimeError("simulated per-agent write failure")
        write_heartbeat(state_dir, **kwargs)

    # Act
    await _run_one_tick(
        agent_lister=_lister,
        is_running_fn=lambda cfg: True,
        write_fn=_write,
    )
    # Assert
    assert (good_dir / "heartbeat.json").is_file()


@pytest.mark.asyncio
async def test_loop_disabled_via_env_var_writes_nothing(tmp_path: Path):
    # Arrange — explicit env save/restore (no monkeypatch, PA-306).
    import os as _os

    state_dir = tmp_path / "sdk-demo"
    state_dir.mkdir()
    key = "SAC_SDK_HEARTBEAT_DISABLED"
    saved = _os.environ.get(key)
    _os.environ[key] = "1"
    # Act
    try:
        await sdk_heartbeat_loop(
            agent_lister=_one_sdk_agent(state_dir),
            is_running_fn=lambda cfg: True,
            write_fn=write_heartbeat,
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
        sdk_heartbeat_loop(
            interval_s=0.05,
            agent_lister=boom,
            is_running_fn=lambda cfg: True,
            write_fn=write_heartbeat,
        )
    )
    # Act
    await asyncio.sleep(0.12)
    task.cancel()
    # Assert — the loop swallowed the tick error and stayed alive.
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loop_honours_cancellation_cleanly(tmp_path: Path):
    # Arrange
    task = asyncio.create_task(
        sdk_heartbeat_loop(
            interval_s=0.05,
            agent_lister=lambda: [],
            is_running_fn=lambda cfg: True,
            write_fn=write_heartbeat,
        )
    )
    # Act
    await asyncio.sleep(0.06)
    task.cancel()
    # Assert
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_default_interval_is_thirty_seconds():
    # Arrange — guard the documented cadence default.
    # Act
    value = DEFAULT_SDK_HEARTBEAT_INTERVAL_S
    # Assert
    assert value == 30.0
