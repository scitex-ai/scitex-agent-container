"""Loud auth-failure reporting from the claude-session supervisor.

When the SDK conversation dies because the Anthropic OAuth token has
expired/rotated (a 401 mid-session), or option-building fails because
the credentials file is missing/expired, the supervisor must record a
LOUD, specific error: ``cause="auth-expired"`` with the ``claude login``
refresh hint in the detail — never the ambiguous ``sdk-crash`` /
``sdk-options`` that the operator only notices by the silence.

No mocks: a real capturing DB-writer object is injected via the
``db_writer`` seam, and the SDK is a real injected fake module whose
client raises the auth error.

Style: AAA markers, one assert per test.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import scitex_agent_container._runners.claude_session as runner
from scitex_agent_container._runners._session_inbox import (
    TurnEnvelope,
    make_inbox,
)


class _CapturingDBWriter:
    """Real writer object that records every diary call (no mock libs)."""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.heartbeats: list[dict] = []
        self.turns: list[dict] = []

    def record_error(self, **kwargs):
        self.errors.append(kwargs)
        return len(self.errors)

    def record_heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)

    def record_turn(self, **kwargs):
        self.turns.append(kwargs)


def _sdk_module_raising(exc: Exception):
    """Injected SDK module whose client raises ``exc`` on the first query."""

    class _Text:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Assistant:
        def __init__(self, content):
            self.content = content

    class _User:
        pass

    class _Result:
        pass

    class _Client:
        def __init__(self, *, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def query(self, prompt):
            raise exc

        async def receive_response(self):
            if False:  # pragma: no cover — never reached; query raises first
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


def _run_until_done(sdk_mod, writer, *, build_options_fn, max_restarts=0):
    async def _go():
        inbox = make_inbox()
        loop = asyncio.get_running_loop()
        await inbox.put(TurnEnvelope(text="go", response=loop.create_future()))
        await runner._run_conversation(
            "agent-x",
            Path("/tmp"),  # state_dir not asserted on; writes go via db_writer
            pid=1,
            inbox=inbox,
            resume_session_id=None,
            stop=asyncio.Event(),
            sdk_module=sdk_mod,
            build_sdk_options_fn=build_options_fn,
            host="testhost",
            db_writer=writer,
            max_restarts=max_restarts,
        )

    asyncio.run(_go())


def _ok_build_options(name, **kw):
    return object()


class TestSessionAuthFailureLoud:
    """A 401 mid-session is reported as auth-expired with the hint."""

    def test_session_401_records_auth_expired_cause(self, tmp_path: Path) -> None:
        # Arrange
        writer = _CapturingDBWriter()
        sdk_mod = _sdk_module_raising(RuntimeError("API error: 401 Unauthorized"))

        def _build(name, **kw):
            return object()

        # Act
        _run_until_done(sdk_mod, writer, build_options_fn=_build)
        # Assert
        assert any(e["cause"] == "auth-expired" for e in writer.errors)

    def test_session_401_detail_carries_refresh_hint(self, tmp_path: Path) -> None:
        # Arrange
        writer = _CapturingDBWriter()
        sdk_mod = _sdk_module_raising(RuntimeError("API error: 401 Unauthorized"))
        # Act
        _run_until_done(sdk_mod, writer, build_options_fn=_ok_build_options)
        # Assert
        assert any("claude login" in (e.get("detail") or "") for e in writer.errors)

    def test_auth_failure_is_terminal_no_retry(self, tmp_path: Path) -> None:
        # Arrange — max_restarts=3, but an auth failure must NOT retry.
        writer = _CapturingDBWriter()
        sdk_mod = _sdk_module_raising(RuntimeError("401 invalid api key"))
        # Act
        _run_until_done(
            sdk_mod, writer, build_options_fn=_ok_build_options, max_restarts=3
        )
        # Assert — exactly one error row, not one-per-retry.
        assert len(writer.errors) == 1


class TestNonAuthFailureStillGeneric:
    """A non-auth crash keeps the generic sdk-crash cause."""

    def test_network_crash_records_sdk_crash(self, tmp_path: Path) -> None:
        # Arrange
        writer = _CapturingDBWriter()
        sdk_mod = _sdk_module_raising(RuntimeError("Connection reset by peer"))
        # Act
        _run_until_done(sdk_mod, writer, build_options_fn=_ok_build_options)
        # Assert
        assert any(e["cause"] == "sdk-crash" for e in writer.errors)


class TestOptionBuildAuthFailureLoud:
    """Missing/expired creds at option-build time → loud auth-expired."""

    def test_missing_creds_options_failure_is_auth_expired(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        from scitex_agent_container.runtimes._sdk_common import SDKCommonError

        writer = _CapturingDBWriter()
        sdk_mod = _sdk_module_raising(RuntimeError("unused — build fails first"))

        def _build_raises(name, **kw):
            raise SDKCommonError(
                "no Anthropic auth available — run `claude /login` so "
                "/x/.credentials.json exists, or export SAC_ANTHROPIC_API_KEY."
            )

        # Act
        _run_until_done(sdk_mod, writer, build_options_fn=_build_raises)
        # Assert
        assert any(e["cause"] == "auth-expired" for e in writer.errors)
