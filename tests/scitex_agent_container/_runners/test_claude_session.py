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
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)


async def _seed_inbox(mission: str):
    """Build an inbox seeded with one mission turn followed by Shutdown.

    Returns ``(inbox, response_future)`` so tests can await the future
    after ``_run_conversation`` returns.
    """
    inbox = make_inbox()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    await inbox.put(TurnEnvelope(text=mission, response=fut))
    await inbox.put(ShutdownEnvelope())
    return inbox, fut


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


class _StubHookMatcher:
    """Captures registered hook callbacks per event class."""

    instances: list["_StubHookMatcher"] = []

    def __init__(self, *, hooks: list, matcher: str | None = None) -> None:
        self.hooks = hooks
        self.matcher = matcher
        type(self).instances.append(self)


def _patch_sdk(monkeypatch) -> types.ModuleType:
    """Insert a fake ``claude_agent_sdk`` module exposing the names the
    runner imports."""
    mod = types.ModuleType("claude_agent_sdk")
    mod.AssistantMessage = _StubAssistant  # type: ignore[attr-defined]
    mod.ClaudeSDKClient = _StubClient  # type: ignore[attr-defined]
    mod.ResultMessage = _StubResult  # type: ignore[attr-defined]
    mod.TextBlock = _StubText  # type: ignore[attr-defined]
    mod.HookMatcher = _StubHookMatcher  # type: ignore[attr-defined]
    _StubHookMatcher.instances = []
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

    async def _scenario():
        inbox, _fut = await _seed_inbox("say hello")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
        )

    asyncio.run(_scenario())

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

    async def _scenario():
        inbox, _fut = await _seed_inbox("resume me")
        await runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            inbox=inbox,
            resume_session_id="prev-sid",
            stop=asyncio.Event(),
        )

    asyncio.run(_scenario())
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

    async def _scenario():
        inbox, _fut = await _seed_inbox("long task")
        await runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=stop,
        )

    asyncio.run(_scenario())
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

    async def _scenario():
        inbox, _fut = await _seed_inbox("x")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
        )

    asyncio.run(_scenario())
    rows = [
        json.loads(line)
        for line in (state_dir / "session.jsonl").read_text().splitlines()
    ]
    # Last row must be the structured error envelope.
    assert rows[-1]["type"] == "error"
    assert rows[-1]["kind"] == "sdk_missing"


# ---------------------------------------------------------------------------
# Phase 3 — quota accumulator, hook bridge
# ---------------------------------------------------------------------------


class TestQuotaAccumulator:
    def test_zeros_when_absent(self, tmp_path: Path) -> None:
        totals = runner.read_quota(tmp_path)
        assert totals == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
        }

    def test_accumulate_sums_and_increments_turns(self, tmp_path: Path) -> None:
        runner.accumulate_quota(
            tmp_path,
            {
                "input_tokens": 10,
                "output_tokens": 50,
                "cache_creation_input_tokens": 33000,
                "cache_read_input_tokens": 0,
            },
        )
        runner.accumulate_quota(
            tmp_path,
            {"input_tokens": 5, "output_tokens": 12, "cache_read_input_tokens": 200},
        )
        totals = runner.read_quota(tmp_path)
        assert totals["input_tokens"] == 15
        assert totals["output_tokens"] == 62
        assert totals["cache_creation_input_tokens"] == 33000
        assert totals["cache_read_input_tokens"] == 200
        assert totals["turns"] == 2

    def test_none_usage_is_a_noop(self, tmp_path: Path) -> None:
        runner.accumulate_quota(tmp_path, None)
        assert not (tmp_path / "quota.json").exists()
        assert runner.read_quota(tmp_path)["turns"] == 0


