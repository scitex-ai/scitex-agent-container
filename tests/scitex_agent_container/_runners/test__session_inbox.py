"""Tests for the runner's inbound-turn channel (PR1: queue + executor wiring)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from scitex_agent_container._runners import claude_session as runner
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)


class _FakeAssistantMsg:
    def __init__(self, *texts: str) -> None:
        self.content = [_FakeTextBlock(t) for t in texts]


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResultMsg:
    def __init__(self, session_id: str, usage: dict | None = None) -> None:
        self.session_id = session_id
        self.usage = usage


class _FakeSDKClient:
    """Stand-in for ``ClaudeSDKClient`` — records query()/interrupt() and
    yields a pre-canned message stream from receive_response()."""

    def __init__(self, options=None) -> None:
        self.queries: list[str] = []
        self.interrupted = 0
        # Per-turn scripted reply: list of message lists (one per query call)
        self.scripts: list[list] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def receive_response(self):
        # Pop the next scripted turn; default to a minimal valid reply.
        if self.scripts:
            msgs = self.scripts.pop(0)
        else:
            msgs = [
                _FakeAssistantMsg("ok"),
                _FakeResultMsg("sess-test", {"input_tokens": 1, "output_tokens": 1}),
            ]
        for m in msgs:
            yield m


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    from scitex_agent_container._runners import _session_state

    monkeypatch.setattr(runner, "DEFAULT_STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_state, "DEFAULT_STATE_ROOT", tmp_path)
    # Stub build_sdk_options so the conversation loop doesn't hit the
    # real Anthropic auth resolver (which fails in CI without creds).
    from types import SimpleNamespace

    from scitex_agent_container.runtimes import _sdk_common

    def _fake_build(name, **kw):
        return SimpleNamespace(name=name, **kw)

    monkeypatch.setattr(_sdk_common, "build_sdk_options", _fake_build)
    return tmp_path


def _patch_sdk_surface(fake_client: _FakeSDKClient):
    """Patch the SDK symbols imported inside _run_conversation."""
    import claude_agent_sdk

    return [
        patch.object(
            claude_agent_sdk, "ClaudeSDKClient", lambda options=None: fake_client
        ),
        patch.object(claude_agent_sdk, "AssistantMessage", _FakeAssistantMsg),
        patch.object(claude_agent_sdk, "TextBlock", _FakeTextBlock),
        patch.object(claude_agent_sdk, "ResultMessage", _FakeResultMsg),
        patch.object(claude_agent_sdk, "UserMessage", type("U", (), {})),
    ]


class TestInboxDrain:
    def test_turn_envelope_resolves_with_assistant_text(self, state_root: Path) -> None:
        """A TurnEnvelope is drained, the SDK is queried, and the future
        resolves with the concatenated assistant chunks."""
        client = _FakeSDKClient()
        client.scripts = [
            [
                _FakeAssistantMsg("Hello ", "world"),
                _FakeResultMsg("sess-1", {"input_tokens": 5, "output_tokens": 7}),
            ]
        ]

        async def _scenario() -> str:
            inbox = make_inbox()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            env = TurnEnvelope(text="hi", response=loop.create_future())
            await inbox.put(env)
            await inbox.put(ShutdownEnvelope())
            patches = _patch_sdk_surface(client)
            for p in patches:
                p.start()
            try:
                await runner._run_conversation(
                    "alpha",
                    state_root / "alpha",
                    pid=1234,
                    inbox=inbox,
                    resume_session_id=None,
                    stop=stop,
                )
            finally:
                for p in patches:
                    p.stop()
            return await env.response

        reply = asyncio.run(_scenario())
        assert reply == "Hello world"
        assert client.queries == ["hi"]

    def test_multiple_turns_processed_serially(self, state_root: Path) -> None:
        """Two TurnEnvelopes followed by Shutdown — each turn's future
        resolves independently, queries hit the SDK in order."""
        client = _FakeSDKClient()
        client.scripts = [
            [_FakeAssistantMsg("first"), _FakeResultMsg("sess-2", {})],
            [_FakeAssistantMsg("second"), _FakeResultMsg("sess-2", {})],
        ]

        async def _scenario():
            inbox = make_inbox()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            e1 = TurnEnvelope(text="q1", response=loop.create_future())
            e2 = TurnEnvelope(text="q2", response=loop.create_future())
            for env in (e1, e2):
                await inbox.put(env)
            await inbox.put(ShutdownEnvelope())
            patches = _patch_sdk_surface(client)
            for p in patches:
                p.start()
            try:
                await runner._run_conversation(
                    "beta",
                    state_root / "beta",
                    pid=2,
                    inbox=inbox,
                    resume_session_id=None,
                    stop=stop,
                )
            finally:
                for p in patches:
                    p.stop()
            return await e1.response, await e2.response

        r1, r2 = asyncio.run(_scenario())
        assert r1 == "first"
        assert r2 == "second"
        assert client.queries == ["q1", "q2"]

    def test_exit_after_sets_stop(self, state_root: Path) -> None:
        """A turn with exit_after=True signals stop after completion."""
        client = _FakeSDKClient()

        async def _scenario():
            inbox = make_inbox()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            env = TurnEnvelope(
                text="bye", response=loop.create_future(), exit_after=True
            )
            await inbox.put(env)
            patches = _patch_sdk_surface(client)
            for p in patches:
                p.start()
            try:
                await runner._run_conversation(
                    "gamma",
                    state_root / "gamma",
                    pid=3,
                    inbox=inbox,
                    resume_session_id=None,
                    stop=stop,
                )
            finally:
                for p in patches:
                    p.stop()
            return stop.is_set()

        assert asyncio.run(_scenario()) is True


class TestDrainFailedInbox:
    def test_pending_futures_get_exception(self) -> None:
        """If SDK init fails, queued turn futures are resolved with
        the failure so producers don't hang."""

        async def _scenario():
            inbox = make_inbox()
            loop = asyncio.get_running_loop()
            env = TurnEnvelope(text="x", response=loop.create_future())
            await inbox.put(env)
            await inbox.put(ShutdownEnvelope())
            runner._drain_failed_inbox(inbox, RuntimeError("boom"))
            try:
                await env.response
                return "no-exc"
            except RuntimeError as exc:
                return str(exc)

        assert asyncio.run(_scenario()) == "boom"
