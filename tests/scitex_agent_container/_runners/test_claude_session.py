"""Tests for the claude-session runner (no-mocks).

Replaces the previous monkeypatch-heavy suite with honest seams:

- ``run_conversation`` accepts ``sdk_module=`` and ``build_sdk_options_fn=``
  kwargs, so tests pass a hand-rolled fake SDK module (a ``types.ModuleType``
  carrying real stub classes) without touching ``sys.modules`` or
  rewriting production imports.
- ``run`` accepts ``run_conversation_fn=`` / ``serve_inbound_fn=`` /
  ``shutdown_timeout_s=`` so tests substitute the convo + http coroutines
  with real fakes and tighten the hang-recovery window.
- ``build_event_log_hooks`` accepts ``event_log_root=`` so hook callbacks
  write to ``tmp_path`` via the real ``append_event`` helper.
- ``state_dir_for`` already accepts ``root=``; the runtime root for the
  spawn-the-runner test uses ``--state-root <tmp_path>`` instead of
  rewriting ``Path.home``.

Every test follows the AAA structure (one ``# Arrange`` / ``# Act`` /
``# Assert`` block) and exactly one assertion per function.
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

# ---------------------------------------------------------------------------
# Hand-rolled SDK stand-ins (real classes — not Mock objects)
# ---------------------------------------------------------------------------


class _StubText:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubAssistant:
    def __init__(self, blocks: list) -> None:
        self.content = blocks


class _StubResult:
    def __init__(self, session_id: str, usage: dict | None = None) -> None:
        self.session_id = session_id
        self.usage = usage or {}


class _StubUser:
    """Placeholder for sdk.UserMessage — only ``isinstance`` is exercised."""


class _StubHookMatcher:
    """Captures registered hook callbacks per event class."""

    instances: list["_StubHookMatcher"] = []

    def __init__(self, *, hooks: list, matcher: str | None = None) -> None:
        self.hooks = hooks
        self.matcher = matcher
        type(self).instances.append(self)


def _make_sdk_module(
    client_cls: type, *, hook_matcher_cls: type = _StubHookMatcher
) -> types.ModuleType:
    """Build a real ``types.ModuleType`` carrying the SDK names the runner
    imports. No ``sys.modules`` mutation — passed in via ``sdk_module=``.
    """
    mod = types.ModuleType("claude_agent_sdk_stub")
    mod.AssistantMessage = _StubAssistant  # type: ignore[attr-defined]
    mod.ClaudeSDKClient = client_cls  # type: ignore[attr-defined]
    mod.ResultMessage = _StubResult  # type: ignore[attr-defined]
    mod.TextBlock = _StubText  # type: ignore[attr-defined]
    mod.UserMessage = _StubUser  # type: ignore[attr-defined]
    mod.HookMatcher = hook_matcher_cls  # type: ignore[attr-defined]
    return mod


def _fake_build_options(name: str, **kw) -> types.SimpleNamespace:
    """Honest stand-in for runtimes._sdk_common.build_sdk_options — returns
    a real namespace carrying the kwargs back so the test can assert on
    the shape the runner passed in. Injected via ``build_sdk_options_fn=``.
    """
    return types.SimpleNamespace(name=name, **kw)


class _StubClient:
    """Drop-in for ``ClaudeSDKClient`` — real async-context-manager."""

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


async def _seed_inbox(mission: str):
    """Seed an inbox with one TurnEnvelope and a ShutdownEnvelope."""
    inbox = make_inbox()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    await inbox.put(TurnEnvelope(text=mission, response=fut))
    await inbox.put(ShutdownEnvelope())
    return inbox, fut


# ---------------------------------------------------------------------------
# state_dir_for
# ---------------------------------------------------------------------------


def test_state_dir_for_returns_root_joined_with_name(tmp_path: Path) -> None:
    # Arrange
    name = "alpha"
    # Act
    result = runner.state_dir_for(name, root=tmp_path)
    # Assert
    assert result == tmp_path / "alpha"


def test_state_dir_for_does_not_create_directory(tmp_path: Path) -> None:
    # Arrange
    name = "alpha"
    # Act
    result = runner.state_dir_for(name, root=tmp_path)
    # Assert
    assert not result.exists()


def test_state_dir_for_default_root_is_under_user_home() -> None:
    # Arrange
    name = "zeta"
    # Act
    result_str = str(runner.state_dir_for(name))
    # Assert
    assert (
        "agent-container/runtime/zeta" in result_str
        or "agent-container\\runtime\\zeta" in result_str
    )


# ---------------------------------------------------------------------------
# write_pid / read_pid
# ---------------------------------------------------------------------------


def test_write_pid_then_read_pid_returns_same_value(tmp_path: Path) -> None:
    # Arrange
    pid = 12_345
    # Act
    runner.write_pid(tmp_path, pid)
    # Assert
    assert runner.read_pid(tmp_path) == pid


def test_read_pid_returns_none_when_file_missing(tmp_path: Path) -> None:
    # Arrange: no pid file exists.
    # Act
    result = runner.read_pid(tmp_path)
    # Assert
    assert result is None


def test_read_pid_returns_none_when_file_corrupt(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "pid").write_text("not-a-number\n")
    # Act
    result = runner.read_pid(tmp_path)
    # Assert
    assert result is None


def test_write_pid_creates_final_file(tmp_path: Path) -> None:
    # Arrange
    pid = 1
    # Act
    runner.write_pid(tmp_path, pid)
    # Assert
    assert (tmp_path / "pid").is_file()


def test_write_pid_leaves_no_tmp_file(tmp_path: Path) -> None:
    # Arrange
    pid = 1
    # Act
    runner.write_pid(tmp_path, pid)
    # Assert
    assert not (tmp_path / "pid.tmp").exists()


# ---------------------------------------------------------------------------
# write_heartbeat / read_heartbeat
# ---------------------------------------------------------------------------


def test_read_heartbeat_returns_pid_field(tmp_path: Path) -> None:
    # Arrange
    runner.write_heartbeat(tmp_path, pid=42, state=runner.STATE_IDLE)
    # Act
    hb = runner.read_heartbeat(tmp_path)
    # Assert
    assert hb is not None and hb["pid"] == 42


def test_read_heartbeat_returns_state_field(tmp_path: Path) -> None:
    # Arrange
    runner.write_heartbeat(tmp_path, pid=42, state=runner.STATE_IDLE)
    # Act
    hb = runner.read_heartbeat(tmp_path)
    # Assert
    assert hb is not None and hb["state"] == runner.STATE_IDLE


def test_read_heartbeat_returns_float_ts(tmp_path: Path) -> None:
    # Arrange
    runner.write_heartbeat(tmp_path, pid=42, state=runner.STATE_IDLE)
    # Act
    hb = runner.read_heartbeat(tmp_path)
    # Assert
    assert hb is not None and isinstance(hb["ts"], float)


def test_read_heartbeat_returns_none_when_missing(tmp_path: Path) -> None:
    # Arrange: no heartbeat file exists.
    # Act
    result = runner.read_heartbeat(tmp_path)
    # Assert
    assert result is None


def test_read_heartbeat_returns_none_when_corrupt(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "heartbeat.json").write_text("{not json")
    # Act
    result = runner.read_heartbeat(tmp_path)
    # Assert
    assert result is None


def test_subsequent_heartbeat_writes_overwrite_state(tmp_path: Path) -> None:
    # Arrange
    runner.write_heartbeat(tmp_path, pid=1, state=runner.STATE_STARTING)
    # Act
    runner.write_heartbeat(tmp_path, pid=1, state=runner.STATE_IDLE)
    # Assert
    hb = runner.read_heartbeat(tmp_path)
    assert hb is not None and hb["state"] == runner.STATE_IDLE


# ---------------------------------------------------------------------------
# _heartbeat_loop (in-process)
# ---------------------------------------------------------------------------


def test_heartbeat_loop_first_write_is_immediate(tmp_path: Path) -> None:
    # Arrange
    async def _scenario() -> Any:
        stop = asyncio.Event()
        task = asyncio.create_task(
            runner._heartbeat_loop(
                tmp_path, pid=os.getpid(), tick_seconds=10.0, stop=stop
            )
        )
        await asyncio.sleep(0.05)
        hb = runner.read_heartbeat(tmp_path)
        stop.set()
        await task
        return hb

    # Act
    hb = asyncio.run(_scenario())
    # Assert
    assert hb is not None


def test_heartbeat_loop_subsequent_ticks_refresh_ts(tmp_path: Path) -> None:
    # Arrange
    async def _scenario() -> tuple[dict, dict]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            runner._heartbeat_loop(
                tmp_path, pid=os.getpid(), tick_seconds=0.05, stop=stop
            )
        )
        await asyncio.sleep(0.02)
        first = runner.read_heartbeat(tmp_path)
        await asyncio.sleep(0.12)
        second = runner.read_heartbeat(tmp_path)
        stop.set()
        await task
        return first, second  # type: ignore[return-value]

    # Act
    first, second = asyncio.run(_scenario())
    # Assert
    assert second["ts"] > first["ts"]


# ---------------------------------------------------------------------------
# end-to-end subprocess: pid file is recorded; SIGTERM exits cleanly
# ---------------------------------------------------------------------------


def _wait_for_pid_file(state_dir: Path, deadline_s: float = 5.0) -> None:
    """Block until the runner has produced both pid + heartbeat files, or
    raise ``TimeoutError`` — so the caller can treat readiness as a
    precondition without paying for an extra assert."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if (state_dir / "pid").is_file() and (state_dir / "heartbeat.json").is_file():
            return
        time.sleep(0.05)
    raise TimeoutError(f"runner never wrote pid/heartbeat at {state_dir}")


