"""Harness-agnostic session Protocol + normalized event/message shapes.

Foundation for the ``openai`` agent SDK family (scitex-todo card
``openai-compat-1``, "Land ProviderConfig + ProviderSession Protocol" —
that card title is quoted verbatim; the Protocol it landed is the one
renamed to :class:`HarnessSession` here).
This module defines the SHAPES a future ``openai-agents``-backed session
(openai-compat-2) will produce and consume, so that both the existing
``claude-agent-sdk`` path and the future OpenAI path can eventually be
driven through one uniform turn-loop.

Landed foundation-only: nothing in the live runner (``_runners/
claude_session.py`` → ``_session_conversation.py`` → ``_session_turn.py``)
imports or calls anything in this module yet. It is pure, additive,
unused-by-default code — behaviourally a no-op for existing Claude
agents. openai-compat-2 will add a concrete ``OpenAIAgentsSession`` class that
satisfies :class:`HarnessSession`, wrapping ``openai-agents``'
``Runner.run_streamed()``. A concrete Claude-side implementation MAY be
added later as a retrofit of ``_drive_turn`` (see "Composition with
RuntimeBase" below) — that retrofit is explicitly NOT part of this phase,
to keep the no-op guarantee airtight (zero live call sites touched).

Shape rationale (informed by both SDKs' REAL vocabularies, not guessed)
------------------------------------------------------------------------
``claude-agent-sdk`` (installed in this repo — see ``runtimes/
_sdk_common.py`` and ``_runners/_session_turn.py::_drive_turn``) streams
``AssistantMessage`` (content blocks: ``TextBlock`` / ``ThinkingBlock`` /
``ToolUseBlock`` / ``ToolResultBlock``), ``UserMessage`` (echo),
``ResultMessage`` (terminal — carries ``session_id`` + ``usage``), and
background-subagent lifecycle messages (``TaskStartedMessage`` /
``TaskProgressMessage`` / ``TaskNotificationMessage``). The ``openai-
agents`` SDK's ``Runner.run_streamed()`` streams an analogous mix of raw
deltas and semantic "run items" (message / tool-call / tool-call-output /
reasoning / handoff), then exposes a terminal aggregate (``final_output``
et al.) once the stream is exhausted. :class:`NormalizedEvent` models the
shared shape both vocabularies reduce to; :class:`RunResult` is the
terminal aggregate, carried on the LAST event of ``kind="result"`` (both
SDKs already stream their terminal info as part of the same sequence —
this mirrors that, needing no second method on the Protocol).

Composition with RuntimeBase
-----------------------------
:class:`HarnessSession` is deliberately NOT a subtype of, or a
replacement for, :class:`runtimes.base.RuntimeBase`. They operate at
different layers of the stack:

* ``RuntimeBase`` (``ClaudeSessionRuntime`` / ``TuiSessionRuntime``) is
  PROCESS lifecycle — ``start`` / ``stop`` / ``is_running`` / ``logs`` for
  one whole agent's container, driven from the HOST/CLI side
  (``sac agents start <name>``).
* ``HarnessSession`` is CONVERSATION lifecycle — one agent SDK
  connection's turn-taking, driven INSIDE the running container by the
  runner (today: ``_session_conversation.py`` opening one
  ``ClaudeSDKClient`` and calling ``_drive_turn`` per inbound message).

The name is HARNESS, not PROVIDER, because the axis this Protocol
selects on is WHICH AGENT PROGRAM RUNS THE LOOP — its implementations
are ``ClaudeCodeSession`` / ``CodexSession`` / :class:`OpenAIAgentsSession`
/ ``PiSession`` / ``OpenHandsSession``, which are agent programs, not
inference providers. "Which model thinks" is a separate axis
(``inference``); conflating the two is what the old ``ProviderSession``
name did.

They compose VERTICALLY, not by inheritance: a future runner selects a
``HarnessSession`` implementation based on ``AgentConfig.provider``
(``config._provider_types.AgentProvider`` — see the naming-collision
note there against the unrelated ``ClaudeSpec.provider``) and drives it
from inside the process that ``RuntimeBase.start()`` launched. Reusing
``HarnessSession`` for both harnesses (rather than only using it as an
OpenAI-side implementation detail) is what keeps ``openai-compat-2``
from having to reinvent the turn-loop contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

__all__ = [
    "ToolSpec",
    "Message",
    "NormalizedEvent",
    "RunResult",
    "HarnessSession",
]

# Chat-message role vocabulary — the intersection both SDKs accept
# (Claude: UserMessage/AssistantMessage content-block roles; OpenAI:
# chat-style role field on TResponseInputItem). "tool" carries a
# tool-result being fed back to the model (Message.tool_call_id set).
MessageRole = Literal["system", "user", "assistant", "tool"]

# NormalizedEvent discriminator. See the module docstring's "Shape
# rationale" for how each kind maps onto both SDKs' native vocabularies.
EventKind = Literal[
    "text_delta",  # streaming assistant text chunk
    "reasoning",  # thinking/reasoning content (ThinkingBlock / reasoning_item)
    "tool_call",  # the model requested a tool invocation
    "tool_result",  # a tool's output being fed back into the conversation
    "task",  # background subagent / task lifecycle notification
    "error",  # a fatal or recoverable error surfaced mid-stream
    "result",  # terminal event; NormalizedEvent.result is populated
]


@dataclass(frozen=True)
class ToolSpec:
    """A tool definition, harness-agnostic.

    Maps onto ``claude-agent-sdk``'s MCP tool registration
    (``create_sdk_mcp_server`` / ``SdkMcpTool``: name, description, a JSON
    Schema for inputs, and a handler) and onto ``openai-agents``'
    ``FunctionTool`` (name, description, ``params_json_schema``,
    ``on_invoke_tool``) closely enough that openai-compat-2's
    ``ToolSpec -> FunctionTool`` conversion (per its scitex-todo card) is
    a near-direct field mapping.
    """

    name: str
    description: str = ""
    # JSON Schema (``{"type": "object", "properties": {...}, ...}``) for
    # the tool's input. Kept as a plain dict (not a nested dataclass) so
    # any tool author can hand it the schema shape their MCP server /
    # framework already produces without an extra conversion step.
    parameters: dict[str, Any] = field(default_factory=dict)
    # Optional local callable — undefined (``None``) for tools that are
    # ALWAYS invoked through an external MCP server rather than in-process.
    handler: Any = None


@dataclass(frozen=True)
class Message:
    """One chat-history entry, harness-agnostic.

    ``tool_call_id`` is set only on ``role="tool"`` messages (the result
    being fed back for a specific prior tool call); ``name`` is set only
    when the message originates from a named tool/subagent.
    """

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class RunResult:
    """Terminal aggregate for one completed turn.

    Mirrors what both SDKs already surface once a turn's stream is
    exhausted: Claude's ``ResultMessage`` (``session_id`` + ``usage``)
    and ``openai-agents``' ``RunResultStreaming`` (``final_output`` +
    usage on ``raw_responses``). Carried on the terminal
    ``NormalizedEvent`` (``kind="result"``) rather than returned via a
    separate Protocol method — see the module docstring.
    """

    text: str = ""
    session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""


@dataclass(frozen=True)
class NormalizedEvent:
    """One harness-agnostic event in a session's turn stream.

    ``kind`` discriminates which fields are meaningful (see
    :data:`EventKind`); unused fields keep their default. ``raw`` is an
    escape hatch — the harness-native object, kept for debugging /
    forward-compat — and is NEVER required for correct handling of a
    known ``kind``.
    """

    kind: EventKind
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: Any = None
    error: str = ""
    result: RunResult | None = None
    raw: Any = None


@runtime_checkable
class HarnessSession(Protocol):
    """Harness-agnostic conversational session.

    Wraps a single agent SDK's client/session object with a uniform
    surface: one :meth:`start`, N :meth:`send` turns (each an async
    stream of :class:`NormalizedEvent`, terminating in a ``kind="result"``
    event carrying a :class:`RunResult`), then :meth:`close`. Mirrors the
    ``ClaudeSDKClient`` open → ``query``/``receive_response`` (repeated
    per turn) → close lifecycle already in production use (see
    ``_runners/_session_conversation.py`` / ``_session_turn.py``), so a
    future runner can drive EITHER harness through the same loop shape.

    Concrete implementations are NOT required in this phase (foundation
    only — see the module docstring). openai-compat-2 lands the first
    concrete implementation, wrapping ``openai-agents``'
    ``Runner.run_streamed()``.
    """

    async def start(self) -> None:
        """Open the underlying harness connection (auth + client init)."""
        ...

    def send(self, message: Message) -> AsyncIterator[NormalizedEvent]:
        """Send one turn; the caller iterates the yielded events.

        Implementations are async generators. The last event of a
        completed turn has ``kind="result"`` with :attr:`NormalizedEvent.result`
        populated; a turn that errors yields a ``kind="error"`` event
        instead (implementations MAY still raise for unrecoverable
        failures — callers should treat both as turn-ending).
        """
        ...

    async def close(self) -> None:
        """Tear down the underlying harness connection."""
        ...
