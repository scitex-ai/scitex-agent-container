"""End-to-end persistence of the SDK subprocess's stderr into
``session.jsonl`` on a ``sdk_runtime`` failure (sac-log-assistant-text
PARTIAL fix).

Symptom this test guards against: a runner that emitted a generic
``{"type": "error", "kind": "sdk_runtime", "detail": "Command failed exit 1"}``
event with no stderr — the lead saw the failure but not the *reason*.

What this test does (no mocks):

1. Runs a *real* failing subprocess (``bash -c 'echo <token> >&2; exit 1'``)
   and captures its stderr lines exactly the way the
   ``claude-agent-sdk``'s ``_handle_stderr`` reader does — by streaming
   ``stderr.splitlines()`` through the runner-registered ``stderr``
   callback that ``run_conversation`` puts on ``ClaudeAgentOptions``.
2. Then trips the conversation: a tiny stub SDK module whose
   ``ClaudeSDKClient.__aenter__`` raises the SDK's classic
   ``"Command failed (exit code: 1)"`` ``ProcessError`` text — i.e. the
   exact same shape as the in-production failure.
3. Asserts the ``sdk_runtime`` event persisted to ``session.jsonl``
   carries (a) the real stderr text in a dedicated ``stderr`` field and
   (b) an on-disk ``stderr_log`` pointer to ``runner-stderr.log`` —
   *both* read paths the lead has to recover the actual cause.

AAA markers each on their own line, one ``assert`` per test
(STX-TQ001 + STX-TQ007), no mocks (real subprocess + real ``tmp_path``
+ real session.jsonl writes).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import types
from pathlib import Path
from typing import Any

import scitex_agent_container._runners.claude_session as runner
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)

# Unique token a real subprocess writes to stderr so the assertion is
# checking the actual piped bytes (not some leftover from another test).
_STDERR_TOKEN = "sac-log-assistant-text-real-stderr-token"


def _real_subprocess_stderr_lines() -> list[str]:
    """Spawn a real failing subprocess and return its stderr lines.

    Mirrors the SDK's ``_handle_stderr`` reader: the parent collects
    the child's stderr line by line. Returned lines are what the SDK
    would have fed into the runner's per-line stderr callback.
    """
    proc = subprocess.run(
        ["bash", "-c", f"echo {_STDERR_TOKEN} >&2; exit 1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stderr.splitlines()


def _make_sdk_module_that_raises_on_enter() -> types.ModuleType:
    """Build a stub SDK module whose client raises on ``__aenter__``.

    Mirrors the in-production failure shape: the SDK's subprocess
    spawn raises a ``ProcessError`` whose message is the classic
    ``"Command failed (exit code: 1)\\nError output: Check stderr
    output for details"`` placeholder. The error class itself is a
    plain ``RuntimeError`` here (the supervisor only inspects
    ``str(exc)`` for classification) so the test has no SDK dep.
    """

    class _Text:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Assistant:
        def __init__(self, content):
            self.content = content

    class _User:
        pass

    class _Result:
        def __init__(self, sid, usage):
            self.session_id = sid
            self.usage = usage

    class _ProcessLikeError(RuntimeError):
        pass

    class _Client:
        def __init__(self, *, options):
            self._options = options

        async def __aenter__(self):
            raise _ProcessLikeError(
                "Command failed (exit code: 1)\n"
                "Error output: Check stderr output for details"
            )

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            if False:  # pragma: no cover - never reached
                yield None

        async def interrupt(self):
            return None

    class _HookMatcher:
        def __init__(self, *a, **kw):
            pass

    mod = types.ModuleType("fake_sdk")
    mod.AssistantMessage = _Assistant
    mod.TextBlock = _Text
    mod.UserMessage = _User
    mod.ResultMessage = _Result
    mod.ClaudeSDKClient = _Client
    mod.HookMatcher = _HookMatcher
    return mod


def _build_options_feeding_real_subprocess_stderr(stderr_lines: list[str]):
    """Build-options seam that immediately feeds REAL stderr lines into
    the runner's per-line stderr callback BEFORE the client is opened.

    The runner registers ``extra["stderr"] = stderr_capture.callback`` on
    every attempt. The SDK would invoke that callback once per piped
    stderr line; we invoke it directly with the lines a real subprocess
    just produced so the path under test (capture → enrich → persist) is
    exercised end-to-end against real bytes.
    """

    def _build(name: str, **kw) -> object:
        extra = kw.get("extra") or {}
        cb = extra.get("stderr")
        if cb is not None:
            for line in stderr_lines:
                cb(line)
        return object()

    return _build


async def _seed_one_turn_then_shutdown():
    inbox = make_inbox()
    loop = asyncio.get_running_loop()
    await inbox.put(TurnEnvelope(text="go", response=loop.create_future()))
    await inbox.put(ShutdownEnvelope())
    return inbox


def _run_until_failure_emitted(state_dir: Path) -> None:
    """Drive ``run_conversation`` with the failing-on-enter SDK stub.

    The conversation supervisor catches the ``__aenter__`` exception,
    enriches it with the captured stderr, persists the ``sdk_runtime``
    event to ``session.jsonl``, and returns (max_restarts default 0
    means a single failure terminates the runner).
    """
    sdk_mod = _make_sdk_module_that_raises_on_enter()
    stderr_lines = _real_subprocess_stderr_lines()
    build = _build_options_feeding_real_subprocess_stderr(stderr_lines)

    async def _run() -> None:
        inbox = await _seed_one_turn_then_shutdown()
        await runner._run_conversation(
            "stderr-fix-agent",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=build,
        )

    asyncio.run(_run())


def _read_sdk_runtime_event(state_dir: Path) -> dict[str, Any]:
    """Return the persisted ``sdk_runtime`` error event from session.jsonl."""
    text = (state_dir / "session.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "error" and rec.get("kind") == "sdk_runtime":
            return rec
    raise AssertionError(f"no sdk_runtime error event found in session.jsonl:\n{text}")


class TestExitOneStderrSurvivesIntoSessionJsonl:
    """The SDK subprocess's real stderr reaches the lead-visible event."""

    def test_sdk_runtime_event_carries_real_stderr_token(self, tmp_path: Path) -> None:
        # Arrange
        state_dir = tmp_path / "stderr-fix"
        # Act
        _run_until_failure_emitted(state_dir)
        event = _read_sdk_runtime_event(state_dir)
        # Assert
        assert _STDERR_TOKEN in event.get("stderr", "")

    def test_sdk_runtime_event_points_to_runner_stderr_log(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        state_dir = tmp_path / "stderr-fix"
        # Act
        _run_until_failure_emitted(state_dir)
        event = _read_sdk_runtime_event(state_dir)
        # Assert
        assert event.get("stderr_log", "").endswith("runner-stderr.log")

    def test_runner_stderr_log_on_disk_contains_real_stderr_token(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        state_dir = tmp_path / "stderr-fix"
        # Act
        _run_until_failure_emitted(state_dir)
        log_text = (state_dir / "runner-stderr.log").read_text(encoding="utf-8")
        # Assert
        assert _STDERR_TOKEN in log_text


class TestSdkRuntimeEventStillCarriesDetailAndAttempt:
    """The new stderr fields don't displace the pre-existing event shape."""

    def test_event_retains_detail_string(self, tmp_path: Path) -> None:
        # Arrange
        state_dir = tmp_path / "stderr-fix"
        # Act
        _run_until_failure_emitted(state_dir)
        event = _read_sdk_runtime_event(state_dir)
        # Assert
        assert "Command failed" in event.get("detail", "")

    def test_event_retains_attempt_field(self, tmp_path: Path) -> None:
        # Arrange
        state_dir = tmp_path / "stderr-fix"
        # Act
        _run_until_failure_emitted(state_dir)
        event = _read_sdk_runtime_event(state_dir)
        # Assert
        assert event.get("attempt") == 0


class TestEmptyCaptureOmitsStderrFields:
    """When nothing was captured (no callback fed) the stderr fields are
    omitted rather than emitted as empty placeholders — keeps the event
    honest about what it observed."""

    def test_no_stderr_field_when_capture_is_empty(self, tmp_path: Path) -> None:
        # Arrange — empty stderr lines list means the runner-registered
        # callback is never invoked, so the capture stays empty.
        state_dir = tmp_path / "empty-capture"
        sdk_mod = _make_sdk_module_that_raises_on_enter()
        build = _build_options_feeding_real_subprocess_stderr([])

        async def _run() -> None:
            inbox = await _seed_one_turn_then_shutdown()
            await runner._run_conversation(
                "empty-capture-agent",
                state_dir,
                pid=1,
                inbox=inbox,
                resume_session_id=None,
                stop=asyncio.Event(),
                sdk_module=sdk_mod,
                build_sdk_options_fn=build,
            )

        asyncio.run(_run())
        event = _read_sdk_runtime_event(state_dir)
        # Act
        has_stderr_key = "stderr" in event
        # Assert
        assert has_stderr_key is False


def test_real_failing_subprocess_emits_the_unique_token() -> None:
    """Sanity guard: the real subprocess produces the unique token on
    stderr — without this the rest of the suite would silently pass
    even if subprocess piping broke at the OS level."""
    # Arrange
    # (no setup — the subprocess is invoked in the helper)
    # Act
    lines = _real_subprocess_stderr_lines()
    # Assert
    assert any(_STDERR_TOKEN in line for line in lines)
