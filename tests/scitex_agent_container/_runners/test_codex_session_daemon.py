"""The codex runner on the shared session daemon (the fourth harness).

Card ``sac-codex-python-sdk-harness-20260814``. Mirrors
``test_openai_session_daemon.py`` — bounded ``asyncio.wait_for`` so a
regression to parking fails as a TimeoutError; hand-rolled stand-in
sessions (the ``_Scripted*`` idiom), never mocks; AAA + one assert per
test.

What is DIFFERENT here, and why these tests exist beyond parity:

* ``can_resume=True``. Every prior runner-hosted harness refused
  ``--resume-session-id``; codex ACCEPTS it and threads it through as
  the codex thread id. So the resume tests assert the opposite branch
  of the registry-derived gate.
* The optional dependency is a 285 MB native binary wheel. The
  import-hint test pins that a MISSING ``openai-codex`` surfaces as
  ``CodexSessionError`` with a pip hint, never a bare ``ImportError``
  from an unrelated frame — and that merely IMPORTING the module and
  CONSTRUCTING a session works without the SDK at all.
* ``normalize_thread_item`` is pure and duck-typed on the SDK's ``type``
  discriminator, so every branch is exercised with hand-built items —
  no SDK, no subprocess, no network.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scitex_agent_container._runners import session_daemon
from scitex_agent_container._runners._codex_session_cli import main as cli_main
from scitex_agent_container._runners._codex_turn_driver import run_codex_conversation
from scitex_agent_container._runners._harness_session import (
    NormalizedEvent,
    RunResult,
)
from scitex_agent_container._runners._incarnation import (
    EXIT_CRASHED,
    EXIT_HARNESS_RETURNED,
    EXIT_ONESHOT_COMPLETE,
    read_exit_record,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
)
from scitex_agent_container._runners.codex_session import (
    CodexSession,
    CodexSessionError,
    normalize_thread_item,
    usage_as_dict,
)

#: Generous ceiling for "the daemon must EXIT on its own" — a regression
#: back to parking turns into a visible TimeoutError, not a hang.
_EXIT_DEADLINE_S = 10.0


# ---------------------------------------------------------------------------
# Stand-in vendor sessions — the HarnessSession surface, hand-rolled.
# ---------------------------------------------------------------------------


class _ScriptedCodexSession:
    """Answers every turn with a scripted delta + terminal result."""

    def __init__(self, agent_name: str, **kwargs: Any) -> None:
        self.agent_name = agent_name
        self.kwargs = kwargs
        self.thread_id = kwargs.get("thread_id")
        self.closed = False

    async def start(self) -> None:
        return None

    async def send(self, message: Any):
        yield NormalizedEvent(kind="text_delta", text="ack")
        yield NormalizedEvent(
            kind="result",
            result=RunResult(
                text="ack",
                session_id=self.thread_id or "thr_new",
                usage={"input_tokens": 3, "output_tokens": 2},
            ),
        )

    async def close(self) -> None:
        self.closed = True


class _SendRaisesCodexSession(_ScriptedCodexSession):
    """Raises OUTSIDE the Protocol contract — must surface as a crash."""

    async def send(self, message: Any):
        raise RuntimeError("codex app-server fell over mid-turn")
        yield  # pragma: no cover — makes this an async generator


def _refusing_factory(agent_name: str, **kwargs: Any) -> Any:
    """A session that cannot even be constructed (no SDK, no auth...)."""
    raise CodexSessionError("openai-codex is not installed")


def _codex_driver_with(factory: Any) -> Any:
    """The REAL turn driver with the vendor session swapped for a stand-in."""
    return functools.partial(run_codex_conversation, session_factory=factory)


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


@pytest.fixture
def sdk_absent():
    """Make ``import openai_codex`` fail, restoring the real state after.

    A real ``sys.modules`` sentinel rather than a patched import hook:
    ``None`` in ``sys.modules`` is exactly what CPython raises
    ``ImportError`` on, so the production ``_import_codex`` runs its
    genuine failure path.
    """
    previous = sys.modules.get("openai_codex", "__absent__")
    sys.modules["openai_codex"] = None
    yield
    if previous == "__absent__":
        sys.modules.pop("openai_codex", None)
    else:
        sys.modules["openai_codex"] = previous


# ---------------------------------------------------------------------------
# Daemon-level: the real codex driver under the residency axis
# ---------------------------------------------------------------------------


def test_one_shot_codex_daemon_exits_zero_on_clean_completion(tmp_path):
    # Arrange: the real driver over a scripted session, declared one-shot.
    driver = _codex_driver_with(_ScriptedCodexSession)
    # Act: mission turn completes; the driver honours exit_after.
    rc = _run_daemon_bounded(tmp_path, "ag-cx-rc", driver, residency="one-shot")
    # Assert: the declared plan is a SUCCESS exit.
    assert rc == 0


def test_one_shot_codex_completion_writes_oneshot_complete_exit_record(tmp_path):
    # Arrange
    driver = _codex_driver_with(_ScriptedCodexSession)
    # Act
    _run_daemon_bounded(tmp_path, "ag-cx-rec", driver, residency="one-shot")
    record = read_exit_record(tmp_path / "ag-cx-rec")
    # Assert: the ExitRecord names the PLANNED end, not a violation.
    assert record["reason"] == EXIT_ONESHOT_COMPLETE


def test_resident_codex_daemon_records_harness_returned_when_session_refuses(
    tmp_path,
):
    # Arrange: session construction fails (no SDK / no auth) → the driver
    # records + drains + RETURNS, which under resident is the residency
    # violation the daemon must account, never a green-heartbeat zombie.
    driver = _codex_driver_with(_refusing_factory)
    # Act
    _run_daemon_bounded(tmp_path, "ag-cx-hr", driver, residency="resident")
    record = read_exit_record(tmp_path / "ag-cx-hr")
    # Assert
    assert record["reason"] == EXIT_HARNESS_RETURNED


def test_resident_codex_daemon_records_crashed_when_send_raises(tmp_path):
    # Arrange: an exception out of send() is outside the HarnessSession
    # contract (errors travel as events) — it must propagate to the
    # daemon and be recorded as a crash, not swallowed.
    driver = _codex_driver_with(_SendRaisesCodexSession)
    # Act
    _run_daemon_bounded(tmp_path, "ag-cx-cr", driver, residency="resident")
    record = read_exit_record(tmp_path / "ag-cx-cr")
    # Assert
    assert record["reason"] == EXIT_CRASHED


def test_resident_codex_daemon_parks_until_stopped(tmp_path):
    # Arrange: a resident daemon with NO mission and no a2a producer
    # spawns no conversation task, so it must park on stop.wait() and
    # only a signal-shaped stop ends it — the inverse of one-shot.
    async def _scenario() -> str:
        task = asyncio.create_task(
            session_daemon.run_session_daemon(
                "ag-cx-park",
                turn_driver=_stub_cli_driver,
                residency="resident",
                state_root=tmp_path,
                tick_seconds=0.01,
            )
        )
        await asyncio.sleep(0.2)
        parked = "parked" if not task.done() else "exited-early"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return parked

    # Act
    verdict = asyncio.run(asyncio.wait_for(_scenario(), timeout=_EXIT_DEADLINE_S))
    # Assert
    assert verdict == "parked"


# ---------------------------------------------------------------------------
# CLI: argparse → asyncio.run → daemon handoff
# ---------------------------------------------------------------------------


def test_cli_main_reaches_daemon_handoff_and_exits_zero(tmp_path):
    # Arrange: the full entrypoint — argparse through asyncio.run to
    # run_session_daemon — with only the vendor turn driver stubbed.
    argv = [
        "--name",
        "ag-cx-cli",
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


def test_cli_main_one_shot_writes_oneshot_complete_exit_record(tmp_path):
    # Arrange
    argv = [
        "--name",
        "ag-cx-cli-rec",
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
    record = read_exit_record(tmp_path / "ag-cx-cli-rec")
    # Assert: the CLI threads --residency into the daemon for real.
    assert record["reason"] == EXIT_ONESHOT_COMPLETE


def test_cli_main_accepts_resume_because_registry_declares_can_resume(tmp_path):
    # Arrange: can_resume=True in the codex-sdk descriptor — unlike the
    # openai CLI (which exits 2 here), this one must ACCEPT the flag and
    # run. This is the opposite branch of the same registry-derived gate.
    argv = [
        "--name",
        "ag-cx-resume",
        "--state-root",
        str(tmp_path),
        "--tick-seconds",
        "0.01",
        "--mission",
        "hi",
        "--residency",
        "one-shot",
        "--resume-session-id",
        "thr_0123456789",
    ]
    # Act
    rc = cli_main(argv, turn_driver=_stub_cli_driver)
    # Assert
    assert rc == 0


def test_driver_passes_the_resume_id_to_the_session_as_a_thread_id(tmp_path):
    # Arrange: --resume-session-id IS the codex thread id, so the driver
    # must hand it to the session constructor. A driver that dropped it
    # would start a FRESH thread while reporting a resume.
    seen: dict[str, Any] = {}

    def _recording_factory(agent_name: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return _ScriptedCodexSession(agent_name, **kwargs)

    async def _scenario() -> None:
        inbox: asyncio.Queue = asyncio.Queue()
        env = TurnEnvelope(
            text="hi", response=asyncio.get_running_loop().create_future(), exit_after=True
        )
        await inbox.put(env)
        await run_codex_conversation(
            "ag-cx-thr",
            tmp_path,
            pid=1,
            inbox=inbox,
            resume_session_id="thr_abc",
            stop=asyncio.Event(),
            session_factory=_recording_factory,
        )

    # Act
    asyncio.run(asyncio.wait_for(_scenario(), timeout=_EXIT_DEADLINE_S))
    # Assert
    assert seen.get("thread_id") == "thr_abc"


# ---------------------------------------------------------------------------
# The optional dependency: absent SDK must be an actionable error
# ---------------------------------------------------------------------------


def test_constructing_a_codex_session_works_without_the_sdk_installed(sdk_absent):
    # Arrange: a Claude-only deployment must be able to import this
    # module and build the object; only OPENING a session needs the SDK.
    session = CodexSession("ag-cx-noimp")
    # Act
    name = session.agent_name
    # Assert
    assert name == "ag-cx-noimp"


def test_opening_a_session_without_the_sdk_raises_codex_session_error(sdk_absent):
    # Arrange: the failure must be OUR typed error, not a bare
    # ImportError leaking from an unrelated frame.
    session = CodexSession("ag-cx-noimp2")
    # Act
    def start():
        asyncio.run(session.start())

    # Assert
    with pytest.raises(CodexSessionError):
        start()


def test_the_missing_sdk_error_carries_the_pip_install_hint(sdk_absent):
    # Arrange: an operator must be able to fix this from the message
    # alone — and the extra is `codex-sdk`, NOT the pre-existing `codex`.
    session = CodexSession("ag-cx-noimp3")
    # Act
    try:
        asyncio.run(session.start())
        message = ""
    except CodexSessionError as exc:
        message = str(exc)
    # Assert
    assert "scitex-agent-container[codex-sdk]" in message


def test_send_before_start_refuses_rather_than_returning_empty():
    # Arrange: a turn against an unopened session must be loud; a silent
    # empty reply would read as "the model said nothing".
    session = CodexSession("ag-cx-unstarted")

    async def _turn():
        async for _event in session.send(
            SimpleNamespace(role="user", content="hi")
        ):  # pragma: no cover — the first __anext__ raises
            pass

    # Act
    def drive():
        asyncio.run(_turn())

    # Assert
    with pytest.raises(CodexSessionError):
        drive()


# ---------------------------------------------------------------------------
# Thread-item normalization — pure, duck-typed, no SDK required
# ---------------------------------------------------------------------------


def test_agent_message_items_normalize_to_text_deltas():
    # Arrange
    item = SimpleNamespace(type="agent_message", text="hello")
    # Act
    event = normalize_thread_item(item)
    # Assert
    assert event.kind == "text_delta"


def test_reasoning_items_normalize_to_reasoning_events():
    # Arrange
    item = SimpleNamespace(type="reasoning", text="thinking")
    # Act
    event = normalize_thread_item(item)
    # Assert
    assert event.kind == "reasoning"


def test_command_execution_items_normalize_to_tool_calls():
    # Arrange — codex's built-in exec tooling is the whole reason this
    # harness earns a row, so its items must reach the transcript.
    item = SimpleNamespace(type="command_execution", command="ls -la")
    # Act
    event = normalize_thread_item(item)
    # Assert
    assert event.kind == "tool_call"


def test_file_change_items_carry_the_item_type_as_the_tool_name():
    # Arrange
    item = SimpleNamespace(type="file_change", text="pong.txt")
    # Act
    event = normalize_thread_item(item)
    # Assert
    assert event.tool_name == "file_change"


def test_error_items_normalize_to_turn_ending_error_events():
    # Arrange
    item = SimpleNamespace(type="error", message="endpoint said no")
    # Act
    event = normalize_thread_item(item)
    # Assert
    assert event.kind == "error"


def test_unknown_future_item_types_are_dropped_not_guessed():
    # Arrange — a future SDK release adding an item type must not crash
    # the turn nor invent a meaning for it.
    item = SimpleNamespace(type="something_new_in_2027")
    # Act
    event = normalize_thread_item(item)
    # Assert
    assert event is None


def test_usage_is_flattened_from_the_sdk_object():
    # Arrange
    usage = SimpleNamespace(input_tokens=11, output_tokens=7, nonsense="x")
    # Act
    flattened = usage_as_dict(usage)
    # Assert
    assert flattened == {"input_tokens": 11, "output_tokens": 7}


def test_missing_usage_degrades_to_an_empty_dict():
    # Arrange — a partial/absent usage object must not raise in the
    # middle of emitting the terminal result event.
    usage = None
    # Act
    flattened = usage_as_dict(usage)
    # Assert
    assert flattened == {}
