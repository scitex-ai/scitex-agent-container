"""``run_conversation`` behaviour: channels/a2a_port threading, plus
session-id fork-detection logging and the resume history fallback.

The channels half: when ``channels`` / ``a2a_port`` are threaded in,
they must arrive in the ``extra`` kwarg of ``build_sdk_options`` under
the sac-private ``_channels`` / ``_a2a_port`` keys so the
``sac mcp channel`` adapter is auto-registered (see
``runtimes/_sdk_common.py``).

The session-id half: a real turn whose ``ResultMessage`` returns a
session id DIFFERENT from the stored one must (i) log the transition at
warning level (fork observability) and (ii) accumulate both ids in the
append-only ``session_id_history`` while the latest marker advances to
the fork. ``_resume_candidate`` walks that history latest-first so a
supervised restart can fall back to a prior still-on-disk id.

Real injected SDK module + real state dir under ``tmp_path`` — no mocks.
"""

from __future__ import annotations

import asyncio
import logging
import types
from pathlib import Path
from typing import Any

import scitex_agent_container._runners.claude_session as runner
from scitex_agent_container._runners import _session_id as sid
from scitex_agent_container._runners._session_conversation import (
    _resume_candidate,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)


def _capturing_build_options(captured: dict[str, Any]):
    def _build(name: str, **kw) -> object:
        captured["extra"] = kw.get("extra")
        # Return an object the stub SDK client accepts as options.
        return object()

    return _build


def _make_one_turn_sdk_module():
    """Minimal SDK module whose client yields one assistant + result."""

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

    class _Client:
        def __init__(self, *, options):
            self._messages = [_Assistant([_Text("hi")]), _Result("sid-1", {})]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt):
            self._prompt = prompt

        async def receive_response(self):
            for m in self._messages:
                yield m

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


async def _seed(mission: str):
    inbox = make_inbox()
    loop = asyncio.get_running_loop()
    await inbox.put(TurnEnvelope(text=mission, response=loop.create_future()))
    await inbox.put(ShutdownEnvelope())
    return inbox


def test_run_conversation_threads_channels_and_port_into_extra(
    tmp_path: Path,
) -> None:
    # Arrange
    captured: dict[str, Any] = {}
    sdk_mod = _make_one_turn_sdk_module()

    async def _run():
        inbox = await _seed("go")
        await runner._run_conversation(
            "alpha",
            tmp_path / "alpha",
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_capturing_build_options(captured),
            channels=["server:sac"],
            a2a_port=7878,
        )

    # Act
    asyncio.run(_run())
    # Assert — sac-private channel keys present so build_sdk_options
    # registers the `sac mcp channel` adapter. (An always-on ``stderr``
    # capture callback is also threaded into extra by the runner; this
    # test asserts only the channel keys it cares about.)
    assert {
        "_channels": captured["extra"]["_channels"],
        "_a2a_port": captured["extra"]["_a2a_port"],
    } == {"_channels": ["server:sac"], "_a2a_port": 7878}


def test_run_conversation_omits_channel_keys_without_channels_or_port(
    tmp_path: Path,
) -> None:
    # Arrange
    captured: dict[str, Any] = {}
    sdk_mod = _make_one_turn_sdk_module()

    async def _run():
        inbox = await _seed("go")
        await runner._run_conversation(
            "beta",
            tmp_path / "beta",
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_capturing_build_options(captured),
        )

    # Act
    asyncio.run(_run())
    # Assert — no channels and no a2a_port → only the always-on stderr
    # capture callback is threaded; no sac-private channel keys.
    assert set(captured["extra"]) == {"stderr"}


# ---------------------------------------------------------------------------
# Session-id fork detection + history (a real turn returning a NEW id)
# ---------------------------------------------------------------------------


