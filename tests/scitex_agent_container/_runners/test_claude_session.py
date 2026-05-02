"""Tests for the claude-session runner skeleton.

Phase 1 scope: state-dir layout, atomic PID + heartbeat I/O, signal
handling. The Phase 2 SDK loop will get its own dedicated tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scitex_agent_container._runners import claude_session as runner

# ---------------------------------------------------------------------------
# state-dir helpers
# ---------------------------------------------------------------------------


class TestStatePaths:
    def test_state_dir_for_uses_root(self, tmp_path: Path) -> None:
        d = runner.state_dir_for("alpha", root=tmp_path)
        assert d == tmp_path / "alpha"
        # state_dir_for never creates — that's the runner's job.
        assert not d.exists()

    def test_state_dir_for_default_root_is_under_home(self) -> None:
        d = runner.state_dir_for("zeta")
        assert "agent-container/runtime/zeta" in str(
            d
        ) or "agent-container\\runtime\\zeta" in str(d)


class TestPidIO:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        runner.write_pid(tmp_path, 12345)
        assert runner.read_pid(tmp_path) == 12345

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert runner.read_pid(tmp_path) is None

    def test_read_corrupt_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "pid").write_text("not-a-number\n")
        assert runner.read_pid(tmp_path) is None

    def test_write_is_atomic_no_tmp_left(self, tmp_path: Path) -> None:
        runner.write_pid(tmp_path, 1)
        assert (tmp_path / "pid").is_file()
        assert not (tmp_path / "pid.tmp").exists()


class TestHeartbeatIO:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        runner.write_heartbeat(tmp_path, pid=42, state=runner.STATE_IDLE)
        hb = runner.read_heartbeat(tmp_path)
        assert hb is not None
        assert hb["pid"] == 42
        assert hb["state"] == runner.STATE_IDLE
        assert isinstance(hb["ts"], float)

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert runner.read_heartbeat(tmp_path) is None

    def test_read_corrupt_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "heartbeat.json").write_text("{not json")
        assert runner.read_heartbeat(tmp_path) is None

    def test_subsequent_writes_overwrite(self, tmp_path: Path) -> None:
        runner.write_heartbeat(tmp_path, pid=1, state=runner.STATE_STARTING)
        runner.write_heartbeat(tmp_path, pid=1, state=runner.STATE_IDLE)
        hb = runner.read_heartbeat(tmp_path)
        assert hb is not None and hb["state"] == runner.STATE_IDLE


# ---------------------------------------------------------------------------
# heartbeat loop (in-process, fast tick)
# ---------------------------------------------------------------------------


class TestHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_first_write_is_immediate(self, tmp_path: Path) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            runner._heartbeat_loop(
                tmp_path, pid=os.getpid(), tick_seconds=10.0, stop=stop
            )
        )
        # Give the loop one event-loop tick to write the first heartbeat.
        await asyncio.sleep(0.05)
        assert runner.read_heartbeat(tmp_path) is not None
        stop.set()
        await task

    @pytest.mark.asyncio
    async def test_subsequent_ticks_refresh_ts(self, tmp_path: Path) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            runner._heartbeat_loop(
                tmp_path, pid=os.getpid(), tick_seconds=0.05, stop=stop
            )
        )
        await asyncio.sleep(0.02)
        first = runner.read_heartbeat(tmp_path)
        await asyncio.sleep(0.12)  # at least 2 more ticks
        second = runner.read_heartbeat(tmp_path)
        stop.set()
        await task
        assert first is not None and second is not None
        assert second["ts"] > first["ts"]


# ---------------------------------------------------------------------------
# end-to-end: spawn the runner as its own process and signal it
# ---------------------------------------------------------------------------


@pytest.mark.timeout(15)
def test_run_module_handles_sigterm(tmp_path: Path) -> None:
    """Spawn the runner as a child process; SIGTERM; expect clean exit."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scitex_agent_container._runners.claude_session",
            "--name",
            "ci-runner",
            "--state-root",
            str(tmp_path),
            "--tick-seconds",
            "0.05",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for PID file to appear (proves the runner reached steady state).
    state_dir = tmp_path / "ci-runner"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if (state_dir / "pid").is_file() and (state_dir / "heartbeat.json").is_file():
            break
        time.sleep(0.05)
    assert (state_dir / "pid").is_file(), "runner never wrote pid"

    # Recorded PID must match the child we spawned.
    recorded = int((state_dir / "pid").read_text().strip())
    assert recorded == proc.pid

    # Send SIGTERM and expect a fast clean shutdown.
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=10)
    assert rc == 0, (
        f"runner exited non-zero ({rc}); stderr={proc.stderr.read().decode()!r}"
    )

    # Final heartbeat reflects the stopping state.
    hb = json.loads((state_dir / "heartbeat.json").read_text())
    assert hb["state"] in (runner.STATE_STOPPING, runner.STATE_IDLE)