@pytest.fixture
def _runner_subprocess(tmp_path: Path):
    """Spawn the runner as a child process; yield (proc, state_dir); send
    SIGTERM + wait on teardown so each test focuses on one observation."""
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
    state_dir = tmp_path / "ci-runner"
    _wait_for_pid_file(state_dir)
    try:
        yield proc, state_dir
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except (
                subprocess.TimeoutExpired
            ):  # stx-allow: fallback (reason: test teardown — kill is best-effort)
                proc.kill()


def test_subprocess_runner_writes_pid_matching_child_pid(_runner_subprocess) -> None:
    # Arrange
    proc, state_dir = _runner_subprocess
    # Act
    recorded = int((state_dir / "pid").read_text().strip())
    # Assert
    assert recorded == proc.pid


def test_subprocess_runner_exits_zero_on_sigterm(_runner_subprocess) -> None:
    # Arrange
    proc, _state_dir = _runner_subprocess
    # Act
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=10)
    # Assert
    assert rc == 0


def test_subprocess_runner_final_heartbeat_reflects_stopping(
    _runner_subprocess,
) -> None:
    # Arrange
    proc, state_dir = _runner_subprocess
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    # Act
    hb = json.loads((state_dir / "heartbeat.json").read_text())
    # Assert
    assert hb["state"] in (runner.STATE_STOPPING, runner.STATE_IDLE)