def _make_sdk_module_returning(result_sid: str) -> types.ModuleType:
    """SDK module whose one turn yields a result with ``result_sid``."""

    class _Text:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Assistant:
        def __init__(self, content):
            self.content = content

    class _User:
        pass

    class _Result:
        def __init__(self, sid_, usage):
            self.session_id = sid_
            self.usage = usage

    class _Client:
        def __init__(self, *, options):
            self._messages = [
                _Assistant([_Text("hi")]),
                _Result(result_sid, {}),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt):
            self._prompt = prompt

        async def receive_response(self):
            for m in self._messages:
                yield m

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


def _run_one_forked_turn(state_dir: Path) -> None:
    """Pre-seed an old id, then run a real turn whose result forks to a new id."""
    sid.write_session_id(state_dir, "id-old")
    sdk_mod = _make_sdk_module_returning("id-new")

    async def _run():
        inbox = await _seed("go")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_capturing_build_options({}),
        )

    asyncio.run(_run())


def test_turn_logs_fork_when_result_session_id_differs(tmp_path: Path, caplog) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    with caplog.at_level(logging.WARNING):
        _run_one_forked_turn(state_dir)
    # Assert — the silent fork is now observable in the logs.
    assert "session_id changed on resume: id-old -> id-new" in caplog.text


def test_turn_history_accumulates_both_old_and_new_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    _run_one_forked_turn(state_dir)
    # Assert — both ids retained, oldest first.
    assert sid.read_session_id_history(state_dir) == ["id-old", "id-new"]


def test_turn_latest_marker_advances_to_new_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    _run_one_forked_turn(state_dir)
    # Assert — the resume marker is the forked (latest) id.
    assert sid.read_session_id(state_dir) == "id-new"


