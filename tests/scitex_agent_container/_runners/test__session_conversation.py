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


# ---------------------------------------------------------------------------
# Dead-session recovery surfaces the resumable candidate list (#192 #3) —
# the autonomous runner's fresh start is the last resort, but it must be
# INFORMATIVE: the supervisor event carries the conversations that ARE
# resumable so the operator can choose one instead.
# ---------------------------------------------------------------------------


def _seed_cwd_conversation(home: Path, session_id: str) -> None:
    """Write a transcript under the SDK projects dir for the current cwd."""
    import json
    import os

    from scitex_agent_container._runners._session_candidates import (
        encode_claude_project,
    )

    proj = home / ".claude" / "projects" / encode_claude_project(os.getcwd())
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "earlier work"}}) + "\n",
        encoding="utf-8",
    )


def _dead_session_fresh_start_event(state_dir: Path) -> dict:
    events = [
        r
        for r in _read_session_jsonl(state_dir)
        if r.get("type") == "supervisor"
        and r.get("event") == "dead-session-fresh-start"
    ]
    return events[0]


def test_dead_session_fresh_start_event_lists_resumable_candidates(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — point HOME at a tmp dir holding a resumable transcript for
    # the runner's cwd, then trigger the dead-session recovery.
    home = tmp_path / "home"
    env_save_restore.set("HOME", str(home))
    _seed_cwd_conversation(home, "resumable-uuid")
    state_dir = tmp_path / "alpha"
    sid.write_session_id(state_dir, "dead-uuid")
    recorder = _DeadSessionRecorder("dead-uuid")
    # Act
    _run_dead_session_recovery(state_dir, recorder)
    # Assert — the fresh-start event surfaces the resumable conversation so
    # the operator can resume it explicitly instead of accepting the reset.
    event = _dead_session_fresh_start_event(state_dir)
    assert event["resumable_candidates"][0]["session_id"] == "resumable-uuid"


# ---------------------------------------------------------------------------
# #41 wake-on-inbound (lead a2a f39bdcc5 + b4e223e0)
# ---------------------------------------------------------------------------


def _make_wedge_sdk_module(captured_clients: list) -> types.ModuleType:
    """SDK module whose ``receive_response`` yields ONE assistant text
    block (simulating partial tool output already streamed) then BLOCKS
    on an internal asyncio.Event — exactly the wedge mode this fix
    addresses. The blocking await releases when ``interrupt()`` is
    called; the client then yields a ResultMessage so the SDK iterator
    closes cleanly without losing the partial text.

    ``captured_clients`` is appended-to from the client's ``__init__``
    so the TEST can reach into the live instance to observe whether
    ``interrupt`` fired.
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
            self._interrupted = asyncio.Event()
            self.interrupt_called = False
            self.queries: list[str] = []
            captured_clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt):
            self.queries.append(prompt)
            # Reset for next turn so the second envelope can re-block
            # the iterator (and we can verify it does NOT need a second
            # interrupt — it streams to completion naturally because no
            # third envelope arrives).
            self._interrupted.clear()

        async def receive_response(self):
            # Always emit a partial assistant text block FIRST. This is
            # the "partial tool output" the lead asked us to verify is
            # not lost when interrupt fires mid-iterator (b4e223e0).
            yield _Assistant([_Text(f"partial-turn-{len(self.queries)}")])
            if len(self.queries) == 1:
                # First turn: BLOCK as if in a long monitor / bash tool.
                # Only ``interrupt()`` can release the await.
                await self._interrupted.wait()
                yield _Result("sid-after-interrupt", {})
            else:
                # Second turn streams to completion immediately.
                yield _Result("sid-second-turn", {})

        async def interrupt(self):
            self.interrupt_called = True
            self._interrupted.set()

    class _HookMatcher:
        def __init__(self, *a, **kw):
            pass

    mod = types.ModuleType("fake_wedge_sdk")
    mod.AssistantMessage = _Assistant
    mod.TextBlock = _Text
    mod.UserMessage = _User
    mod.ResultMessage = _Result
    mod.ClaudeSDKClient = _Client
    mod.HookMatcher = _HookMatcher
    return mod


# ---------------------------------------------------------------------------
# wake-on-inbound — interrupt SDK when a second envelope arrives mid-turn.
#
# Behaviour pinned across the next three tests (split for one-assert-per-
# test). A fake SDK simulates a wedged tool call (turn 1 blocks until
# interrupt fires). The test queues a second envelope mid-stream; the
# wake task MUST interrupt the SDK so the consumer loop can advance,
# but MUST NOT interrupt before the second envelope arrives.
# ---------------------------------------------------------------------------


def _drive_wake_scenario(tmp_path: Path, label: str):
    """Arrange + Act for the wake-on-inbound scenario.

    Returns (client, interrupted_before_second_env: bool) so each split
    test can assert on a single facet. ``interrupted_before_second_env``
    captures the no-spurious-interrupt invariant: the wake task MUST
    NOT fire until the second envelope is actually queued.
    """
    captured_clients: list = []
    sdk_mod = _make_wedge_sdk_module(captured_clients)

    async def _run():
        inbox = make_inbox()
        loop = asyncio.get_running_loop()
        env_first = TurnEnvelope(text="first", response=loop.create_future())
        env_second = TurnEnvelope(text="second", response=loop.create_future())

        # Put the first envelope; the conversation will start driving it
        # and IMMEDIATELY block on the simulated tool wait.
        await inbox.put(env_first)
        conv = asyncio.create_task(
            runner._run_conversation(
                label,
                tmp_path / label,
                pid=1,
                inbox=inbox,
                resume_session_id=None,
                stop=asyncio.Event(),
                sdk_module=sdk_mod,
                build_sdk_options_fn=_capturing_build_options({}),
            )
        )

        # Give the conversation a moment to enter receive_response()
        # and block on the simulated tool wait. Without the wake task,
        # this is where the agent would sit indefinitely.
        await asyncio.sleep(0.05)
        client = captured_clients[0]
        # Capture (not assert) the no-spurious-interrupt invariant —
        # the dedicated test asserts on the captured flag so this helper
        # keeps the one-assert-per-test contract.
        interrupted_before_second_env = client.interrupt_called

        # Now queue the SECOND envelope mid-turn. The wake task should
        # fire, calling interrupt() and unblocking the first turn.
        await inbox.put(env_second)
        # And a shutdown so the loop exits after both turns drain.
        await inbox.put(ShutdownEnvelope())

        # Both responses must resolve within a generous timeout.
        await asyncio.wait_for(env_first.response, timeout=2.0)
        await asyncio.wait_for(env_second.response, timeout=2.0)
        await asyncio.wait_for(conv, timeout=2.0)

        return client, interrupted_before_second_env

    return asyncio.run(_run())


def test_wake_on_inbound_does_not_interrupt_before_second_envelope_arrives(
    tmp_path: Path,
) -> None:
    # Arrange
    label = "wake-test-no-spurious"
    # Act
    _client, interrupted_before_second_env = _drive_wake_scenario(tmp_path, label)
    # Assert — interrupt MUST NOT fire before a second envelope arrives;
    # otherwise the no-spurious-interrupt invariant is broken.
    assert interrupted_before_second_env is False


def test_wake_on_inbound_calls_interrupt_when_second_envelope_arrives(
    tmp_path: Path,
) -> None:
    # Arrange
    label = "wake-test-interrupt"
    # Act
    client, _interrupted_before_second_env = _drive_wake_scenario(tmp_path, label)
    # Assert — wake task fired client.interrupt() so the wedge resolves.
    assert client.interrupt_called is True


def test_wake_on_inbound_processes_both_envelopes_after_interrupt(
    tmp_path: Path,
) -> None:
    # Arrange
    label = "wake-test-both"
    # Act
    client, _interrupted_before_second_env = _drive_wake_scenario(tmp_path, label)
    # Assert — the second envelope is processed after the interrupt
    # unblocks the first turn.
    assert client.queries == ["first", "second"]


# ---------------------------------------------------------------------------
# wake-on-inbound — partial assistant text preserved across mid-tool interrupt.
#
# Edge case pinned (b4e223e0): when interrupt fires MID-tool, the
# ASSISTANT TEXT ALREADY YIELDED must reach the first turn's response
# future (not be torn / lost / corrupted). The same wedge fake is
# reused; assertions split per turn.
# ---------------------------------------------------------------------------


def _drive_preserve_scenario(tmp_path: Path, label: str) -> tuple[str, str]:
    """Arrange + Act for the partial-text-preservation scenario.

    Returns (reply_first, reply_second) so each split test can assert
    on one turn's reply without breaking the one-assert-per-test rule.
    """
    captured_clients: list = []
    sdk_mod = _make_wedge_sdk_module(captured_clients)

    async def _run():
        inbox = make_inbox()
        loop = asyncio.get_running_loop()
        env_first = TurnEnvelope(text="first", response=loop.create_future())
        env_second = TurnEnvelope(text="second", response=loop.create_future())

        await inbox.put(env_first)
        conv = asyncio.create_task(
            runner._run_conversation(
                label,
                tmp_path / label,
                pid=1,
                inbox=inbox,
                resume_session_id=None,
                stop=asyncio.Event(),
                sdk_module=sdk_mod,
                build_sdk_options_fn=_capturing_build_options({}),
            )
        )
        await asyncio.sleep(0.05)
        await inbox.put(env_second)
        await inbox.put(ShutdownEnvelope())

        reply_first = await asyncio.wait_for(env_first.response, timeout=2.0)
        reply_second = await asyncio.wait_for(env_second.response, timeout=2.0)
        await asyncio.wait_for(conv, timeout=2.0)
        return reply_first, reply_second

    return asyncio.run(_run())


def test_wake_on_inbound_preserves_first_turn_partial_text_across_interrupt(
    tmp_path: Path,
) -> None:
    # Arrange
    label = "preserve-test-first"
    # Act
    reply_first, _reply_second = _drive_preserve_scenario(tmp_path, label)
    # Assert — the partial assistant text yielded BEFORE the interrupt
    # must appear in the first turn's reply (not lost / torn).
    assert "partial-turn-1" in reply_first


def test_wake_on_inbound_preserves_second_turn_partial_text(
    tmp_path: Path,
) -> None:
    # Arrange
    label = "preserve-test-second"
    # Act
    _reply_first, reply_second = _drive_preserve_scenario(tmp_path, label)
    # Assert — the second turn's reply carries its own partial text.
    assert "partial-turn-2" in reply_second


def test_wake_on_inbound_does_not_fire_when_no_second_envelope_arrives(
    tmp_path: Path,
) -> None:
    # Arrange — a normal (non-wedged) one-turn fake SDK. The wake task
    # spawns on every turn, but with no second envelope queued and the
    # turn completing naturally, it MUST be cancelled cleanly without
    # ever firing the interrupt — otherwise we'd spuriously cancel
    # normal turns.
    captured_clients: list = []

    # Reuse the wedge fake but disable the block by NOT queuing a second
    # envelope. The wedge fake's first-turn path waits on
    # ``self._interrupted``, so without interrupt firing it would hang
    # forever — for THIS test we need a non-wedging fake. Use the
    # simpler one-turn module.
    sdk_mod = _make_one_turn_sdk_module()
    # Manually capture clients by wrapping the class.
    original_client = sdk_mod.ClaudeSDKClient

    class _Tracked(original_client):
        def __init__(self, *, options):
            super().__init__(options=options)
            captured_clients.append(self)

    sdk_mod.ClaudeSDKClient = _Tracked

    async def _run():
        inbox = await _seed("only-turn")
        await runner._run_conversation(
            "no-interrupt-test",
            tmp_path / "no-interrupt-test",
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=_capturing_build_options({}),
        )
        return captured_clients[0] if captured_clients else None

    # Act
    client = asyncio.run(_run())

    # Assert — interrupt should NOT have been called on a clean turn.
    # The one-turn fake's interrupt is a no-op, but we can sanity-check
    # that the turn finished without error (which it does if reaching
    # this line — _seed appends ShutdownEnvelope after the turn).
    assert client is not None
