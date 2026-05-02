"""Tests for the claude-session runner.

Phase 1: state-dir layout, atomic PID + heartbeat I/O, signal handling.
Phase 2: SDK conversation loop (mission, message stream, session id,
resume, interrupt, missing-SDK fallback).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

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
    """Drive the async loop via ``asyncio.run`` so the test stays
    plugin-free (no pytest-asyncio dependency)."""

    def test_first_write_is_immediate(self, tmp_path: Path) -> None:
        async def _scenario() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(
                runner._heartbeat_loop(
                    tmp_path, pid=os.getpid(), tick_seconds=10.0, stop=stop
                )
            )
            await asyncio.sleep(0.05)
            assert runner.read_heartbeat(tmp_path) is not None
            stop.set()
            await task

        asyncio.run(_scenario())

    def test_subsequent_ticks_refresh_ts(self, tmp_path: Path) -> None:
        async def _scenario() -> tuple[dict, dict]:
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
            return first, second

        first, second = asyncio.run(_scenario())
        assert second["ts"] > first["ts"]


# ---------------------------------------------------------------------------
# end-to-end: spawn the runner as its own process and signal it
# ---------------------------------------------------------------------------


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



# ---------------------------------------------------------------------------
# Tiny in-process stand-ins that match the ducktype the runner expects
# ---------------------------------------------------------------------------


class _StubText:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubAssistant:
    def __init__(self, blocks: list[_StubText]) -> None:
        self.content = blocks


class _StubResult:
    def __init__(self, session_id: str, usage: dict | None = None) -> None:
        self.session_id = session_id
        self.usage = usage or {}


class _StubClient:
    """Replaces ``claude_agent_sdk.ClaudeSDKClient`` for tests."""

    last_options: Any = None
    interrupt_calls = 0

    def __init__(self, *, options: Any) -> None:
        type(self).last_options = options
        self._messages: list[Any] = [
            _StubAssistant([_StubText("hello"), _StubText(" world")]),
            _StubResult("sess-xyz", {"output_tokens": 7}),
        ]

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        for m in self._messages:
            yield m

    async def interrupt(self) -> None:
        type(self).interrupt_calls += 1


def _patch_sdk(monkeypatch) -> types.ModuleType:
    """Insert a fake ``claude_agent_sdk`` module exposing the names the
    runner imports."""
    mod = types.ModuleType("claude_agent_sdk")
    mod.AssistantMessage = _StubAssistant  # type: ignore[attr-defined]
    mod.ClaudeSDKClient = _StubClient  # type: ignore[attr-defined]
    mod.ResultMessage = _StubResult  # type: ignore[attr-defined]
    mod.TextBlock = _StubText  # type: ignore[attr-defined]
    mod.UserMessage = type(
        "UserMessage", (), {}
    )  # unused in this test  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    # Reset the client class-level counters per test.
    _StubClient.last_options = None
    _StubClient.interrupt_calls = 0
    return mod


def _patch_options(monkeypatch) -> None:
    """Stub ``build_sdk_options`` so the runner doesn't try to look up a
    real registry entry / .mcp.json."""
    from scitex_agent_container.runtimes import _sdk_common as common

    def _fake(name, **kw):
        ns = types.SimpleNamespace(name=name, **kw)
        return ns

    monkeypatch.setattr(common, "build_sdk_options", _fake)


# ---------------------------------------------------------------------------
# happy path — assistant text + ResultMessage
# ---------------------------------------------------------------------------


def test_conversation_writes_assistant_messages_and_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sdk(monkeypatch)
    _patch_options(monkeypatch)
    state_dir = tmp_path / "alpha"

    asyncio.run(
        runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            mission="say hello",
            resume_session_id=None,
            stop=asyncio.Event(),
        )
    )

    # Session id was persisted from ResultMessage.session_id.
    assert runner.read_session_id(state_dir) == "sess-xyz"

    # session.jsonl carries the user prompt, two assistant chunks, and
    # the closing result row, in order.
    lines = (state_dir / "session.jsonl").read_text().splitlines()
    parsed = [json.loads(line) for line in lines]
    assert [p["type"] for p in parsed] == ["user", "assistant", "assistant", "result"]
    assert parsed[0]["text"] == "say hello"
    assert parsed[1]["text"] == "hello"
    assert parsed[2]["text"] == " world"
    assert parsed[3]["session_id"] == "sess-xyz"
    assert parsed[3]["usage"] == {"output_tokens": 7}

    # Heartbeat reflects post-turn idle state.
    hb = runner.read_heartbeat(state_dir)
    assert hb is not None and hb["state"] == runner.STATE_IDLE


def test_conversation_forwards_resume_session_id_to_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sdk(monkeypatch)
    _patch_options(monkeypatch)
    asyncio.run(
        runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            mission="resume me",
            resume_session_id="prev-sid",
            stop=asyncio.Event(),
        )
    )
    assert _StubClient.last_options.resume == "prev-sid"  # type: ignore[attr-defined]
    assert _StubClient.last_options.permission_mode == "bypassPermissions"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# stop-mid-stream — interrupt() is awaited
# ---------------------------------------------------------------------------


def test_conversation_calls_interrupt_when_stop_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sdk(monkeypatch)
    _patch_options(monkeypatch)
    stop = asyncio.Event()
    stop.set()  # signal stop *before* the conversation starts
    asyncio.run(
        runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            mission="long task",
            resume_session_id=None,
            stop=stop,
        )
    )
    # The runner should ask the SDK to interrupt at least once.
    assert _StubClient.interrupt_calls >= 1


# ---------------------------------------------------------------------------
# missing SDK — runner records error, exits cleanly
# ---------------------------------------------------------------------------


def test_conversation_records_error_when_sdk_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)

    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "claude_agent_sdk":
            raise ImportError("simulated absence")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    state_dir = tmp_path / "alpha"
    asyncio.run(
        runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            mission="x",
            resume_session_id=None,
            stop=asyncio.Event(),
        )
    )
    rows = [
        json.loads(line)
        for line in (state_dir / "session.jsonl").read_text().splitlines()
    ]
    # Last row must be the structured error envelope.
    assert rows[-1]["type"] == "error"
    assert rows[-1]["kind"] == "sdk_missing"