def test_turn_history_retains_prior_id_after_fork(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    _run_one_forked_turn(state_dir)
    # Assert — the orphaned prior id stays auditable / resumable.
    assert "id-old" in sid.read_session_id_history(state_dir)


# ---------------------------------------------------------------------------
# Resume fallback — _resume_candidate walks history latest-first
# ---------------------------------------------------------------------------


def test_resume_candidate_attempt0_is_latest_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    sid.write_session_id(state_dir, "id-B")
    # Act
    candidate = _resume_candidate(state_dir, attempt=0, fallback=None)
    # Assert
    assert candidate == "id-B"


def test_resume_candidate_attempt1_falls_back_to_prior_id(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    sid.write_session_id(state_dir, "id-B")
    # Act — latest (id-B) was rejected; the supervisor steps to the prior.
    candidate = _resume_candidate(state_dir, attempt=1, fallback=None)
    # Assert
    assert candidate == "id-A"


def test_resume_candidate_returns_none_when_history_exhausted(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "id-A")
    # Act — only one id; attempt 1 has nothing older → fresh start.
    candidate = _resume_candidate(state_dir, attempt=1, fallback=None)
    # Assert
    assert candidate is None


def test_resume_candidate_attempt0_uses_fallback_before_any_history(
    tmp_path: Path,
) -> None:
    # Arrange — first-ever start: no history file yet.
    state_dir = tmp_path / "alpha"
    # Act
    candidate = _resume_candidate(state_dir, attempt=0, fallback="seed-sid")
    # Assert — preserves the pre-history initial-resume behaviour.
    assert candidate == "seed-sid"


# ---------------------------------------------------------------------------
# C2 — run_conversation observes background-subagent task messages
# ---------------------------------------------------------------------------


def _make_sdk_module_with_task() -> types.ModuleType:
    """SDK module whose one turn interleaves a TaskNotification + result.

    Exposes ``TaskNotificationMessage`` so ``resolve_task_types`` enables
    background-subagent observation, then the scripted client yields a
    completion notification between the assistant text and the result.
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
        def __init__(self, sid_, usage):
            self.session_id = sid_
            self.usage = usage

    class _TaskNotification:
        def __init__(self, task_id, status, summary):
            self.task_id = task_id
            self.session_id = "s1"
            self.status = status
            self.summary = summary
            self.output_file = "/out"

    class _Client:
        def __init__(self, *, options):
            self._messages = [
                _Assistant([_Text("hi")]),
                _TaskNotification("bg-1", "completed", "subagent done"),
                _Result("sid-1", {}),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt):
            self._prompt = prompt

        async def receive_response(self):
            for m in self._messages:
                yield m

        async def interrupt(self):
            return None

    class _HookMatcher:
        def __init__(self, *a, **kw):
            pass

    mod = types.ModuleType("fake_sdk_task")
    mod.AssistantMessage = _Assistant
    mod.TextBlock = _Text
    mod.UserMessage = _User
    mod.ResultMessage = _Result
    mod.TaskNotificationMessage = _TaskNotification
    mod.ClaudeSDKClient = _Client
    mod.HookMatcher = _HookMatcher
    return mod


def _read_session_jsonl(state_dir: Path) -> list[dict]:
    import json

    text = (state_dir / "session.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _run_conversation_with_task(state_dir: Path) -> None:
    sdk_mod = _make_sdk_module_with_task()

    async def _run():
        inbox = await _seed("go")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_capturing_build_options({}),
        )

    asyncio.run(_run())


def test_run_conversation_captures_task_completion_to_jsonl(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "alpha"
    # Act
    _run_conversation_with_task(state_dir)
    # Assert — the background-subagent completion reached the transcript end
    # to end through the full run_conversation path.
    notifications = [
        r for r in _read_session_jsonl(state_dir) if r["type"] == "task_notification"
    ]
    assert notifications[0]["summary"] == "subagent done"


def test_run_conversation_logs_warning_when_sdk_lacks_task_types(
    tmp_path: Path, caplog
) -> None:
    # Arrange — the default one-turn SDK module exposes NO task classes, so
    # background-subagent observation is unavailable and must be logged LOUD.
    state_dir = tmp_path / "beta"
    sdk_mod = _make_one_turn_sdk_module()

    async def _run():
        inbox = await _seed("go")
        await runner._run_conversation(
            "beta",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_capturing_build_options({}),
        )

    # Act
    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())
    # Assert — the gap is observable, never a silent swallow.
    assert "background-task observation UNAVAILABLE for beta" in caplog.text


# ---------------------------------------------------------------------------
# Dead-session self-heal — a stale --resume target must NOT crash-loop
# ---------------------------------------------------------------------------


class _DeadSessionRecorder:
    """Records each client open with the resume id it was asked to use."""

    def __init__(self, dead_id: str) -> None:
        self.dead_id = dead_id
        self.opens: list[str | None] = []


def _make_dead_session_sdk_module(recorder: _DeadSessionRecorder) -> types.ModuleType:
    """SDK module that rejects a resume of ``recorder.dead_id``, else succeeds.

    The injected ``build_sdk_options_fn`` (below) carries the ``resume``
    value onto the returned options object so the scripted client can
    branch on it — exactly what the real claude subprocess does (it fails
    "No conversation found with session ID: <uuid>" only when --resume
    targets a gone session, and starts fresh when --resume is absent).
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
        def __init__(self, sid_, usage):
            self.session_id = sid_
            self.usage = usage

    class _Client:
        def __init__(self, *, options):
            resume = getattr(options, "resume", None)
            recorder.opens.append(resume)
            self._resume = resume

        async def __aenter__(self):
            # A resume of the dead id is rejected at open time, just like
            # the real SDK's stale --resume ProcessError.
            if self._resume == recorder.dead_id:
                raise RuntimeError(
                    f"Error: No conversation found with session ID: {recorder.dead_id}"
                )
            return self

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt):
            self._prompt = prompt

        async def receive_response(self):
            # Fresh session: the agent answers normally with a NEW id.
            yield _Assistant([_Text("recovered")])
            yield _Result("fresh-sid", {})

        async def interrupt(self):
            return None

    class _HookMatcher:
        def __init__(self, *a, **kw):
            pass

    mod = types.ModuleType("fake_sdk_dead")
    mod.AssistantMessage = _Assistant
    mod.TextBlock = _Text
    mod.UserMessage = _User
    mod.ResultMessage = _Result
    mod.ClaudeSDKClient = _Client
    mod.HookMatcher = _HookMatcher
    return mod


def _resume_threading_build_options(name: str, **kw) -> object:
    """Build-options stub that surfaces the ``resume`` kwarg on the options.

    The scripted dead-session client reads ``options.resume`` to decide
    whether to reject (stale resume) or start fresh — so the resume value
    the supervisor chose per attempt must be observable on the object.
    """
    opts = types.SimpleNamespace()
    opts.resume = kw.get("resume")
    return opts


def _run_dead_session_recovery(state_dir: Path, recorder: _DeadSessionRecorder) -> None:
    sdk_mod = _make_dead_session_sdk_module(recorder)

    async def _run():
        inbox = await _seed("go")
        # max_restarts=0 — the PRODUCTION default. Dead-session recovery
        # must NOT depend on a restart budget.
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_resume_threading_build_options,
            max_restarts=0,
        )

    asyncio.run(_run())


