"""Tests for ``_runners/_harness_session.py`` (openai-compat-1 foundation).

Covers the four dataclasses (``ToolSpec``, ``Message``, ``NormalizedEvent``,
``RunResult``) and the ``HarnessSession`` Protocol. No mocks — the
Protocol is exercised against a REAL, hand-written implementation
(``_FakeSession``, mirroring the ``_ScriptedClient`` pattern in
``tests/.../test__session_turn.py``), proving the shape is actually
drivable end-to-end, not just structurally declared.

Landed foundation-only: nothing in production yet implements or calls
this Protocol (see the module docstring for why) — these tests pin the
SHAPE the openai-compat-2 runner will build against.

STX-TQ002 AAA + STX-TQ007 one-assert-per-test. Async tests follow the
``asyncio.run(_go())`` convention already used in
``tests/.../_runners/test__session_turn.py`` rather than pytest-asyncio
markers.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from scitex_agent_container._runners._harness_session import (
    Message,
    NormalizedEvent,
    HarnessSession,
    RunResult,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


def test_tool_spec_carries_supplied_name():
    # Arrange
    # Act
    spec = ToolSpec(name="search")
    # Assert
    assert spec.name == "search"


def test_tool_spec_description_defaults_to_empty_string():
    # Arrange
    # Act
    spec = ToolSpec(name="search")
    # Assert
    assert spec.description == ""


def test_tool_spec_parameters_defaults_to_empty_dict():
    # Arrange
    # Act
    spec = ToolSpec(name="search")
    # Assert
    assert spec.parameters == {}


def test_tool_spec_parameters_default_is_not_shared_between_instances():
    # Arrange
    a = ToolSpec(name="a")
    b = ToolSpec(name="b")
    # Act
    a.parameters["x"] = 1
    # Assert
    assert "x" not in b.parameters


def test_tool_spec_handler_defaults_to_none():
    # Arrange
    # Act
    spec = ToolSpec(name="search")
    # Assert
    assert spec.handler is None


def test_tool_spec_is_frozen():
    # Arrange
    spec = ToolSpec(name="search")
    raised: BaseException | None = None
    # Act
    try:
        spec.name = "renamed"  # type: ignore[misc]
    except Exception as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; dataclass frozen contract is to raise.)
        raised = exc
    # Assert
    assert raised is not None


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


def test_message_carries_supplied_role():
    # Arrange
    # Act
    msg = Message(role="user", content="hello")
    # Assert
    assert msg.role == "user"


def test_message_carries_supplied_content():
    # Arrange
    # Act
    msg = Message(role="user", content="hello")
    # Assert
    assert msg.content == "hello"


def test_message_name_defaults_to_none():
    # Arrange
    # Act
    msg = Message(role="user", content="hi")
    # Assert
    assert msg.name is None


def test_message_tool_call_id_defaults_to_none():
    # Arrange
    # Act
    msg = Message(role="user", content="hi")
    # Assert
    assert msg.tool_call_id is None


def test_message_tool_role_carries_tool_call_id():
    # Arrange
    # Act
    msg = Message(role="tool", content="42", tool_call_id="call_1")
    # Assert
    assert msg.tool_call_id == "call_1"


# ---------------------------------------------------------------------------
# RunResult
# ---------------------------------------------------------------------------


def test_run_result_text_defaults_to_empty_string():
    # Arrange
    # Act
    result = RunResult()
    # Assert
    assert result.text == ""


def test_run_result_session_id_defaults_to_none():
    # Arrange
    # Act
    result = RunResult()
    # Assert
    assert result.session_id is None


def test_run_result_usage_defaults_to_empty_dict():
    # Arrange
    # Act
    result = RunResult()
    # Assert
    assert result.usage == {}


def test_run_result_stop_reason_defaults_to_empty_string():
    # Arrange
    # Act
    result = RunResult()
    # Assert
    assert result.stop_reason == ""


def test_run_result_carries_supplied_session_id():
    # Arrange
    # Act
    result = RunResult(session_id="sess-1")
    # Assert
    assert result.session_id == "sess-1"


# ---------------------------------------------------------------------------
# NormalizedEvent
# ---------------------------------------------------------------------------


def test_normalized_event_carries_supplied_kind():
    # Arrange
    # Act
    event = NormalizedEvent(kind="text_delta")
    # Assert
    assert event.kind == "text_delta"


def test_normalized_event_text_defaults_to_empty_string():
    # Arrange
    # Act
    event = NormalizedEvent(kind="text_delta")
    # Assert
    assert event.text == ""


def test_normalized_event_tool_input_defaults_to_empty_dict():
    # Arrange
    # Act
    event = NormalizedEvent(kind="tool_call")
    # Assert
    assert event.tool_input == {}


def test_normalized_event_result_defaults_to_none():
    # Arrange
    # Act
    event = NormalizedEvent(kind="text_delta")
    # Assert
    assert event.result is None


def test_normalized_event_raw_defaults_to_none():
    # Arrange
    # Act
    event = NormalizedEvent(kind="text_delta")
    # Assert
    assert event.raw is None


def test_normalized_event_terminal_result_kind_carries_run_result():
    # Arrange
    result = RunResult(text="hi", session_id="s1")
    # Act
    event = NormalizedEvent(kind="result", result=result)
    # Assert
    assert event.result is result


# ---------------------------------------------------------------------------
# HarnessSession Protocol — exercised against a REAL implementation
# ---------------------------------------------------------------------------


class _FakeSession:
    """A real, minimal ``HarnessSession`` implementation for shape-testing.

    Mirrors the ``_ScriptedClient`` pattern in
    ``tests/.../_runners/test__session_turn.py`` — a hand-written stand-in,
    not a mock, so the test exercises the actual Protocol shape end-to-end.
    """

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.sent: list[Message] = []

    async def start(self) -> None:
        self.started = True

    async def send(self, message: Message) -> AsyncIterator[NormalizedEvent]:
        self.sent.append(message)
        yield NormalizedEvent(kind="text_delta", text="hel")
        yield NormalizedEvent(kind="text_delta", text="lo")
        yield NormalizedEvent(
            kind="result",
            result=RunResult(text="hello", session_id="sess-abc"),
        )

    async def close(self) -> None:
        self.closed = True


def test_fake_session_satisfies_harness_session_isinstance_check():
    # Arrange
    session = _FakeSession()
    # Act
    conforms = isinstance(session, HarnessSession)
    # Assert
    assert conforms is True


def test_plain_object_does_not_satisfy_harness_session_isinstance_check():
    # Arrange
    class _NotASession:
        pass

    # Act
    conforms = isinstance(_NotASession(), HarnessSession)
    # Assert
    assert conforms is False


def test_harness_session_start_sets_started_flag():
    # Arrange
    async def _go() -> bool:
        session = _FakeSession()
        await session.start()
        return session.started

    # Act
    started = asyncio.run(_go())
    # Assert
    assert started is True


def test_harness_session_close_sets_closed_flag():
    # Arrange
    async def _go() -> bool:
        session = _FakeSession()
        await session.close()
        return session.closed

    # Act
    closed = asyncio.run(_go())
    # Assert
    assert closed is True


def _drive_one_turn() -> list[NormalizedEvent]:
    async def _go() -> list[NormalizedEvent]:
        session = _FakeSession()
        await session.start()
        events = [e async for e in session.send(Message(role="user", content="hi"))]
        await session.close()
        return events

    return asyncio.run(_go())


def test_harness_session_send_yields_text_delta_events_in_order():
    # Arrange
    # Act
    events = _drive_one_turn()
    # Assert
    assert [e.text for e in events if e.kind == "text_delta"] == ["hel", "lo"]


def test_harness_session_send_terminates_with_result_kind():
    # Arrange
    # Act
    events = _drive_one_turn()
    # Assert
    assert events[-1].kind == "result"


def test_harness_session_terminal_event_carries_run_result_text():
    # Arrange
    # Act
    events = _drive_one_turn()
    # Assert
    assert events[-1].result.text == "hello"


def test_harness_session_terminal_event_carries_session_id():
    # Arrange
    # Act
    events = _drive_one_turn()
    # Assert
    assert events[-1].result.session_id == "sess-abc"