# ---------------------------------------------------------------------------
# run_conversation — happy path (sdk_module + build_sdk_options_fn injection)
# ---------------------------------------------------------------------------


def _reset_stub_state() -> None:
    _StubClient.last_options = None
    _StubClient.interrupt_calls = 0
    _StubHookMatcher.instances = []


def _run_convo(
    *,
    name: str,
    state_dir: Path,
    inbox,
    sdk_module,
    resume_session_id: str | None = None,
    stop: asyncio.Event | None = None,
    max_restarts: int = 0,
) -> None:
    async def _run():
        await runner._run_conversation(
            name,
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=resume_session_id,
            stop=stop or asyncio.Event(),
            sdk_module=sdk_module,
            build_sdk_options_fn=_fake_build_options,
            max_restarts=max_restarts,
            restart_backoff_s=0.001,
        )

    asyncio.run(_run())


def test_conversation_persists_session_id_from_result(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("say hello")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert runner.read_session_id(state_dir) == "sess-xyz"


def test_conversation_writes_session_jsonl_in_expected_order(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("say hello")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    parsed = [
        json.loads(line)
        for line in (state_dir / "session.jsonl").read_text().splitlines()
    ]
    kinds = [p["type"] for p in parsed]
    # Assert
    assert kinds == ["user", "assistant", "assistant", "result"]


def test_conversation_assistant_chunks_match_stub_text(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("say hello")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    parsed = [
        json.loads(line)
        for line in (state_dir / "session.jsonl").read_text().splitlines()
    ]
    texts = [parsed[1]["text"], parsed[2]["text"]]
    # Assert
    assert texts == ["hello", " world"]


def test_conversation_result_row_records_usage(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("say hello")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    parsed = [
        json.loads(line)
        for line in (state_dir / "session.jsonl").read_text().splitlines()
    ]
    # Assert
    assert parsed[3]["usage"] == {"output_tokens": 7}


def test_conversation_final_heartbeat_is_idle(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("say hello")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    hb = runner.read_heartbeat(state_dir)
    # Assert
    assert hb is not None and hb["state"] == runner.STATE_IDLE


# ---------------------------------------------------------------------------
# run_conversation — options forwarding
# ---------------------------------------------------------------------------


def test_conversation_forwards_resume_session_id_to_options(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("resume me")
        await runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            inbox=inbox,
            resume_session_id="prev-sid",
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert _StubClient.last_options.resume == "prev-sid"


def test_conversation_uses_bypass_permissions_mode(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("hi")
        await runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert _StubClient.last_options.permission_mode == "bypassPermissions"


# ---------------------------------------------------------------------------
# run_conversation — interrupt on stop, sdk-missing fallback
# ---------------------------------------------------------------------------


def test_conversation_calls_interrupt_when_stop_already_set(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    sdk_mod = _make_sdk_module(_StubClient)
    stop = asyncio.Event()
    stop.set()

    async def _run():
        inbox, _fut = await _seed_inbox("long task")
        await runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=stop,
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert _StubClient.interrupt_calls >= 1


class _BrokenSDKModule(types.ModuleType):
    """A module whose attribute access raises — emulates SDK-missing."""

    def __getattr__(self, name):
        raise ImportError(f"simulated absence: {name}")


def test_conversation_records_error_when_sdk_module_attribute_missing(
    tmp_path: Path,
) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    broken = _BrokenSDKModule("broken_sdk")

    async def _run():
        inbox, _fut = await _seed_inbox("x")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=broken,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    rows = [
        json.loads(line)
        for line in (state_dir / "session.jsonl").read_text().splitlines()
    ]
    # Assert
    assert rows[-1]["kind"] == "sdk_missing"


# ---------------------------------------------------------------------------
# Quota accumulator
# ---------------------------------------------------------------------------


def test_read_quota_returns_zeros_when_file_absent(tmp_path: Path) -> None:
    # Arrange: no quota file exists.
    # Act
    totals = runner.read_quota(tmp_path)
    # Assert
    assert totals == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "turns": 0,
    }


def test_accumulate_quota_sums_input_tokens(tmp_path: Path) -> None:
    # Arrange
    runner.accumulate_quota(tmp_path, {"input_tokens": 10})
    # Act
    runner.accumulate_quota(tmp_path, {"input_tokens": 5})
    # Assert
    assert runner.read_quota(tmp_path)["input_tokens"] == 15


def test_accumulate_quota_increments_turns_per_call(tmp_path: Path) -> None:
    # Arrange
    runner.accumulate_quota(tmp_path, {"input_tokens": 1})
    # Act
    runner.accumulate_quota(tmp_path, {"input_tokens": 1})
    # Assert
    assert runner.read_quota(tmp_path)["turns"] == 2


def test_accumulate_quota_with_none_does_not_create_file(tmp_path: Path) -> None:
    # Arrange: no prior write.
    # Act
    runner.accumulate_quota(tmp_path, None)
    # Assert
    assert not (tmp_path / "quota.json").exists()


# ---------------------------------------------------------------------------
# Hook bridge
# ---------------------------------------------------------------------------


def test_build_event_log_hooks_registers_four_event_classes() -> None:
    # Arrange
    _StubHookMatcher.instances = []
    # Act
    hooks = runner._build_event_log_hooks("alpha", _StubHookMatcher)
    # Assert
    assert set(hooks) == {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"}


def test_build_event_log_hooks_registers_one_callback_per_class() -> None:
    # Arrange
    _StubHookMatcher.instances = []
    # Act
    hooks = runner._build_event_log_hooks("alpha", _StubHookMatcher)
    # Assert
    assert all(len(m) == 1 and len(m[0].hooks) == 1 for m in hooks.values())


def _read_event_log(root: Path, agent: str) -> list[dict]:
    """Read back the per-agent JSONL ring buffer produced by append_event."""
    path = root / f"{agent}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_pretool_hook_writes_pretool_kind_to_event_log(tmp_path: Path) -> None:
    # Arrange
    hooks = runner._build_event_log_hooks(
        "alpha", _StubHookMatcher, event_log_root=tmp_path
    )
    cb = hooks["PreToolUse"][0].hooks[0]
    # Act
    asyncio.run(
        cb({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "use-1", None)
    )
    rows = _read_event_log(tmp_path, "alpha")
    # Assert
    assert [r["kind"] for r in rows] == ["pretool"]


def test_pretool_hook_records_tool_name(tmp_path: Path) -> None:
    # Arrange
    hooks = runner._build_event_log_hooks(
        "alpha", _StubHookMatcher, event_log_root=tmp_path
    )
    cb = hooks["PreToolUse"][0].hooks[0]
    # Act
    asyncio.run(
        cb({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "use-1", None)
    )
    rows = _read_event_log(tmp_path, "alpha")
    # Assert
    assert rows[0]["tool"] == "Bash"


def test_prompt_hook_writes_prompt_kind_to_event_log(tmp_path: Path) -> None:
    # Arrange
    hooks = runner._build_event_log_hooks(
        "alpha", _StubHookMatcher, event_log_root=tmp_path
    )
    prompt_cb = hooks["UserPromptSubmit"][0].hooks[0]
    # Act
    asyncio.run(prompt_cb({"prompt": "hi"}, None, None))
    rows = _read_event_log(tmp_path, "alpha")
    # Assert
    assert [r["kind"] for r in rows] == ["prompt"]


def test_stop_hook_writes_stop_kind_to_event_log(tmp_path: Path) -> None:
    # Arrange
    hooks = runner._build_event_log_hooks(
        "alpha", _StubHookMatcher, event_log_root=tmp_path
    )
    stop_cb = hooks["Stop"][0].hooks[0]
    # Act
    asyncio.run(stop_cb({"stop_hook_active": True}, None, None))
    rows = _read_event_log(tmp_path, "alpha")
    # Assert
    assert rows[-1]["stop_hook_active"] is True


# ---------------------------------------------------------------------------
# End-to-end: run_conversation populates quota + registers hooks
# ---------------------------------------------------------------------------


def test_conversation_accumulates_one_turn_into_quota(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("go")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert runner.read_quota(state_dir)["turns"] == 1


def test_conversation_quota_carries_output_tokens_from_stub(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("go")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert runner.read_quota(state_dir)["output_tokens"] == 7


def test_conversation_registers_four_hook_matcher_instances(tmp_path: Path) -> None:
    # Arrange
    _reset_stub_state()
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_StubClient)

    async def _run():
        inbox, _fut = await _seed_inbox("go")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert len(_StubHookMatcher.instances) == 4


# ---------------------------------------------------------------------------
# Supervisor — auto-restart on SDK client crash
# ---------------------------------------------------------------------------


class _FlakyClient:
    """First instance raises inside __aenter__; later ones recover."""

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


def test_supervisor_constructs_a_second_client_on_first_crash(tmp_path: Path) -> None:
    # Arrange
    _FlakyClient.instances = 0
    sdk_mod = _make_sdk_module(_FlakyClient)

    async def _run():
        inbox, _fut = await _seed_inbox("retry me")
        await runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
            max_restarts=1,
            restart_backoff_s=0.001,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert _FlakyClient.instances == 2


def test_supervisor_logs_supervisor_event_on_restart(tmp_path: Path) -> None:
    # Arrange
    _FlakyClient.instances = 0
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_FlakyClient)

    async def _run():
        inbox, _fut = await _seed_inbox("retry me")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
            max_restarts=1,
            restart_backoff_s=0.001,
        )

    # Act
    asyncio.run(_run())
    kinds = [
        json.loads(line).get("type")
        for line in (state_dir / "session.jsonl").read_text().splitlines()
    ]
    # Assert
    assert "supervisor" in kinds


def test_supervisor_records_post_restart_session_id(tmp_path: Path) -> None:
    # Arrange
    _FlakyClient.instances = 0
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_FlakyClient)

    async def _run():
        inbox, _fut = await _seed_inbox("retry me")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
            max_restarts=1,
            restart_backoff_s=0.001,
        )

    # Act
    asyncio.run(_run())
    # Assert
    assert runner.read_session_id(state_dir) == "sess-after-restart"


class _AlwaysFailsClient:
    instances = 0

    def __init__(self, *, options: Any) -> None:
        type(self).instances += 1

    async def __aenter__(self):
        raise RuntimeError("always broken")

    async def __aexit__(self, *_a):
        return None


def test_supervisor_gives_up_after_max_restarts(tmp_path: Path) -> None:
    # Arrange
    _AlwaysFailsClient.instances = 0
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_AlwaysFailsClient)

    async def _run():
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
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
            max_restarts=2,
            restart_backoff_s=0.001,
        )

    # Act
    asyncio.run(_run())
    # Assert: initial attempt + 2 restarts = 3 instances.
    assert _AlwaysFailsClient.instances == 3


def test_supervisor_propagates_failure_to_pending_future(tmp_path: Path) -> None:
    # Arrange
    _AlwaysFailsClient.instances = 0
    state_dir = tmp_path / "alpha"
    sdk_mod = _make_sdk_module(_AlwaysFailsClient)

    async def _run():
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
            sdk_module=sdk_mod,
            build_sdk_options_fn=_fake_build_options,
            max_restarts=2,
            restart_backoff_s=0.001,
        )
        with pytest.raises(RuntimeError, match="always broken"):
            await fut

    # Act
    coro = _run()
    # Assert
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# _autonomous_loop
# ---------------------------------------------------------------------------


def test_autonomous_loop_returns_zero_on_drive_until_match() -> None:
    # Arrange
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
        rc = await runner._autonomous_loop(
            inbox,
            mission="kick off",
            drive_until="DONE",
            max_turns=10,
            kick_text="continue",
            stop=stop,
            loop=loop,
        )
        await consumer_task
        return rc

    # Act
    rc = asyncio.run(scenario())
    # Assert
    assert rc == 0


def test_autonomous_loop_sets_stop_after_match() -> None:
    # Arrange
    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def consumer():
            env1: TurnEnvelope = await inbox.get()
            env1.response.set_result("DONE")

        consumer_task = asyncio.create_task(consumer())
        await runner._autonomous_loop(
            inbox,
            mission="kick off",
            drive_until="DONE",
            max_turns=10,
            kick_text="continue",
            stop=stop,
            loop=loop,
        )
        await consumer_task
        return stop.is_set()

    # Act
    stopped = asyncio.run(scenario())
    # Assert
    assert stopped is True


def test_autonomous_loop_returns_one_when_max_turns_capped() -> None:
    # Arrange
    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def consumer():
            for _ in range(3):
                env: TurnEnvelope = await inbox.get()
                env.response.set_result("not done yet")

        consumer_task = asyncio.create_task(consumer())
        rc = await runner._autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=3,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        await consumer_task
        return rc

    # Act
    rc = asyncio.run(scenario())
    # Assert
    assert rc == 1


def test_autonomous_loop_first_turn_uses_mission_text() -> None:
    # Arrange
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
        await runner._autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=3,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        await consumer_task

    # Act
    asyncio.run(scenario())
    # Assert
    assert seen[0] == "seed"


def test_autonomous_loop_subsequent_turns_use_kick_text() -> None:
    # Arrange
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
        await runner._autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=3,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        await consumer_task

    # Act
    asyncio.run(scenario())
    # Assert
    assert seen[1:] == ["kick", "kick"]


def test_autonomous_loop_exits_when_stop_already_set() -> None:
    # Arrange
    async def scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        stop.set()
        loop = asyncio.get_running_loop()
        rc = await runner._autonomous_loop(
            inbox,
            mission="seed",
            drive_until="DONE",
            max_turns=5,
            kick_text="kick",
            stop=stop,
            loop=loop,
        )
        return rc, inbox.empty()

    # Act
    rc, empty = asyncio.run(scenario())
    # Assert
    assert rc == 1 and empty is True


# ---------------------------------------------------------------------------
# _parse_argv
# ---------------------------------------------------------------------------


def test_parse_argv_requires_name() -> None:
    # Arrange
    argv: list[str] = []
    # Act
    call = lambda: runner._parse_argv(argv)  # noqa: E731
    # Assert
    with pytest.raises(SystemExit):
        call()


def test_parse_argv_minimal_returns_name(tmp_path_factory) -> None:
    # Arrange
    argv = ["--name", "alpha"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.name == "alpha"


def test_parse_argv_minimal_state_root_defaults_to_none() -> None:
    # Arrange
    argv = ["--name", "alpha"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.state_root is None


def test_parse_argv_minimal_tick_seconds_default() -> None:
    # Arrange
    argv = ["--name", "alpha"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.tick_seconds == runner.DEFAULT_TICK_SECONDS


def test_parse_argv_minimal_mission_defaults_to_none() -> None:
    # Arrange
    argv = ["--name", "alpha"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.mission is None


def test_parse_argv_minimal_autonomous_disabled_by_default() -> None:
    # Arrange
    argv = ["--name", "alpha"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.autonomous_enabled is False


def test_parse_argv_minimal_max_restarts_zero_by_default() -> None:
    # Arrange
    argv = ["--name", "alpha"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.max_restarts == 0


def test_parse_argv_full_state_root_parsed_as_path() -> None:
    # Arrange
    argv = ["--name", "ag", "--state-root", "/tmp/sr"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.state_root == Path("/tmp/sr")


def test_parse_argv_full_a2a_port_parsed_as_int() -> None:
    # Arrange
    argv = ["--name", "ag", "--a2a-port", "9999"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.a2a_port == 9_999


def test_parse_argv_full_print_stream_flag_sets_true() -> None:
    # Arrange
    argv = ["--name", "ag", "--print-stream"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.print_stream is True


def test_parse_argv_full_autonomous_max_turns_parsed_as_int() -> None:
    # Arrange
    argv = ["--name", "ag", "--autonomous-max-turns", "7"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.autonomous_max_turns == 7


def test_parse_argv_full_restart_backoff_parsed_as_float() -> None:
    # Arrange
    argv = ["--name", "ag", "--restart-backoff-s", "0.25"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.restart_backoff_s == 0.25


# ---------------------------------------------------------------------------
# run() — heartbeat-only path (no mission, no a2a)
# ---------------------------------------------------------------------------


def test_run_no_mission_writes_pid_file(tmp_path: Path) -> None:
    # Arrange
    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run("ag-run-1", state_root=tmp_path, tick_seconds=0.01)

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0 and (tmp_path / "ag-run-1" / "pid").is_file()


def test_run_no_mission_writes_heartbeat_with_stopping_state(tmp_path: Path) -> None:
    # Arrange
    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run("ag-run-1b", state_root=tmp_path, tick_seconds=0.01)

    # Act
    asyncio.run(_scenario())
    hb = runner.read_heartbeat(tmp_path / "ag-run-1b")
    # Assert
    assert hb is not None and hb["state"] == runner.STATE_STOPPING


# ---------------------------------------------------------------------------
# run() — mission seeds the inbox via real run_conversation_fn seam
# ---------------------------------------------------------------------------


def _make_drain_convo(drained: list[str]):
    """Real coroutine: drains inbox until ShutdownEnvelope; records prompts."""

    async def _conv(
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
        while True:
            env = await inbox.get()
            if isinstance(env, ShutdownEnvelope):
                return
            if isinstance(env, TurnEnvelope):
                drained.append(env.text)
                if not env.response.done():
                    env.response.set_result("ack")

    return _conv


def test_run_with_mission_drains_mission_text_through_inbox(tmp_path: Path) -> None:
    # Arrange
    drained: list[str] = []

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-run-2",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="hello",
            run_conversation_fn=_make_drain_convo(drained),
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0 and drained == ["hello"]


# ---------------------------------------------------------------------------
# run() — foreground (print_stream) path
# ---------------------------------------------------------------------------


def test_run_print_stream_foreground_returns_after_convo(tmp_path: Path) -> None:
    # Arrange
    async def _foreground_conv(
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
        env = await inbox.get()
        if hasattr(env, "response") and not env.response.done():
            env.response.set_result("ok")

    async def _scenario() -> int:
        return await runner.run(
            "ag-fg",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            print_stream=True,
            run_conversation_fn=_foreground_conv,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0


def test_run_print_stream_writes_stopping_heartbeat(tmp_path: Path) -> None:
    # Arrange
    async def _foreground_conv(
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
        env = await inbox.get()
        if hasattr(env, "response") and not env.response.done():
            env.response.set_result("ok")

    async def _scenario() -> int:
        return await runner.run(
            "ag-fg-2",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            print_stream=True,
            run_conversation_fn=_foreground_conv,
        )

    # Act
    asyncio.run(_scenario())
    hb = runner.read_heartbeat(tmp_path / "ag-fg-2")
    # Assert
    assert hb is not None and hb["state"] == runner.STATE_STOPPING


# ---------------------------------------------------------------------------
# run() — autonomous path drives until match
# ---------------------------------------------------------------------------


def test_run_autonomous_drives_until_match_returns_zero(tmp_path: Path) -> None:
    # Arrange
    async def _convo(
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
            run_conversation_fn=_convo,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0


# ---------------------------------------------------------------------------
# run() — a2a_port spawns the http task (real serve_inbound_fn seam)
# ---------------------------------------------------------------------------


def test_run_with_a2a_port_invokes_serve_inbound_on_supplied_port(
    tmp_path: Path,
) -> None:
    # Arrange
    served: dict[str, Any] = {}

    async def _fake_serve(inbox, *, host, port, stop, **kw):
        served["host"] = host
        served["port"] = port
        await stop.wait()

    async def _fake_conv(name, state_dir, **kw):
        await kw["stop"].wait()

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-a2a",
            state_root=tmp_path,
            tick_seconds=0.01,
            a2a_port=12_345,
            a2a_host="0.0.0.0",
            run_conversation_fn=_fake_conv,
            serve_inbound_fn=_fake_serve,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0 and served["port"] == 12_345


def test_run_with_a2a_port_forwards_host(tmp_path: Path) -> None:
    # Arrange
    served: dict[str, Any] = {}

    async def _fake_serve(inbox, *, host, port, stop, **kw):
        served["host"] = host
        served["port"] = port
        await stop.wait()

    async def _fake_conv(name, state_dir, **kw):
        await kw["stop"].wait()

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-a2a-2",
            state_root=tmp_path,
            tick_seconds=0.01,
            a2a_port=12_346,
            a2a_host="0.0.0.0",
            run_conversation_fn=_fake_conv,
            serve_inbound_fn=_fake_serve,
        )

    # Act
    asyncio.run(_scenario())
    # Assert
    assert served["host"] == "0.0.0.0"


# ---------------------------------------------------------------------------
# run() — cancels a hung convo task at the shutdown deadline
# ---------------------------------------------------------------------------


def test_run_cancels_hung_convo_at_shutdown_deadline(tmp_path: Path) -> None:
    # Arrange: a coroutine that never observes ShutdownEnvelope.
    async def _hanging_conv(name, state_dir, **kw):
        while True:
            await asyncio.sleep(60)

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-hang",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="hi",
            run_conversation_fn=_hanging_conv,
            shutdown_timeout_s=0.05,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0