class TestHookBridge:
    """The runner's hooks dict must register a callback per SDK event
    class, and each callback must forward its payload to event_log
    with the right kind + fields."""

    def test_hooks_dict_has_four_event_classes(self) -> None:
        hooks = runner._build_event_log_hooks("alpha", _StubHookMatcher)
        assert set(hooks) == {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"}
        # Each event class registers exactly one matcher with one callback.
        for matchers in hooks.values():
            assert len(matchers) == 1
            assert len(matchers[0].hooks) == 1

    def test_pretool_callback_forwards_to_event_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple] = []

        def _spy(agent: str, kind: str, payload: dict, *, root=None) -> None:
            captured.append((agent, kind, payload))

        from scitex_agent_container._state import event_log

        monkeypatch.setattr(event_log, "append_event", _spy)
        hooks = runner._build_event_log_hooks("alpha", _StubHookMatcher)
        cb = hooks["PreToolUse"][0].hooks[0]
        asyncio.run(
            cb(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                "use-1",
                None,
            )
        )
        assert captured == [
            ("alpha", "pretool", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        ]

    def test_prompt_and_stop_callbacks_route_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple] = []

        def _spy(agent: str, kind: str, payload: dict, *, root=None) -> None:
            captured.append((kind, payload))

        from scitex_agent_container._state import event_log

        monkeypatch.setattr(event_log, "append_event", _spy)
        hooks = runner._build_event_log_hooks("alpha", _StubHookMatcher)
        prompt_cb = hooks["UserPromptSubmit"][0].hooks[0]
        stop_cb = hooks["Stop"][0].hooks[0]
        asyncio.run(prompt_cb({"prompt": "hi"}, None, None))
        asyncio.run(stop_cb({"stop_hook_active": True}, None, None))
        assert ("prompt", {"prompt": "hi"}) in captured
        assert ("stop", {"stop_hook_active": True}) in captured


def test_conversation_accumulates_quota_and_registers_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a single SDK turn populates quota.json and the
    HookMatcher stub captures four registered hook entries (one per
    SDK event class)."""
    _patch_sdk(monkeypatch)
    _patch_options(monkeypatch)
    state_dir = tmp_path / "alpha"

    async def _scenario():
        inbox, _fut = await _seed_inbox("go")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
        )

    asyncio.run(_scenario())
    quota = runner.read_quota(state_dir)
    assert quota["turns"] == 1
    assert quota["output_tokens"] == 7  # _StubResult usage above
    # Four HookMatcher instances created (one per event class).
    assert len(_StubHookMatcher.instances) == 4


# ---------------------------------------------------------------------------
# supervisor — auto-restart on SDK client crash
# ---------------------------------------------------------------------------


class _FlakyClient:
    """First instance raises inside __aenter__; subsequent instances behave
    like the happy-path stub."""

    instances = 0

    def __init__(self, *, options: Any) -> None:
        type(self).instances += 1
        self._first = type(self).instances == 1
        self._messages: list[Any] = [
            _StubAssistant([_StubText("recovered")]),
            _StubResult("sess-after-restart", {"output_tokens": 3}),
        ]

    async def __aenter__(self) -> "_FlakyClient":
        if self._first:
            raise RuntimeError("simulated SDK crash")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        for m in self._messages:
            yield m

    async def interrupt(self) -> None:
        pass


def test_supervisor_restarts_after_sdk_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With max_restarts=1, an SDK crash on the first attempt should be
    logged as a supervisor event, then the runner reopens the client
    and drives the queued turn successfully."""
    mod = _patch_sdk(monkeypatch)
    mod.ClaudeSDKClient = _FlakyClient  # swap in the flaky version
    _FlakyClient.instances = 0
    _patch_options(monkeypatch)
    state_dir = tmp_path / "alpha"

    async def _scenario():
        inbox, _fut = await _seed_inbox("retry me")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            max_restarts=1,
            restart_backoff_s=0.001,
        )

    asyncio.run(_scenario())

    # Two ClaudeSDKClient instances were constructed (one crash, one recovery)
    assert _FlakyClient.instances == 2
    # session.jsonl should carry an error row then a supervisor row then the
    # recovered result.
    lines = (state_dir / "session.jsonl").read_text().splitlines()
    parsed = [json.loads(line) for line in lines]
    kinds = [p.get("type") for p in parsed]
    assert "error" in kinds
    assert "supervisor" in kinds
    # The post-restart attempt produced the recovery result.
    assert runner.read_session_id(state_dir) == "sess-after-restart"


