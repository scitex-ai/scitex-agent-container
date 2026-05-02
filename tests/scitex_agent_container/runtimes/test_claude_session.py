"""Tests for ``runtimes/claude_session.py`` adapter (Phase 1)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scitex_agent_container._runners import claude_session as runner
from scitex_agent_container.runtimes.claude_session import (
    ClaudeSessionRuntime,
    _pid_alive,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the runner's default state root into tmp_path."""
    monkeypatch.setattr(runner, "DEFAULT_STATE_ROOT", tmp_path)
    return tmp_path


def _config(name: str = "alpha") -> SimpleNamespace:
    return SimpleNamespace(name=name)


# ---------------------------------------------------------------------------
# Synthetic-PID tests (no real subprocess; exercise control flow)
# ---------------------------------------------------------------------------


class TestIsRunning:
    def test_no_pid_file_means_not_running(self, state_root: Path) -> None:
        rt = ClaudeSessionRuntime()
        assert rt.is_running(_config()) is False  # type: ignore[arg-type]

    def test_dead_pid_means_not_running(self, state_root: Path) -> None:
        runner.write_pid(state_root / "alpha", 999_999_999)
        rt = ClaudeSessionRuntime()
        assert rt.is_running(_config()) is False  # type: ignore[arg-type]

    def test_self_pid_counts_as_running(self, state_root: Path) -> None:
        runner.write_pid(state_root / "alpha", os.getpid())
        rt = ClaudeSessionRuntime()
        assert rt.is_running(_config()) is True  # type: ignore[arg-type]


class TestStop:
    def test_no_pid_returns_true(self, state_root: Path) -> None:
        rt = ClaudeSessionRuntime()
        assert rt.stop(_config()) is True  # type: ignore[arg-type]

    def test_dead_pid_cleans_state(self, state_root: Path) -> None:
        sd = state_root / "alpha"
        runner.write_pid(sd, 999_999_999)
        runner.write_heartbeat(sd, pid=999_999_999, state=runner.STATE_IDLE)
        rt = ClaudeSessionRuntime()
        with patch("os.kill", side_effect=ProcessLookupError):
            assert rt.stop(_config()) is True  # type: ignore[arg-type]
        assert not (sd / "pid").exists()
        assert not (sd / "heartbeat.json").exists()


class TestLogs:
    def test_no_heartbeat_yields_placeholder(self, state_root: Path) -> None:
        rt = ClaudeSessionRuntime()
        out = rt.logs(_config())  # type: ignore[arg-type]
        assert "no heartbeat" in out.lower()

    def test_heartbeat_renders_as_json(self, state_root: Path) -> None:
        runner.write_heartbeat(state_root / "alpha", pid=1, state=runner.STATE_IDLE)
        rt = ClaudeSessionRuntime()
        out = rt.logs(_config())  # type: ignore[arg-type]
        assert "idle" in out


class TestPidAlive:
    def test_self_alive(self) -> None:
        assert _pid_alive(os.getpid()) is True

    def test_huge_pid_dead(self) -> None:
        assert _pid_alive(999_999_999) is False


# ---------------------------------------------------------------------------
# End-to-end: real subprocess via start/stop
# ---------------------------------------------------------------------------


@pytest.mark.timeout(20)
class TestStartStopE2E:
    def test_start_spawns_runner_and_stop_kills_it(
        self, state_root: Path, monkeypatch
    ) -> None:
        # Override the runner default the spawned child will read so it
        # writes into our tmp_path. The adapter passes the agent name on
        # argv but does NOT pass --state-root, so the env var is the
        # only way to redirect.
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(state_root))
        rt = ClaudeSessionRuntime()
        cfg = _config("e2e")
        assert rt.start(cfg) is True  # type: ignore[arg-type]
        try:
            assert rt.is_running(cfg) is True  # type: ignore[arg-type]
            # Wait briefly for the heartbeat to materialize.
            sd = state_root / "e2e"
            deadline = time.time() + 3.0
            while time.time() < deadline and not (sd / "heartbeat.json").exists():
                time.sleep(0.05)
            assert (sd / "heartbeat.json").is_file()
        finally:
            assert rt.stop(cfg) is True  # type: ignore[arg-type]
        assert rt.is_running(cfg) is False  # type: ignore[arg-type]
