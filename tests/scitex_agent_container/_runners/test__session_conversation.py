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
    # Assert — sac-private extra keys present so build_sdk_options
    # registers the `sac mcp channel` adapter.
    assert captured["extra"] == {"_channels": ["server:sac"], "_a2a_port": 7878}


def test_run_conversation_extra_is_none_without_channels_or_port(
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
    # Assert — no channels and no a2a_port → no extra payload at all.
    assert captured["extra"] is None


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
