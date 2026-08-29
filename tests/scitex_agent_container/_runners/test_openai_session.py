"""Tests for ``_runners/openai_session.py`` (openai-compat-2).

Two tiers, matching the optional-dependency contract:

* **SDK-free** — event normalization (duck-typed on the SDK's public
  ``type``/``name`` string discriminators, exercised with hand-written
  stand-in event objects mirroring the documented shapes — the
  ``_FakeSession``/``_ScriptedClient`` pattern, not mocks), Protocol
  conformance, the lazy-import guarantee (module import + construction
  succeed with ``agents`` BLOCKED), and terminal-aggregate building.
* **Real SDK** — ``pytest.importorskip("agents")`` per test: ToolSpec →
  ``FunctionTool`` conversion (including invoking the wrapped handler),
  normalization of REAL SDK event objects, and the ``start``/``close``
  lifecycle against a ``PostgresAgentSession``.

``pg_schema`` is taken by EXACTLY the tests that reach the store — the
round-trip and the failing-turn one — and by no others. It is not autouse
on purpose: ``__init__``/``start()``/``close()`` open no connection, and a
test that takes the fixture anyway would stop being able to prove that.

No mocks (PA-306); no live-API turns (a ``send`` happy path needs a
real OpenAI key — the error-path contract is covered instead, which is
network-shape-independent: any failure must surface as a turn-ending
``kind="error"`` event, never an exception mid-iteration).

STX-TQ002 AAA + STX-TQ007 one-assert-per-test; async via
``asyncio.run(_go())`` (the ``test__harness_session.py`` convention).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners._harness_session import (
    Message,
    NormalizedEvent,
    HarnessSession,
    ToolSpec,
)
from scitex_agent_container._runners._openai_pg_session import PostgresAgentSession
from scitex_agent_container._runners.openai_session import (
    OpenAIAgentsSession,
    OpenAISessionError,
    _run_result_from_streamed,
    normalize_stream_event,
    tool_spec_to_function_tool,
    usage_as_dict,
)

_ENV_KEYS = ("SAC_OPENAI_API_KEY", "OPENAI_API_KEY", "SAC_OPENAI_MODEL")


@pytest.fixture
def openai_env(tmp_path: Path):
    """Scrub OpenAI env keys, install a fake sac key, restore on teardown."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["SAC_OPENAI_API_KEY"] = "sk-test-not-a-real-key"
    try:
        yield tmp_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def block_agents_import():
    """Make ``import agents`` raise — proves the lazy-import contract.

    ``sys.modules[name] = None`` is the documented interpreter behavior
    for forcing ``ModuleNotFoundError`` on import; restored on teardown.
    Not a mock — no call recording, no behavioral stand-in.
    """
    real = sys.modules.get("agents")
    sys.modules["agents"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        if real is None:
            sys.modules.pop("agents", None)
        else:
            sys.modules["agents"] = real


# ---------------------------------------------------------------------------
# Stand-in stream events — hand-written shapes mirroring the SDK's
# documented public discriminators (type/name Literal fields). Real SDK
# objects are exercised in the importorskip tier below.
# ---------------------------------------------------------------------------


@dataclass
class _TextDeltaData:
    delta: str = "hi"
    type: str = "response.output_text.delta"


@dataclass
class _RawEvent:
    data: Any = None
    type: str = "raw_response_event"


@dataclass
class _RawToolCall:
    name: str = "mytool"
    arguments: str = '{"x": 1}'


@dataclass
class _ToolCallItem:
    raw_item: Any = field(default_factory=_RawToolCall)
    type: str = "tool_call_item"


@dataclass
class _ToolOutputItem:
    output: Any = "42"
    type: str = "tool_call_output_item"


@dataclass
class _ReasoningSummary:
    text: str = "thinking hard"


@dataclass
class _ReasoningRaw:
    summary: list = field(default_factory=lambda: [_ReasoningSummary()])


@dataclass
class _ReasoningItem:
    raw_item: Any = field(default_factory=_ReasoningRaw)
    type: str = "reasoning_item"


@dataclass
class _ItemEvent:
    name: str = "tool_called"
    item: Any = None
    type: str = "run_item_stream_event"


@dataclass
class _NewAgent:
    name: str = "triage"


@dataclass
class _AgentUpdatedEvent:
    new_agent: Any = field(default_factory=_NewAgent)
    type: str = "agent_updated_stream_event"


# ---------------------------------------------------------------------------
# normalize_stream_event — SDK-free tier
# ---------------------------------------------------------------------------


def test_normalize_text_delta_yields_text_delta_kind():
    # Arrange
    event = _RawEvent(data=_TextDeltaData(delta="hel"))
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.kind == "text_delta"


def test_normalize_text_delta_carries_delta_text():
    # Arrange
    event = _RawEvent(data=_TextDeltaData(delta="hel"))
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.text == "hel"


def test_normalize_non_text_raw_event_is_dropped():
    # Arrange
    event = _RawEvent(data=_TextDeltaData(type="response.created"))
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized is None


def test_normalize_tool_called_yields_tool_call_kind():
    # Arrange
    event = _ItemEvent(name="tool_called", item=_ToolCallItem())
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.kind == "tool_call"


def test_normalize_tool_called_carries_tool_name():
    # Arrange
    event = _ItemEvent(name="tool_called", item=_ToolCallItem())
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.tool_name == "mytool"


def test_normalize_tool_called_decodes_json_arguments():
    # Arrange
    event = _ItemEvent(name="tool_called", item=_ToolCallItem())
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.tool_input == {"x": 1}


def test_normalize_tool_called_malformed_arguments_degrade_to_empty_dict():
    # Arrange
    item = _ToolCallItem(raw_item=_RawToolCall(arguments='{"x": '))
    event = _ItemEvent(name="tool_called", item=item)
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.tool_input == {}


def test_normalize_tool_output_yields_tool_result_kind():
    # Arrange
    event = _ItemEvent(name="tool_output", item=_ToolOutputItem())
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.kind == "tool_result"


def test_normalize_tool_output_carries_output_value():
    # Arrange
    event = _ItemEvent(name="tool_output", item=_ToolOutputItem(output="42"))
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.tool_output == "42"


def test_normalize_reasoning_item_yields_reasoning_kind():
    # Arrange
    event = _ItemEvent(name="reasoning_item_created", item=_ReasoningItem())
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.kind == "reasoning"


def test_normalize_reasoning_item_carries_summary_text():
    # Arrange
    event = _ItemEvent(name="reasoning_item_created", item=_ReasoningItem())
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.text == "thinking hard"


def test_normalize_message_output_created_is_dropped_as_delta_duplicate():
    # Arrange
    event = _ItemEvent(name="message_output_created", item=_ToolOutputItem())
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized is None


def test_normalize_handoff_requested_yields_task_kind():
    # Arrange
    event = _ItemEvent(name="handoff_requested", item=None)
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.kind == "task"


def test_normalize_agent_updated_yields_task_kind():
    # Arrange
    event = _AgentUpdatedEvent()
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.kind == "task"


def test_normalize_agent_updated_names_the_new_agent():
    # Arrange
    event = _AgentUpdatedEvent(new_agent=_NewAgent(name="triage"))
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.text == "agent_updated:triage"


def test_normalize_unknown_event_type_is_dropped():
    # Arrange
    event = _RawEvent(type="some_future_event")
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized is None


# ---------------------------------------------------------------------------
# usage_as_dict / _run_result_from_streamed — SDK-free tier
# ---------------------------------------------------------------------------


@dataclass
class _UsageStandIn:
    requests: int = 1
    input_tokens: int = 10
    output_tokens: int = 5
    total_tokens: int = 15


@dataclass
class _ContextWrapperStandIn:
    usage: Any = field(default_factory=_UsageStandIn)


@dataclass
class _StreamedStandIn:
    """Post-drain ``RunResultStreaming`` shape (duck-typed fields only)."""

    final_output: Any = "hello"
    context_wrapper: Any = field(default_factory=_ContextWrapperStandIn)
    last_response_id: str = "resp_123"
    is_complete: bool = True


def test_usage_as_dict_none_returns_empty_dict():
    # Arrange
    usage = None
    # Act
    out = usage_as_dict(usage)
    # Assert
    assert out == {}


def test_usage_as_dict_flattens_token_counts():
    # Arrange
    usage = _UsageStandIn()
    # Act
    out = usage_as_dict(usage)
    # Assert
    assert out == {
        "requests": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


def test_run_result_text_prefers_final_output():
    # Arrange
    streamed = _StreamedStandIn(final_output="hello")
    # Act
    result = _run_result_from_streamed(streamed, "sess-1", "joined")
    # Assert
    assert result.text == "hello"


def test_run_result_text_falls_back_to_joined_deltas():
    # Arrange
    streamed = _StreamedStandIn(final_output=None)
    # Act
    result = _run_result_from_streamed(streamed, "sess-1", "joined")
    # Assert
    assert result.text == "joined"


def test_run_result_carries_session_id():
    # Arrange
    streamed = _StreamedStandIn()
    # Act
    result = _run_result_from_streamed(streamed, "sess-1", "")
    # Assert
    assert result.session_id == "sess-1"


def test_run_result_usage_includes_last_response_id():
    # Arrange
    streamed = _StreamedStandIn(last_response_id="resp_123")
    # Act
    result = _run_result_from_streamed(streamed, "sess-1", "")
    # Assert
    assert result.usage["last_response_id"] == "resp_123"


def test_run_result_stop_reason_complete_when_stream_finished():
    # Arrange
    streamed = _StreamedStandIn(is_complete=True)
    # Act
    result = _run_result_from_streamed(streamed, "sess-1", "")
    # Assert
    assert result.stop_reason == "complete"


# ---------------------------------------------------------------------------
# OpenAIAgentsSession — Protocol conformance + lazy-import contract (SDK-free)
# ---------------------------------------------------------------------------


def test_openai_session_satisfies_harness_session_protocol():
    # Arrange
    session = OpenAIAgentsSession("alpha")
    # Act
    conforms = isinstance(session, HarnessSession)
    # Assert
    assert conforms is True


def test_openai_session_tracing_defaults_off():
    # Arrange: sac must not POST trace payloads to OpenAI by default.
    # Act
    session = OpenAIAgentsSession("alpha")
    # Assert
    assert session.tracing is False


def test_module_imports_with_agents_blocked(block_agents_import):
    # Arrange
    name = "scitex_agent_container._runners.openai_session"
    saved = sys.modules.pop(name)
    # Act
    try:
        reimported = importlib.import_module(name)
    finally:
        sys.modules[name] = saved
    # Assert
    assert reimported is not None


def test_start_with_agents_blocked_raises_with_install_hint(
    block_agents_import, openai_env
):
    # Arrange
    session = OpenAIAgentsSession("alpha")

    async def _go() -> str:
        try:
            await session.start()
        except OpenAISessionError as exc:
            return str(exc)
        return ""

    # Act
    message = asyncio.run(_go())
    # Assert
    assert "openai-agents" in message


def test_tool_conversion_with_agents_blocked_raises_openai_session_error(
    block_agents_import,
):
    # Arrange
    spec = ToolSpec(name="t", handler=lambda: None)
    raised: Exception | None = None
    # Act
    try:
        tool_spec_to_function_tool(spec)
    except OpenAISessionError as exc:
        raised = exc
    # Assert
    assert raised is not None


def test_send_before_start_raises():
    # Arrange
    session = OpenAIAgentsSession("alpha")

    async def _go() -> Exception | None:
        try:
            async for _ in session.send(Message(role="user", content="hi")):
                pass
        except OpenAISessionError as exc:
            return exc
        return None

    # Act
    raised = asyncio.run(_go())
    # Assert
    assert raised is not None


# ---------------------------------------------------------------------------
# Real-SDK tier — pytest.importorskip("agents") per test
# ---------------------------------------------------------------------------


def test_tool_spec_converts_to_function_tool_with_name():
    # Arrange
    agents = pytest.importorskip("agents")
    spec = ToolSpec(name="adder", description="adds", handler=lambda a, b: a + b)
    # Act
    tool = tool_spec_to_function_tool(spec)
    # Assert
    assert isinstance(tool, agents.FunctionTool)


def test_tool_spec_parameters_map_to_params_json_schema():
    # Arrange
    pytest.importorskip("agents")
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    spec = ToolSpec(name="adder", parameters=schema, handler=lambda a: a)
    # Act
    tool = tool_spec_to_function_tool(spec)
    # Assert
    assert tool.params_json_schema == schema


def test_tool_spec_empty_parameters_default_to_object_schema():
    # Arrange
    pytest.importorskip("agents")
    spec = ToolSpec(name="ping", handler=lambda: "pong")
    # Act
    tool = tool_spec_to_function_tool(spec)
    # Assert
    assert tool.params_json_schema == {"type": "object", "properties": {}}


def test_tool_spec_conversion_disables_strict_schema():
    # Arrange
    pytest.importorskip("agents")
    spec = ToolSpec(name="ping", handler=lambda: "pong")
    # Act
    tool = tool_spec_to_function_tool(spec)
    # Assert
    assert tool.strict_json_schema is False


def test_tool_spec_without_handler_raises():
    # Arrange
    pytest.importorskip("agents")
    spec = ToolSpec(name="external")
    raised: Exception | None = None
    # Act
    try:
        tool_spec_to_function_tool(spec)
    except OpenAISessionError as exc:
        raised = exc
    # Assert
    assert raised is not None


def test_converted_tool_invokes_sync_handler_with_decoded_kwargs():
    # Arrange
    pytest.importorskip("agents")
    spec = ToolSpec(name="adder", handler=lambda a, b: a + b)
    tool = tool_spec_to_function_tool(spec)
    # Act
    result = asyncio.run(tool.on_invoke_tool(None, json.dumps({"a": 2, "b": 3})))
    # Assert
    assert result == 5


def test_converted_tool_awaits_async_handler():
    # Arrange
    pytest.importorskip("agents")

    async def _double(x: int) -> int:
        return x * 2

    tool = tool_spec_to_function_tool(ToolSpec(name="double", handler=_double))
    # Act
    result = asyncio.run(tool.on_invoke_tool(None, json.dumps({"x": 21})))
    # Assert
    assert result == 42


def test_converted_tool_empty_arguments_call_handler_without_kwargs():
    # Arrange
    pytest.importorskip("agents")
    tool = tool_spec_to_function_tool(ToolSpec(name="ping", handler=lambda: "pong"))
    # Act
    result = asyncio.run(tool.on_invoke_tool(None, ""))
    # Assert
    assert result == "pong"


def test_normalize_real_sdk_text_delta_event():
    # Arrange
    pytest.importorskip("agents")
    from agents.stream_events import RawResponsesStreamEvent
    from openai.types.responses import ResponseTextDeltaEvent

    event = RawResponsesStreamEvent(
        data=ResponseTextDeltaEvent(
            content_index=0,
            delta="hi",
            item_id="i",
            logprobs=[],
            output_index=0,
            sequence_number=0,
            type="response.output_text.delta",
        )
    )
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.text == "hi"


def test_normalize_real_sdk_tool_called_event():
    # Arrange
    pytest.importorskip("agents")
    from agents import Agent
    from agents.items import ToolCallItem
    from agents.stream_events import RunItemStreamEvent
    from openai.types.responses import ResponseFunctionToolCall

    item = ToolCallItem(
        agent=Agent(name="t"),
        raw_item=ResponseFunctionToolCall(
            arguments='{"x": 1}', call_id="c1", name="mytool", type="function_call"
        ),
    )
    event = RunItemStreamEvent(name="tool_called", item=item)
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.tool_input == {"x": 1}


def test_normalize_real_sdk_agent_updated_event():
    # Arrange
    pytest.importorskip("agents")
    from agents import Agent
    from agents.stream_events import AgentUpdatedStreamEvent

    event = AgentUpdatedStreamEvent(new_agent=Agent(name="triage"))
    # Act
    normalized = normalize_stream_event(event)
    # Assert
    assert normalized.text == "agent_updated:triage"


def test_start_builds_postgres_session_state(openai_env: Path):
    """NO ``pg_schema``: constructing the session must not touch the store.

    That is a property worth asserting rather than a convenience. The store
    handle is opened on the first READ or WRITE, so ``start()`` stays as
    network-free as it was when it opened a local file — and the autouse
    unreachable-DSN guard is what proves it: if construction connected, this
    test would fail on port 1 instead of passing.
    """
    # Arrange
    pytest.importorskip("agents")
    session = OpenAIAgentsSession("alpha", model="gpt-4o-mini")

    async def _go() -> Any:
        await session.start()
        state = session._session
        await session.close()
        return state

    # Act
    state = asyncio.run(_go())
    # Assert
    assert isinstance(state, PostgresAgentSession)


def test_session_state_round_trips_through_postgres(
    openai_env: Path, pg_schema: str
):
    """A turn WRITTEN by one session object is READ by a different one.

    The replacement for ``test_start_creates_the_state_db_file``, which
    asserted that a file appeared on disk. There is no file; the property
    that mattered was never the file but that conversation state SURVIVES
    the session object, so this reads it back through a second
    ``OpenAIAgentsSession`` — the answer can only come from the store.
    """
    # Arrange
    pytest.importorskip("agents")
    written = [{"role": "user", "content": "remember this"}]

    async def _go() -> list[Any]:
        writer = OpenAIAgentsSession("alpha", model="gpt-4o-mini")
        await writer.start()
        try:
            await writer._session.add_items(list(written))
        finally:
            await writer.close()
        reader = OpenAIAgentsSession("alpha", model="gpt-4o-mini")
        await reader.start()
        try:
            return await reader._session.get_items()
        finally:
            await reader.close()

    # Act
    items = asyncio.run(_go())
    # Assert
    assert items == written


def test_close_resets_started_flag(openai_env: Path):
    # Arrange
    pytest.importorskip("agents")
    session = OpenAIAgentsSession("alpha", model="gpt-4o-mini")

    async def _go() -> bool:
        await session.start()
        await session.close()
        return session._started

    # Act
    started = asyncio.run(_go())
    # Assert
    assert started is False


def test_send_failure_surfaces_as_turn_ending_error_event(
    openai_env: Path, pg_schema: str
):
    """Any turn failure (fake key → 401 online, DNS error offline) must
    yield a terminal ``kind="error"`` event, never leak an exception.

    ``pg_schema`` because this one DOES touch the store: ``Runner`` reads
    the conversation history before it calls the model, so the turn reaches
    PostgreSQL before it reaches the failure being asserted. Without it the
    autouse guard's unreachable DSN raises there instead, and the test would
    still go green — on the wrong error.
    """
    # Arrange
    pytest.importorskip("agents")
    session = OpenAIAgentsSession(
        "alpha",
        model="gpt-4o-mini",
        record_spend=False,
    )

    async def _go() -> list[NormalizedEvent]:
        await session.start()
        try:
            events = [
                e
                async for e in session.send(Message(role="user", content="hi"))
            ]
        finally:
            await session.close()
        return events

    # Act
    events = asyncio.run(asyncio.wait_for(_go(), timeout=120))
    # Assert
    assert events[-1].kind == "error"
