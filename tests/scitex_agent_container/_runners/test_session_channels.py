"""Daemon-runner channel/a2a wiring (sac-node-comms fix).

Locks the fix for the LOCAL push gap: the long-lived ``claude-session``
daemon must thread ``spec.claude.channels`` + its own a2a_port into
``build_sdk_options`` so the ``sac mcp channel`` adapter is registered
and ``a2a_send`` to it yields ``delivered_subscriber_count >= 1``.

Three seams under test, all with real injected collaborators (no mocks):
  * ``_parse_argv`` accepts repeatable ``--channels``.
  * ``run`` forwards ``channels`` + ``a2a_port`` to the conversation fn.
  * ``run_conversation`` packs them into the ``extra`` kwarg of the
    injected ``build_sdk_options_fn`` (``_channels`` / ``_a2a_port``).
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import scitex_agent_container._runners.claude_session as runner
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)

# ---------------------------------------------------------------------------
# _parse_argv — --channels is repeatable, defaults to None
# ---------------------------------------------------------------------------


def test_parse_argv_channels_absent_defaults_none() -> None:
    # Arrange
    argv = ["--name", "ag"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels is None


def test_parse_argv_single_channel_collected_into_list() -> None:
    # Arrange
    argv = ["--name", "ag", "--channels", "server:sac"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels == ["server:sac"]


def test_parse_argv_repeated_channels_accumulate() -> None:
    # Arrange
    argv = ["--name", "ag", "--channels", "server:sac", "--channels", "client:x"]
    # Act
    ns = runner._parse_argv(argv)
    # Assert
    assert ns.channels == ["server:sac", "client:x"]


# ---------------------------------------------------------------------------
# run() — forwards channels + a2a_port into the conversation fn
# ---------------------------------------------------------------------------


def _run_capturing_conversation(name: str, tmp_path: Path) -> dict[str, Any]:
    """Run ``run`` once with capturing conversation/serve seams.

    Returns the kwargs the conversation fn observed so each assertion
    test can check a single field.
    """
    captured: dict[str, Any] = {}

    async def _fake_serve(inbox, *, host, port, stop, **kw):
        await stop.wait()

    async def _fake_conv(_name, _state_dir, **kw):
        captured["channels"] = kw.get("channels")
        captured["a2a_port"] = kw.get("a2a_port")
        await kw["stop"].wait()

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            name,
            state_root=tmp_path,
            tick_seconds=0.01,
            a2a_port=12_345,
            channels=["server:sac"],
            run_conversation_fn=_fake_conv,
            serve_inbound_fn=_fake_serve,
        )

    asyncio.run(_scenario())
    return captured


def test_run_forwards_channels_to_conversation(tmp_path: Path) -> None:
    # Arrange
    name = "ag-chan-c"
    # Act
    captured = _run_capturing_conversation(name, tmp_path)
    # Assert
    assert captured["channels"] == ["server:sac"]


def test_run_forwards_a2a_port_to_conversation(tmp_path: Path) -> None:
    # Arrange
    name = "ag-chan-p"
    # Act
    captured = _run_capturing_conversation(name, tmp_path)
    # Assert
    assert captured["a2a_port"] == 12_345


# ---------------------------------------------------------------------------
# run_conversation() — packs channels + a2a_port into build_sdk_options extra
# ---------------------------------------------------------------------------


def _capturing_build_options(captured: dict[str, Any]):
    def _build(name: str, **kw) -> object:
        captured["extra"] = kw.get("extra")
        # Return an object that the stub SDK client accepts as options.
        return object()

    return _build


def _make_one_turn_sdk_module():
    """Minimal SDK module whose client yields one assistant + result."""
    import types

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
    # Assert — the sac-private extra keys must be present so
    # build_sdk_options registers the `sac mcp channel` adapter.
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
