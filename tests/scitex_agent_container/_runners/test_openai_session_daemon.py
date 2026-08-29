"""The OpenAI runner on the shared session daemon (v4 step 7).

Card ``sac-v4-layering-refactor-harness-runtime-inference-20260813``:
the openai-agents runner's process lifetime now runs through
``run_session_daemon`` with :func:`run_openai_conversation` as the turn
driver — no ad-hoc loop, no lying parity flags, and the CLI entrypoint
actually runs (the pre-fix ``_openai_session_cli`` called
``asyncio.run`` with no ``import asyncio`` in the module, so every real
invocation NameError'd after argparse).

Mirrors the ``test_session_daemon_zombie_exit.py`` /
``test_session_daemon_residency.py`` harness patterns: bounded
``asyncio.wait_for`` so a regression to parking fails as a
TimeoutError; hand-rolled stand-in sessions (the ``_ScriptedClient``
idiom), never mocks; STX-TQ002 AAA + one assert per test.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners import session_daemon
from scitex_agent_container._runners._harness_session import (
    NormalizedEvent,
    RunResult,
)
from scitex_agent_container._runners._incarnation import (
    EXIT_CRASHED,
    EXIT_HARNESS_RETURNED,
    EXIT_ONESHOT_COMPLETE,
    WRITER_TURN_DRIVER,
    read_exit_record,
)
from scitex_agent_container._runners._openai_session_cli import main as cli_main
from scitex_agent_container._runners._openai_turn_driver import (
    run_openai_conversation,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)
from scitex_agent_container._runners._session_state import (
    STATE_BUSY,
    read_heartbeat,
)

#: Generous ceiling for "the daemon must EXIT on its own" — a regression
#: back to parking turns into a visible TimeoutError, not a hang.
_EXIT_DEADLINE_S = 10.0


# ---------------------------------------------------------------------------
# Stand-in vendor sessions — the HarnessSession surface, hand-rolled.
# ---------------------------------------------------------------------------


class _ScriptedOpenAISession:
    """Answers every turn with a scripted delta + terminal result."""

    def __init__(self, agent_name: str, **kwargs: Any) -> None:
        self.agent_name = agent_name
        self.kwargs = kwargs
        self.closed = False

    async def start(self) -> None:
        return None

    async def send(self, message: Any):
        yield NormalizedEvent(kind="text_delta", text="ack")
        yield NormalizedEvent(
            kind="result",
            result=RunResult(
                text="ack",
                session_id=self.agent_name,
                usage={"input_tokens": 3, "output_tokens": 2},
            ),
        )

    async def close(self) -> None:
        self.closed = True


class _ErroringOpenAISession(_ScriptedOpenAISession):
    """Yields a turn-ending ``kind="error"`` event (the Protocol contract)."""

    async def send(self, message: Any):
        yield NormalizedEvent(kind="error", error="endpoint said no")


class _SendRaisesOpenAISession(_ScriptedOpenAISession):
    """Raises OUTSIDE the Protocol contract — must surface as a crash."""

    async def send(self, message: Any):
        raise RuntimeError("vendor SDK fell over mid-turn")
        yield  # pragma: no cover — makes this an async generator


def _refusing_factory(agent_name: str, **kwargs: Any) -> Any:
    """A session that cannot even be constructed (no key, no SDK...)."""
    raise RuntimeError("no OpenAI auth available")


def _openai_driver_with(factory: Any) -> Any:
    """The REAL turn driver with the vendor session swapped for a stand-in."""
    return functools.partial(run_openai_conversation, session_factory=factory)


def _run_daemon_bounded(
    tmp_path: Path, name: str, driver: Any, *, residency: str
) -> int:
    """Run a headless mission daemon under ``residency`` with a deadline."""

    async def _scenario() -> int:
        return await asyncio.wait_for(
            session_daemon.run_session_daemon(
                name,
                turn_driver=driver,
                residency=residency,
                state_root=tmp_path,
                tick_seconds=0.01,
                mission="boot",
            ),
            timeout=_EXIT_DEADLINE_S,
        )

    return asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Daemon-level: the real openai driver under the residency axis
# ---------------------------------------------------------------------------


def test_one_shot_openai_daemon_exits_zero_on_clean_completion(
    tmp_path: Path,
) -> None:
    # Arrange: the real driver over a scripted session, declared one-shot.
    driver = _openai_driver_with(_ScriptedOpenAISession)
    # Act: mission turn completes; the driver honours exit_after.
    rc = _run_daemon_bounded(tmp_path, "ag-oa-rc", driver, residency="one-shot")
    # Assert: the declared plan is a SUCCESS exit.
    assert rc == 0


def test_one_shot_openai_completion_writes_oneshot_complete_exit_record(
    tmp_path: Path,
) -> None:
    # Arrange
    driver = _openai_driver_with(_ScriptedOpenAISession)
    # Act
    _run_daemon_bounded(tmp_path, "ag-oa-rec", driver, residency="one-shot")
    rec = read_exit_record(tmp_path / "ag-oa-rec")
    # Assert: the ExitRecord names the PLANNED end, not a violation.
    assert rec is not None and rec["reason"] == EXIT_ONESHOT_COMPLETE


def test_resident_openai_daemon_records_harness_returned_when_session_refuses(
    tmp_path: Path,
) -> None:
    # Arrange: session construction fails (no key / no SDK) → the driver
    # records + drains + RETURNS, which under resident is the residency
    # violation the daemon must account, never a green-heartbeat zombie.
    driver = _openai_driver_with(_refusing_factory)
    # Act
    _run_daemon_bounded(tmp_path, "ag-oa-hr", driver, residency="resident")
    rec = read_exit_record(tmp_path / "ag-oa-hr")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_HARNESS_RETURNED


def test_resident_openai_daemon_records_crashed_when_send_raises(
    tmp_path: Path,
) -> None:
    # Arrange: an exception out of send() is outside the HarnessSession
    # contract (errors travel as events) — it must propagate to the
    # daemon and be recorded as a crash, not swallowed.
    driver = _openai_driver_with(_SendRaisesOpenAISession)
    # Act
    _run_daemon_bounded(tmp_path, "ag-oa-cr", driver, residency="resident")
    rec = read_exit_record(tmp_path / "ag-oa-cr")
    # Assert
    assert rec is not None and rec["reason"] == EXIT_CRASHED


# ---------------------------------------------------------------------------
# CLI: argparse → asyncio.run → daemon handoff (the NameError regression gate)
# ---------------------------------------------------------------------------


async def _stub_cli_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
    """Minimal residency-honouring driver for CLI smoke runs."""
    inbox = kwargs["inbox"]
    stop = kwargs["stop"]
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope):
            if not env.response.done():
                env.response.set_result("ok")
            if env.exit_after:
                stop.set()
                return


def test_cli_main_reaches_daemon_handoff_and_exits_zero(tmp_path: Path) -> None:
    # Arrange: the full entrypoint — argparse through asyncio.run to
    # run_session_daemon — with only the vendor turn driver stubbed.
    # Pre-fix code NameError'd here on the missing asyncio import before
    # any daemon work; this pins the whole invoke path, not the lint.
    argv = [
        "--name",
        "ag-oa-cli",
        "--state-root",
        str(tmp_path),
        "--tick-seconds",
        "0.01",
        "--mission",
        "hi",
        "--residency",
        "one-shot",
    ]
    # Act
    rc = cli_main(argv, turn_driver=_stub_cli_driver)
    # Assert
    assert rc == 0


def test_cli_main_one_shot_writes_oneshot_complete_exit_record(
    tmp_path: Path,
) -> None:
    # Arrange
    argv = [
        "--name",
        "ag-oa-cli-rec",
        "--state-root",
        str(tmp_path),
        "--tick-seconds",
        "0.01",
        "--mission",
        "hi",
        "--residency",
        "one-shot",
    ]
    # Act
    cli_main(argv, turn_driver=_stub_cli_driver)
    rec = read_exit_record(tmp_path / "ag-oa-cli-rec")
    # Assert: the CLI threads --residency into the daemon for real.
    assert rec is not None and rec["reason"] == EXIT_ONESHOT_COMPLETE


def test_cli_main_refuses_resume_because_registry_declares_no_resume(
    tmp_path: Path,
) -> None:
    # Arrange: can_resume=False in the openai-agents descriptor — the
    # CLI must refuse the flag (exit 2) instead of silently remapping it.
    argv = [
        "--name",
        "ag-oa-resume",
        "--state-root",
        str(tmp_path),
        "--resume-session-id",
        "11111111-2222-3333-4444-555555555555",
    ]
    # Act
    rc = cli_main(argv, turn_driver=_stub_cli_driver)
    # Assert
    assert rc == 2


# ---------------------------------------------------------------------------
# Driver-level: turn bookkeeping, beats, transcript, refusals
# ---------------------------------------------------------------------------


def _drive_one_turn(
    tmp_path: Path,
    name: str,
    factory: Any,
    *,
    resume_session_id: str | None = None,
) -> TurnEnvelope:
    """Feed the driver one exit_after turn; return the resolved envelope."""

    async def _go() -> TurnEnvelope:
        stop = asyncio.Event()
        inbox = make_inbox()
        loop = asyncio.get_running_loop()
        env = TurnEnvelope(
            text="hi", response=loop.create_future(), exit_after=True
        )
        await inbox.put(env)
        await asyncio.wait_for(
            run_openai_conversation(
                name,
                tmp_path / name,
                pid=os.getpid(),
                inbox=inbox,
                resume_session_id=resume_session_id,
                stop=stop,
                session_factory=factory,
            ),
            timeout=_EXIT_DEADLINE_S,
        )
        return env

    return asyncio.run(_go())


def test_driver_resolves_the_turn_future_with_streamed_text(
    tmp_path: Path,
) -> None:
    # Arrange
    factory = _ScriptedOpenAISession
    # Act
    env = _drive_one_turn(tmp_path, "ag-drv-txt", factory)
    # Assert: the /v1/turn awaiter gets the assistant reply.
    assert env.response.result() == "ack"


def test_driver_tags_the_envelope_with_the_session_id(tmp_path: Path) -> None:
    # Arrange
    factory = _ScriptedOpenAISession
    # Act
    env = _drive_one_turn(tmp_path, "ag-drv-sid", factory)
    # Assert: set before the future resolves, from the terminal result.
    assert env.session_id == "ag-drv-sid"


def test_driver_appends_result_record_to_the_transcript(tmp_path: Path) -> None:
    # Arrange
    factory = _ScriptedOpenAISession
    # Act
    _drive_one_turn(tmp_path, "ag-drv-jsonl", factory)
    lines = (
        (tmp_path / "ag-drv-jsonl" / "session.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    kinds = [json.loads(line).get("type") for line in lines if line.strip()]
    # Assert: user + assistant + result all reached session.jsonl.
    assert kinds.count("result") == 1 and "user" in kinds and "assistant" in kinds


def test_driver_accumulates_turn_usage_into_quota(tmp_path: Path) -> None:
    # Arrange
    from scitex_agent_container._runners._session_state import read_quota

    # Act
    _drive_one_turn(tmp_path, "ag-drv-quota", _ScriptedOpenAISession)
    quota = read_quota(tmp_path / "ag-drv-quota")
    # Assert: the result usage fed the same totals the beats report.
    assert quota.get("turns") == 1


def test_driver_busy_beat_is_stamped_by_the_turn_driver_writer(
    tmp_path: Path,
) -> None:
    # Arrange: a session that reads the heartbeat MID-TURN, when the
    # busy beat written just before send() is the latest testimony.
    captured: dict[str, Any] = {}
    state_dir = tmp_path / "ag-drv-beat"

    class _BeatPeekSession(_ScriptedOpenAISession):
        async def send(self, message: Any):
            captured.update(read_heartbeat(state_dir) or {})
            async for event in super().send(message):
                yield event

    # Act
    _drive_one_turn(tmp_path, "ag-drv-beat", _BeatPeekSession)
    # Assert: the beat names the ACTUAL writer per the #1042 vocabulary.
    assert captured.get("writer") == WRITER_TURN_DRIVER


def test_driver_busy_beat_carries_the_busy_state(tmp_path: Path) -> None:
    # Arrange
    captured: dict[str, Any] = {}
    state_dir = tmp_path / "ag-drv-busy"

    class _BeatPeekSession(_ScriptedOpenAISession):
        async def send(self, message: Any):
            captured.update(read_heartbeat(state_dir) or {})
            async for event in super().send(message):
                yield event

    # Act
    _drive_one_turn(tmp_path, "ag-drv-busy", _BeatPeekSession)
    # Assert
    assert captured.get("state") == STATE_BUSY


def test_driver_error_event_resolves_the_future_with_the_failure(
    tmp_path: Path,
) -> None:
    # Arrange: a turn-ending kind="error" event must surface to the
    # awaiter as the real cause, never as a silent empty reply.
    factory = _ErroringOpenAISession
    # Act
    env = _drive_one_turn(tmp_path, "ag-drv-err", factory)
    # Assert
    with pytest.raises(RuntimeError, match="endpoint said no"):
        env.response.result()


def test_driver_refuses_resume_session_id_per_registry(tmp_path: Path) -> None:
    # Arrange: can_resume=False → the queued turn must fail LOUDLY with
    # the refusal instead of silently starting an unrelated conversation.
    resume_id = "deadbeef-0000-0000-0000-000000000000"
    # Act
    env = _drive_one_turn(
        tmp_path,
        "ag-drv-resume",
        _ScriptedOpenAISession,
        resume_session_id=resume_id,
    )
    # Assert
    with pytest.raises(RuntimeError, match="can_resume=False"):
        env.response.result()


def test_driver_closes_the_vendor_session_on_shutdown(tmp_path: Path) -> None:
    # Arrange: a ShutdownEnvelope (the daemon's stop path) must reach
    # session.close() so MCP subprocesses are never orphaned.
    sessions: list[_ScriptedOpenAISession] = []

    def _factory(agent_name: str, **kwargs: Any) -> _ScriptedOpenAISession:
        session = _ScriptedOpenAISession(agent_name, **kwargs)
        sessions.append(session)
        return session

    async def _go() -> bool:
        stop = asyncio.Event()
        inbox = make_inbox()
        await inbox.put(ShutdownEnvelope())
        await asyncio.wait_for(
            run_openai_conversation(
                "ag-drv-close",
                tmp_path / "ag-drv-close",
                pid=os.getpid(),
                inbox=inbox,
                resume_session_id=None,
                stop=stop,
                session_factory=_factory,
            ),
            timeout=_EXIT_DEADLINE_S,
        )
        return sessions[0].closed

    # Act
    closed = asyncio.run(_go())
    # Assert
    assert closed is True


# ---------------------------------------------------------------------------
# #1035 — a self-hosted endpoint needs chat-completions, not Responses
# ---------------------------------------------------------------------------

_API_ENV_KEYS = ("SAC_OPENAI_API", "OPENAI_BASE_URL")


@pytest.fixture
def self_hosted_env():
    """Real env pointing OPENAI_BASE_URL at a self-hosted gateway."""
    saved = {key: os.environ.get(key) for key in _API_ENV_KEYS}
    for key in _API_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:18770/v1"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def recording_agents_module():
    """A stand-in ``agents`` module recording set_default_openai_api calls."""
    module = types.ModuleType("agents")
    module.calls = []  # type: ignore[attr-defined]
    module.set_default_openai_api = module.calls.append  # type: ignore[attr-defined]
    real = sys.modules.get("agents")
    sys.modules["agents"] = module
    try:
        yield module
    finally:
        if real is None:
            sys.modules.pop("agents", None)
        else:
            sys.modules["agents"] = real


def test_driver_selects_chat_completions_for_a_self_hosted_endpoint(
    tmp_path: Path, self_hosted_env, recording_agents_module
) -> None:
    # Arrange: the fleet's gpt-oss/qwen gateways serve only
    # /v1/chat/completions; the SDK's Responses default 404s (#1035).
    # Act: one driven turn through the real driver.
    _drive_one_turn(tmp_path, "ag-drv-api", _ScriptedOpenAISession)
    # Assert: the driver reconfigured the SDK before the first turn.
    assert recording_agents_module.calls == ["chat_completions"]