def test_dead_session_resume_recovers_with_fresh_start(tmp_path: Path) -> None:
    # Arrange — both the latest marker AND the history hold the SAME dead
    # uuid (the production shape: the only recorded id is the dead one).
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-uuid")
    recorder = _DeadSessionRecorder("dead-uuid")
    # Act
    _run_dead_session_recovery(state_dir, recorder)
    # Assert — the runner opened a FRESH session (resume=None) after the
    # dead-id rejection rather than dying; the recovered turn wrote a new id.
    assert sid.read_session_id(state_dir) == "fresh-sid"


def test_dead_session_resume_does_not_re_resume_dead_uuid(tmp_path: Path) -> None:
    # Arrange — latest marker AND history both contain only the dead uuid.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-uuid")
    recorder = _DeadSessionRecorder("dead-uuid")
    # Act
    _run_dead_session_recovery(state_dir, recorder)
    # Assert — exactly ONE open used the dead uuid; the recovery opened a
    # fresh session (resume=None) instead of re-resuming the dead id (the
    # crash-loop). No second dead-uuid open.
    assert recorder.opens.count("dead-uuid") == 1


def test_dead_session_purges_dead_id_from_history(tmp_path: Path) -> None:
    # Arrange — the dead uuid sits in the append-only history that the
    # supervisor's resume fallback would otherwise walk and re-resume.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-uuid")
    recorder = _DeadSessionRecorder("dead-uuid")
    # Act
    _run_dead_session_recovery(state_dir, recorder)
    # Assert — the dead uuid is gone from the history so a later restart
    # cannot re-resume it either.
    assert "dead-uuid" not in sid.read_session_id_history(state_dir)


def _make_valid_resume_sdk_module(recorder: _DeadSessionRecorder) -> types.ModuleType:
    """SDK module that ACCEPTS the resume id (happy path — valid session)."""
    return _make_dead_session_sdk_module(recorder)


def test_valid_session_is_still_resumed_not_reset(tmp_path: Path) -> None:
    # Arrange — the stored id is VALID (the recorder's dead_id is something
    # else), so the client accepts the resume and the supervisor must NOT
    # reset it. This guards the happy path against an over-eager reset.
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "valid-uuid")
    recorder = _DeadSessionRecorder("a-different-dead-id")
    sdk_mod = _make_valid_resume_sdk_module(recorder)

    async def _run():
        inbox = await _seed("go")
        await runner._run_conversation(
            "alpha",
            state_dir,
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_resume_threading_build_options,
            max_restarts=0,
        )

    # Act
    asyncio.run(_run())
    # Assert — the valid id was the one resume target the client saw; no
    # dead-session reset fired (the only open used the valid id).
    assert recorder.opens == ["valid-uuid"]