def test_supervisor_gives_up_after_max_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If every restart attempt fails, the supervisor drains the inbox
    with the last exception and exits."""

    class _AlwaysFails:
        instances = 0

        def __init__(self, *, options: Any) -> None:
            type(self).instances += 1

        async def __aenter__(self):
            raise RuntimeError("always broken")

        async def __aexit__(self, *_a):
            return None

    mod = _patch_sdk(monkeypatch)
    mod.ClaudeSDKClient = _AlwaysFails
    _patch_options(monkeypatch)
    state_dir = tmp_path / "alpha"

    async def _scenario():
        inbox = make_inbox()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await inbox.put(TurnEnvelope(text="doomed", response=fut))
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            max_restarts=2,
            restart_backoff_s=0.001,
        )
        # The pending future should now carry the failure.
        with pytest.raises(RuntimeError, match="always broken"):
            await fut

    asyncio.run(_scenario())
    # initial attempt + 2 restarts = 3 instances
    assert _AlwaysFails.instances == 3  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# F-CS3 phase 2 — _autonomous_loop tests
# ---------------------------------------------------------------------------


from scitex_agent_container._runners.claude_session import (  # noqa: E402
    _autonomous_loop,
)


def test_autonomous_loop_exits_on_drive_until_match():
    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def consumer():
            env1: TurnEnvelope = await inbox.get()
            env1.response.set_result("still working")
            env2: TurnEnvelope = await inbox.get()
            env2.response.set_result("all good — DONE here")

        consumer_task = asyncio.create_task(consumer())
        rc = await _autonomous_loop(
            inbox,
            mission="kick off",
            drive_until="DONE",
            max_turns=10,
            kick_text="continue",
            stop=stop,
            loop=loop,
        )
        await consumer_task
        return rc, stop.is_set()

    rc, stopped = asyncio.run(scenario())
    assert rc == 0
    assert stopped is True


def test_autonomous_loop_caps_at_max_turns():
    seen: list[str] = []

    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def consumer():
            for _ in range(3):
                env: TurnEnvelope = await inbox.get()
                seen.append(env.text)
                env.response.set_result("not done yet")

        consumer_task = asyncio.create_task(consumer())
        rc = await _autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=3,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        await consumer_task
        return rc, stop.is_set()

    rc, stopped = asyncio.run(scenario())
    assert rc == 1
    assert stopped is True
    assert seen[0] == "seed"
    assert seen[1:] == ["kick", "kick"]


def test_autonomous_loop_stops_when_event_set_before_loop_starts():
    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        stop.set()
        loop = asyncio.get_running_loop()
        rc = await _autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=5,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        return rc, inbox.empty()

    rc, empty = asyncio.run(scenario())
    assert rc == 1
    assert empty is True


# ---------------------------------------------------------------------------
# Merged from test_claude_session_run.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners import claude_session as runner


@pytest.fixture(autouse=True)
def _home_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


# ---------------------------------------------------------------------------
# _parse_argv
# ---------------------------------------------------------------------------


class TestParseArgv:
    def test_requires_name(self) -> None:
        with pytest.raises(SystemExit):
            runner._parse_argv([])

    def test_minimal_args(self) -> None:
        ns = runner._parse_argv(["--name", "alpha"])
        assert ns.name == "alpha"
        assert ns.state_root is None
        assert ns.tick_seconds == runner.DEFAULT_TICK_SECONDS
        assert ns.mission is None
        assert ns.resume_session_id is None
        assert ns.print_stream is False
        assert ns.a2a_port is None
        assert ns.a2a_host == "127.0.0.1"
        assert ns.autonomous_enabled is False
        assert ns.autonomous_drive_until == "DONE"
        assert ns.autonomous_max_turns == 50
        assert ns.max_restarts == 0

    def test_full_args(self) -> None:
        ns = runner._parse_argv(
            [
                "--name",
                "ag",
                "--state-root",
                "/tmp/sr",
                "--tick-seconds",
                "5",
                "--mission",
                "hi",
                "--resume-session-id",
                "abc",
                "--a2a-port",
                "9999",
                "--a2a-host",
                "0.0.0.0",
                "--print-stream",
                "--autonomous-enabled",
                "--autonomous-drive-until",
                "END",
                "--autonomous-max-turns",
                "7",
                "--autonomous-kick-text",
                "go",
                "--max-restarts",
                "3",
                "--restart-backoff-s",
                "0.25",
            ]
        )
        assert ns.name == "ag"
        assert ns.state_root == Path("/tmp/sr")
        assert ns.tick_seconds == 5.0
        assert ns.mission == "hi"
        assert ns.resume_session_id == "abc"
        assert ns.a2a_port == 9999  # stx-allow: STX-NL001
        assert ns.a2a_host == "0.0.0.0"
        assert ns.print_stream is True
        assert ns.autonomous_enabled is True
        assert ns.autonomous_drive_until == "END"
        assert ns.autonomous_max_turns == 7
        assert ns.autonomous_kick_text == "go"
        assert ns.max_restarts == 3
        assert ns.restart_backoff_s == 0.25


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_routes_args_through_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run(name, **kw):
        captured["name"] = name
        captured.update(kw)
        return 0

    monkeypatch.setattr(runner, "run", _fake_run)
    rc = runner.main(["--name", "alpha", "--tick-seconds", "0.01"])
    assert rc == 0
    assert captured["name"] == "alpha"
    assert captured["tick_seconds"] == 0.01


# ---------------------------------------------------------------------------
# run() — minimal heartbeat-only path
# ---------------------------------------------------------------------------


def test_run_no_mission_writes_pid_and_heartbeat(tmp_path: Path) -> None:
    """run() with no mission / no a2a-port should write pid + heartbeat,
    install signal handlers, and exit cleanly on SIGTERM."""

    async def _scenario() -> int:
        loop = asyncio.get_running_loop()

        async def _stop_soon():
            # let run() install its signal handlers, then send SIGTERM to self.
            await asyncio.sleep(0.05)
            # The runner registers SIGTERM via loop.add_signal_handler; we
            # can invoke the registered callback by raising the signal.
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-run-1",
            state_root=tmp_path,
            tick_seconds=0.01,
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    state_dir = tmp_path / "ag-run-1"
    assert (state_dir / "pid").is_file()
    assert (state_dir / "heartbeat.json").is_file()
    hb = runner.read_heartbeat(state_dir)
    assert hb is not None
    assert hb["state"] == runner.STATE_STOPPING


def test_run_stop_via_event_after_mission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a mission, run() should seed the inbox and spawn the convo
    task, then idle until stop. We patch _run_conversation so the test
    doesn't need the SDK."""

    drained: list[str] = []

    async def _fake_conv(
        name,
        state_dir,
        *,
        pid,
        inbox,
        resume_session_id,
        stop,
        print_stream=False,
        max_restarts=0,
        restart_backoff_s=1.0,
    ) -> None:
        # Drain inbox until ShutdownEnvelope.
        from scitex_agent_container._runners._session_inbox import (
            ShutdownEnvelope,
            TurnEnvelope,
        )

        while True:
            env = await inbox.get()
            if isinstance(env, ShutdownEnvelope):
                return
            if isinstance(env, TurnEnvelope):
                drained.append(env.text)
                if not env.response.done():
                    env.response.set_result("ack")

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-run-2",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="hello",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    # mission was placed onto the inbox and consumed by the fake convo.
    assert drained == ["hello"]


