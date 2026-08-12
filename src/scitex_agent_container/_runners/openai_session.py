"""Concrete :class:`HarnessSession` backed by the ``openai-agents`` SDK.

scitex-todo card ``openai-compat-2`` — the first concrete implementation
of the openai-compat-1 Protocol (see :mod:`_runners._harness_session`
for the shape rationale). Wires:

* :class:`~._harness_session.ToolSpec` → ``agents.FunctionTool``
  (:func:`tool_spec_to_function_tool` — a near-direct field mapping, as
  the ToolSpec docstring predicted).
* ``Runner.run_streamed()`` → an async generator of
  :class:`~._harness_session.NormalizedEvent`
  (:func:`normalize_stream_event` + :meth:`OpenAIAgentsSession.send`).
* ``SQLiteSession`` for conversation state (multi-turn memory across
  :meth:`OpenAIAgentsSession.send` calls; placement via
  :func:`runtimes._openai_sdk_common.resolve_state_db_path`).
* Per-turn spend recording into the ledger of
  :mod:`_account.openai_usage` (spend-based tracking — best-effort,
  never fails the turn).

The ``openai-agents`` dependency is OPTIONAL (``pip install
scitex-agent-container[openai]``). This module imports it LAZILY inside
:meth:`OpenAIAgentsSession.start` / :func:`tool_spec_to_function_tool` —
importing the module (and constructing :class:`OpenAIAgentsSession`) works on
Claude-only deployments; only actually OPENING a session requires the
SDK. :func:`normalize_stream_event` is deliberately duck-typed on the
SDK's own ``type`` / ``name`` string discriminators (stable public
Literal fields) so event normalization is pure and testable without a
network connection.

Vocabulary mapping (openai-agents → NormalizedEvent.kind)
----------------------------------------------------------
``raw_response_event`` with ``data.type ==
"response.output_text.delta"``  → ``text_delta``; other raw deltas are
dropped (byte-level protocol noise). ``run_item_stream_event`` by
``name``: ``tool_called`` → ``tool_call``; ``tool_output`` →
``tool_result``; ``reasoning_item_created`` → ``reasoning``;
``message_output_created`` → dropped (its text already streamed as
deltas — emitting both would double the text for consumers that join
``text_delta`` events); handoff/MCP lifecycle names → ``task``.
``agent_updated_stream_event`` → ``task``. The terminal
``kind="result"`` event is synthesized after the stream drains, carrying
``final_output`` + usage from the SDK's terminal aggregate.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from ._harness_session import Message, NormalizedEvent, RunResult, ToolSpec

__all__ = [
    "OpenAISessionError",
    "OpenAIAgentsSession",
    "tool_spec_to_function_tool",
    "normalize_stream_event",
    "usage_as_dict",
]

_INSTALL_HINT = (
    "openai_session requires `openai-agents` "
    "(`pip install scitex-agent-container[openai]`)."
)

# RunItemStreamEvent names that are lifecycle notifications rather than
# content — surfaced as kind="task" so callers can display progress
# without special-casing every SDK release's vocabulary.
_TASK_ITEM_NAMES = frozenset(
    {
        "handoff_requested",
        "handoff_occured",  # upstream's (frozen) misspelling
        "mcp_approval_requested",
        "mcp_approval_response",
        "mcp_list_tools",
        "tool_search_called",
        "tool_search_output_created",
    }
)


class OpenAISessionError(RuntimeError):
    """Raised when the OpenAI session cannot satisfy a precondition."""


def _import_agents() -> Any:
    """Import and return the ``agents`` module, or raise with the pip hint."""
    try:
        import agents
    except Exception as exc:  # stx-allow: fallback (reason: optional dep at runtime; broaden beyond ImportError so a misbuilt transitive dep surfaces as an actionable OpenAISessionError)
        raise OpenAISessionError(_INSTALL_HINT) from exc
    return agents


# ---------------------------------------------------------------------------
# ToolSpec → FunctionTool
# ---------------------------------------------------------------------------


def tool_spec_to_function_tool(spec: ToolSpec) -> Any:
    """Convert a harness-agnostic :class:`ToolSpec` into ``agents.FunctionTool``.

    Field mapping: ``name`` → ``name``, ``description`` → ``description``,
    ``parameters`` → ``params_json_schema`` (defaulted to an empty object
    schema when the spec carries none — ``FunctionTool`` requires a
    schema), ``handler`` → ``on_invoke_tool`` (wrapped: the SDK calls
    ``(ctx, json_args_str)``; the handler receives the decoded kwargs and
    may be sync or async).

    ``strict_json_schema=False``: ToolSpec accepts whatever schema shape
    the tool author's framework already produces; OpenAI strict mode
    demands ``additionalProperties: false`` + exhaustive ``required``,
    which arbitrary MCP-style schemas don't guarantee.

    Raises :class:`OpenAISessionError` if the spec has no ``handler``
    (a ``FunctionTool`` is in-process by definition — external MCP tools
    attach through the SDK's MCP integration, not this conversion) or if
    ``openai-agents`` is not installed.
    """
    agents = _import_agents()
    if spec.handler is None:
        raise OpenAISessionError(
            f"ToolSpec {spec.name!r} has no handler — FunctionTool needs an "
            "in-process callable (external/MCP tools don't convert here)."
        )
    handler = spec.handler

    async def _on_invoke_tool(_ctx: Any, args_json: str) -> Any:
        kwargs = json.loads(args_json) if args_json else {}
        if not isinstance(kwargs, dict):
            kwargs = {"input": kwargs}
        result = handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    schema = spec.parameters or {"type": "object", "properties": {}}
    return agents.FunctionTool(
        name=spec.name,
        description=spec.description,
        params_json_schema=dict(schema),
        on_invoke_tool=_on_invoke_tool,
        strict_json_schema=False,
    )


# ---------------------------------------------------------------------------
# Stream-event normalization (pure; duck-typed on public discriminators)
# ---------------------------------------------------------------------------


def _parse_tool_arguments(raw_item: Any) -> dict[str, Any]:
    """Best-effort decode of a tool call's JSON ``arguments`` string."""
    raw = getattr(raw_item, "arguments", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        # stx-allow: fallback (reason: model-produced arguments may be truncated mid-stream; an empty dict keeps the event usable and `raw` retains the original)
        try:
            decoded = json.loads(raw)
        except ValueError:  # stx-allow: fallback (reason: type coercion or format mismatch)
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _reasoning_text(item: Any) -> str:
    """Extract display text from a ``ReasoningItem`` (best-effort)."""
    raw = getattr(item, "raw_item", None)
    summary = getattr(raw, "summary", None) or []
    parts = [getattr(s, "text", "") for s in summary]
    return "\n".join(p for p in parts if p)


def normalize_stream_event(event: Any) -> NormalizedEvent | None:
    """Map one ``openai-agents`` stream event to a :class:`NormalizedEvent`.

    Returns ``None`` for events with no harness-agnostic meaning (raw
    protocol deltas other than text, assembled-message duplicates,
    unknown future kinds) — callers drop those. Pure and duck-typed on
    the SDK's own ``type`` / ``name`` Literal discriminators, so it
    needs neither a network connection nor (for hand-built fixture
    events) the SDK itself. See the module docstring for the full
    vocabulary mapping.
    """
    etype = getattr(event, "type", "")

    if etype == "raw_response_event":
        data = getattr(event, "data", None)
        if getattr(data, "type", "") == "response.output_text.delta":
            return NormalizedEvent(
                kind="text_delta", text=getattr(data, "delta", "") or "", raw=event
            )
        return None

    if etype == "run_item_stream_event":
        name = getattr(event, "name", "")
        item = getattr(event, "item", None)
        if name == "tool_called":
            raw_item = getattr(item, "raw_item", None)
            return NormalizedEvent(
                kind="tool_call",
                tool_name=getattr(raw_item, "name", "") or "",
                tool_input=_parse_tool_arguments(raw_item),
                raw=event,
            )
        if name == "tool_output":
            return NormalizedEvent(
                kind="tool_result",
                tool_output=getattr(item, "output", None),
                raw=event,
            )
        if name == "reasoning_item_created":
            return NormalizedEvent(
                kind="reasoning", text=_reasoning_text(item), raw=event
            )
        if name in _TASK_ITEM_NAMES:
            return NormalizedEvent(kind="task", text=name, raw=event)
        # message_output_created (text already streamed as deltas) and
        # unknown future names fall through.
        return None

    if etype == "agent_updated_stream_event":
        new_agent = getattr(event, "new_agent", None)
        return NormalizedEvent(
            kind="task",
            text=f"agent_updated:{getattr(new_agent, 'name', '')}",
            raw=event,
        )

    return None


# ---------------------------------------------------------------------------
# Terminal aggregate
# ---------------------------------------------------------------------------


def usage_as_dict(usage: Any) -> dict[str, Any]:
    """Flatten the SDK's ``Usage`` dataclass into the RunResult usage dict.

    Duck-typed (``None``-tolerant) so a missing/partial usage object
    degrades to an empty dict rather than raising mid-terminal-event.
    """
    if usage is None:
        return {}
    out: dict[str, Any] = {}
    for key in ("requests", "input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out


def _run_result_from_streamed(
    streamed: Any, session_id: str | None, joined_text: str
) -> RunResult:
    """Build the terminal :class:`RunResult` from a drained ``RunResultStreaming``."""
    final_output = getattr(streamed, "final_output", None)
    text = str(final_output) if final_output is not None else joined_text
    context_wrapper = getattr(streamed, "context_wrapper", None)
    usage = usage_as_dict(getattr(context_wrapper, "usage", None))
    last_response_id = getattr(streamed, "last_response_id", None)
    if isinstance(last_response_id, str) and last_response_id:
        usage["last_response_id"] = last_response_id
    stop_reason = "complete" if getattr(streamed, "is_complete", False) else ""
    return RunResult(
        text=text, session_id=session_id, usage=usage, stop_reason=stop_reason
    )


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


class OpenAIAgentsSession:
    """:class:`HarnessSession` implementation over ``openai-agents``.

    Lifecycle mirrors the Protocol (and the production ``ClaudeSDKClient``
    loop it was modelled on): one :meth:`start` (auth + ``Agent`` +
    ``SQLiteSession`` construction — no network), N :meth:`send` turns
    (each one ``Runner.run_streamed`` call, streaming
    :class:`NormalizedEvent`, terminating in ``kind="result"``), then
    :meth:`close`.

    Args:
        agent_name: sac agent identity — names the SDK ``Agent`` and the
            default state-db file.
        model: OpenAI model id. ``None`` → ``SAC_OPENAI_MODEL`` env →
            the SDK's own default.
        instructions: System prompt for the SDK ``Agent`` (``None`` keeps
            the SDK default).
        tools: :class:`ToolSpec` items converted via
            :func:`tool_spec_to_function_tool` at :meth:`start`.
        session_id: Logical conversation key inside the state db.
            Defaults to ``agent_name``.
        db_path: SQLite file override (``":memory:"`` for ephemeral
            state). Default: ``resolve_state_db_path(agent_name)``.
        max_turns: Optional cap forwarded to ``Runner.run_streamed``.
        record_spend: Record per-turn token usage into the
            :mod:`_account.openai_usage` spend ledger (best-effort).
        tracing: Opt IN to the SDK's tracing exporter. Default ``False``
            — sac sessions must not POST trace payloads to OpenAI's
            dashboard as a side effect (privacy + no surprise background
            requests; offline runs would also log noisy non-fatal
            tracing-client errors).
    """

    def __init__(
        self,
        agent_name: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        tools: Sequence[ToolSpec] = (),
        session_id: str | None = None,
        db_path: str | Path | None = None,
        max_turns: int | None = None,
        record_spend: bool = True,
        tracing: bool = False,
    ) -> None:
        self.agent_name = agent_name
        self.model = model
        self.instructions = instructions
        self.tools = tuple(tools)
        self.session_id = session_id or agent_name
        self.db_path = db_path
        self.max_turns = max_turns
        self.record_spend = record_spend
        self.tracing = tracing
        self._agent: Any = None
        self._session: Any = None
        self._started = False

    # -- HarnessSession surface ----------------------------------------

    async def start(self) -> None:
        """Open the session: auth, tool conversion, ``Agent`` + ``SQLiteSession``.

        Network-free — the first API call happens on :meth:`send`.
        Raises :class:`OpenAISessionError` if ``openai-agents`` is not
        installed, and
        :class:`~runtimes._openai_sdk_common.OpenAISDKCommonError` if no
        API key is available.
        """
        agents = _import_agents()
        from ..runtimes._openai_sdk_common import (
            default_openai_model,
            provision_openai_auth,
            resolve_state_db_path,
        )

        provision_openai_auth()
        function_tools = [tool_spec_to_function_tool(t) for t in self.tools]
        model = self.model or default_openai_model()
        agent_kwargs: dict[str, Any] = {
            "name": self.agent_name,
            "tools": function_tools,
        }
        if self.instructions is not None:
            agent_kwargs["instructions"] = self.instructions
        if model:
            agent_kwargs["model"] = model
        self._agent = agents.Agent(**agent_kwargs)
        db_path = (
            self.db_path
            if self.db_path is not None
            else resolve_state_db_path(self.agent_name)
        )
        self._session = agents.SQLiteSession(self.session_id, db_path=str(db_path))
        self._started = True

    async def send(self, message: Message) -> AsyncIterator[NormalizedEvent]:
        """Run one turn via ``Runner.run_streamed`` and yield normalized events.

        The last event of a completed turn is ``kind="result"`` carrying
        the :class:`RunResult`; a failing turn yields ``kind="error"``
        instead (per the Protocol docstring both are turn-ending).
        """
        if not self._started:
            raise OpenAISessionError("OpenAIAgentsSession.send() called before start().")
        agents = _import_agents()

        joined: list[str] = []
        try:
            run_kwargs: dict[str, Any] = {"session": self._session}
            if self.max_turns is not None:
                run_kwargs["max_turns"] = self.max_turns
            if not self.tracing:
                run_kwargs["run_config"] = agents.RunConfig(tracing_disabled=True)
            streamed = agents.Runner.run_streamed(
                self._agent, message.content, **run_kwargs
            )
            async for raw_event in streamed.stream_events():
                normalized = normalize_stream_event(raw_event)
                if normalized is None:
                    continue
                if normalized.kind == "text_delta":
                    joined.append(normalized.text)
                yield normalized
        except asyncio.CancelledError:  # cooperative cancellation stays loud
            raise
        except Exception as exc:  # stx-allow: fallback (reason: SDK/network surface is broad; the Protocol contract is a turn-ending kind="error" event, not an exception mid-iteration)
            yield NormalizedEvent(kind="error", error=str(exc), raw=exc)
            return

        result = _run_result_from_streamed(streamed, self.session_id, "".join(joined))
        if self.record_spend:
            self._record_spend(result)
        yield NormalizedEvent(kind="result", result=result, raw=streamed)

    async def close(self) -> None:
        """Tear down the session (closes the ``SQLiteSession`` db handle)."""
        session, self._session = self._session, None
        self._agent = None
        self._started = False
        close = getattr(session, "close", None)
        if callable(close):
            close()

    # -- internals -------------------------------------------------------

    def _record_spend(self, result: RunResult) -> None:
        """Append this turn's usage to the spend ledger (best-effort)."""
        # stx-allow: fallback (reason: spend accounting must never fail the turn; the ledger itself is documented best-effort)
        try:
            from .._account.openai_usage import record_usage

            record_usage(
                result.usage,
                model=self.model or "openai-default",
                agent=self.agent_name,
            )
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            pass
