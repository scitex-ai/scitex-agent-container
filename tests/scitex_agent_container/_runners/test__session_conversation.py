"""``run_conversation`` packs channels + a2a_port into build_sdk_options.

The conversation half of the sac-node-comms fix: when ``channels`` /
``a2a_port`` are threaded in, they must arrive in the ``extra`` kwarg of
``build_sdk_options`` under the sac-private ``_channels`` / ``_a2a_port``
keys so the ``sac mcp channel`` adapter is auto-registered (see
``runtimes/_sdk_common.py``). Real injected SDK module + build-options
seam — no mocks.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Any

import scitex_agent_container._runners.claude_session as runner
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
