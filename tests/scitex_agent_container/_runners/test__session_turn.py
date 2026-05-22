"""Per-turn driver C2 behaviour: observe background-subagent tasks.

``_drive_turn`` drains one turn's SDK response stream. With autonomy C2
it must capture interleaved task-lifecycle messages into ``session.jsonl``
AND onto the injected :class:`TaskObservations` holder, WITHOUT breaking
the turn — only the terminal ``ResultMessage`` ends it.

No mocks: a real ``types.ModuleType`` carries real fake message classes,
a real ``asyncio.Future`` backs the envelope, and a real ``session.jsonl``
under ``tmp_path`` is read back.
"""

from __future__ import annotations

import asyncio
import json
import types
from dataclasses import dataclass
from pathlib import Path

from scitex_agent_container._runners._session_tasks import (
    TaskObservations,
    resolve_task_types,
)
from scitex_agent_container._runners._session_turn import _drive_turn

# ---------------------------------------------------------------------------
# Real fake SDK message classes + a client that yields a scripted stream
# ---------------------------------------------------------------------------


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


class _Assistant:
    def __init__(self, content) -> None:
        self.content = content


class _User:
    pass


class _Result:
    def __init__(self, sid: str, usage) -> None:
        self.session_id = sid
        self.usage = usage


@dataclass
class _TaskNotification:
    task_id: str
    session_id: str
    status: str
    summary: str
    output_file: str


@dataclass
class _Envelope:
    text: str
    response: asyncio.Future
    from_agent: str | None = None
    dispatch_id: str | None = None
    session_id: str | None = None


def _sdk_types() -> dict:
    return {
        "AssistantMessage": _Assistant,
        "TextBlock": _Text,
        "UserMessage": _User,
        "ResultMessage": _Result,
    }


def _task_type_map() -> dict:
    """resolve_task_types over a module exposing only TaskNotificationMessage."""
    mod = types.ModuleType("fake_sdk_turn")
    mod.TaskNotificationMessage = _TaskNotification
    return resolve_task_types(mod)


class _ScriptedClient:
    """Yields a fixed message sequence from ``receive_response``."""

    def __init__(self, messages: list) -> None:
        self._messages = messages

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        for m in self._messages:
            yield m

    async def interrupt(self) -> None:
        return None


def _run_turn(state_dir: Path, messages: list, obs: TaskObservations) -> None:
    """Drive one turn against a scripted client; no host/db/push wiring."""

    async def _go() -> None:
        loop = asyncio.get_running_loop()
        env = _Envelope(text="go", response=loop.create_future())
        await _drive_turn(
            _ScriptedClient(messages),
            env,
            state_dir=state_dir,
            pid=1,
            stop=asyncio.Event(),
            print_stream=False,
            sdk_types=_sdk_types(),
            task_observations=obs,
            task_types=_task_type_map(),
        )

    asyncio.run(_go())


def _read_jsonl(state_dir: Path) -> list[dict]:
    text = (state_dir / "session.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# A turn carrying an interleaved TaskNotification
# ---------------------------------------------------------------------------


def _messages_with_task() -> list:
    """Assistant text, then a background-subagent completion, then result."""
    return [
        _Assistant([_Text("starting")]),
        _TaskNotification("bg-1", "s1", "completed", "subagent finished", "/out"),
        _Assistant([_Text("after task")]),
        _Result("s1", {}),
    ]


def test_drive_turn_captures_task_notification_into_jsonl(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    # Act
    _run_turn(state_dir, _messages_with_task(), obs)
    # Assert — the background-subagent completion reached the transcript.
    notifications = [
        r for r in _read_jsonl(state_dir) if r["type"] == "task_notification"
    ]
    assert notifications[0]["summary"] == "subagent finished"


def test_drive_turn_accumulates_completion_on_observations(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    # Act
    _run_turn(state_dir, _messages_with_task(), obs)
    # Assert — a later turn / the autonomous loop can read the completion.
    assert obs.completions[0]["task_id"] == "bg-1"


def test_drive_turn_does_not_break_on_task_message(tmp_path: Path) -> None:
    # Arrange — assistant text AFTER the task message must still be processed,
    # proving the task branch did not terminate the turn early.
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    # Act
    _run_turn(state_dir, _messages_with_task(), obs)
    # Assert
    assistant_texts = [
        r["text"] for r in _read_jsonl(state_dir) if r["type"] == "assistant"
    ]
    assert "after task" in assistant_texts


def test_drive_turn_resolves_future_with_full_assistant_text(tmp_path: Path) -> None:
    # Arrange — the awaiting /v1/turn future gets the concatenated reply,
    # spanning both assistant chunks across the interleaved task message.
    state_dir = tmp_path / "agent"
    obs = TaskObservations()
    captured: dict = {}

    async def _go() -> None:
        loop = asyncio.get_running_loop()
        env = _Envelope(text="go", response=loop.create_future())
        await _drive_turn(
            _ScriptedClient(_messages_with_task()),
            env,
            state_dir=state_dir,
            pid=1,
            stop=asyncio.Event(),
            print_stream=False,
            sdk_types=_sdk_types(),
            task_observations=obs,
            task_types=_task_type_map(),
        )
        captured["reply"] = env.response.result()

    # Act
    asyncio.run(_go())
    # Assert
    assert captured["reply"] == "startingafter task"


def test_drive_turn_without_task_types_ignores_task_shaped_message(
    tmp_path: Path,
) -> None:
    # Arrange — an SDK with no task classes resolved: a task-shaped object
    # falls through unrecognised (here it has no .content, so it is simply
    # not an assistant/user/result and is skipped) and nothing is captured.
    state_dir = tmp_path / "agent"
    obs = TaskObservations()

    async def _go() -> None:
        loop = asyncio.get_running_loop()
        env = _Envelope(text="go", response=loop.create_future())
        await _drive_turn(
            _ScriptedClient(
                [
                    _TaskNotification("bg-1", "s1", "completed", "x", "/out"),
                    _Result("s1", {}),
                ]
            ),
            env,
            state_dir=state_dir,
            pid=1,
            stop=asyncio.Event(),
            print_stream=False,
            sdk_types=_sdk_types(),
            task_observations=obs,
            task_types={},  # SDK too old → no task classes
        )

    # Act
    asyncio.run(_go())
    # Assert — graceful no-op: nothing accumulated when observation is off.
    assert obs.completions == []