def test_run_print_stream_foreground_returns_after_convo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In foreground (print_stream=True, no autonomous), run() should
    await the convo task and return immediately."""

    async def _fake_conv(
        name,
        state_dir,
        *,
        pid,
        inbox,
        resume_session_id,
        stop,
        print_stream=False,
        max_restarts=0,
        restart_backoff_s=1.0,
    ) -> None:
        # Drain mission turn and finish.
        env = await inbox.get()
        if hasattr(env, "response") and not env.response.done():
            env.response.set_result("ok")

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        return await runner.run(
            "ag-fg",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            print_stream=True,
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    hb = runner.read_heartbeat(tmp_path / "ag-fg")
    assert hb is not None and hb["state"] == runner.STATE_STOPPING


def test_run_autonomous_path_drives_until_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() with autonomous_enabled drives turns until drive_until
    matches; the autonomous loop sets stop, which unwinds the daemon."""

    async def _fake_conv(
        name,
        state_dir,
        *,
        pid,
        inbox,
        resume_session_id,
        stop,
        print_stream=False,
        max_restarts=0,
        restart_backoff_s=1.0,
    ) -> None:
        from scitex_agent_container._runners._session_inbox import (
            ShutdownEnvelope,
            TurnEnvelope,
        )

        replies = iter(["nope", "nope", "DONE here"])
        while True:
            env = await inbox.get()
            if isinstance(env, ShutdownEnvelope):
                return
            if isinstance(env, TurnEnvelope):
                if not env.response.done():
                    try:
                        env.response.set_result(next(replies))
                    except StopIteration:
                        env.response.set_result("DONE")

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        return await runner.run(
            "ag-auto",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            autonomous_enabled=True,
            autonomous_drive_until="DONE",
            autonomous_max_turns=10,
            autonomous_kick_text="continue",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0


def test_run_a2a_port_spawns_http_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a2a_port is set, run() launches serve_inbound. We patch
    serve_inbound to a no-op so the test doesn't open sockets."""
    served: dict[str, Any] = {}

    async def _fake_serve(inbox, *, host, port, stop):
        served["host"] = host
        served["port"] = port
        # Idle until stop fires.
        await stop.wait()

    from scitex_agent_container._runners import _session_http

    monkeypatch.setattr(_session_http, "serve_inbound", _fake_serve)

    async def _fake_conv(name, state_dir, **kw):
        await kw["stop"].wait()

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-a2a",
            state_root=tmp_path,
            tick_seconds=0.01,
            a2a_port=12345,  # stx-allow: STX-NL001
            a2a_host="0.0.0.0",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    assert served["port"] == 12345  # stx-allow: STX-NL001
    assert served["host"] == "0.0.0.0"


def test_run_cancels_hung_convo_task_on_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the convo task ignores the ShutdownEnvelope and hangs, run()
    falls back to convo_task.cancel() after the 5s wait_for window. To
    keep the test fast we patch asyncio.wait_for to immediately raise
    TimeoutError on the convo path."""

    async def _hanging_conv(name, state_dir, **kw):
        # Ignore inbox + stop forever.
        while True:
            await asyncio.sleep(60)

    monkeypatch.setattr(runner, "_run_conversation", _hanging_conv)

    real_wait_for = asyncio.wait_for
    convo_seen: list[int] = []

    async def _instant_timeout(awaitable, timeout):
        # Only short-circuit the 5s convo / http waits, not other waits.
        if 4.5 <= timeout <= 5.5:
            convo_seen.append(1)
            # Cancel the awaitable so it doesn't leak.
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            else:
                awaitable.cancel()
            raise asyncio.TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _instant_timeout)

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-hang",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="hi",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    assert convo_seen, "convo task wait_for(5s) path was not exercised"
