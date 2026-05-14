"""SDK hook callback bridge for the claude-session runner.

Wires Anthropic's hook event taxonomy
(``PreToolUse`` / ``PostToolUse`` / ``UserPromptSubmit`` / ``Stop``)
to ``scitex_agent_container.event_log.append_event`` using the same
record schema the legacy CLI runtime publishes via
``sac record-hook-event``. Keeping that schema identical means
downstream consumers (``sac agent status``, ``event_log.summarize``,
fleet dashboards) work unchanged when an agent flips runtimes.

Hook callbacks are *async no-ops on the wire*: they return ``{}`` to
the SDK and never block. ``append_event`` is itself swallowed-failure,
so a misbehaving hook cannot kill the agent.
"""

from __future__ import annotations

from typing import Any


def build_event_log_hooks(agent_name: str, hook_matcher_cls: Any) -> dict:
    """Return the ``hooks=`` dict passed to ``ClaudeAgentOptions``.

    Each event class registers exactly one matcher with one callback;
    the callback forwards the SDK payload's relevant fields to
    ``event_log.append_event`` under the matching legacy ``kind``.
    """
    from .._state.event_log import append_event

    async def _on_pretool(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "pretool",
            {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
            },
        )
        return {}

    async def _on_posttool(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "posttool",
            {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
                "tool_response": payload.get("tool_response"),
            },
        )
        return {}

    async def _on_prompt(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "prompt",
            {"prompt": payload.get("prompt", "")},
        )
        return {}

    async def _on_stop(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "stop",
            {"stop_hook_active": bool(payload.get("stop_hook_active"))},
        )
        return {}

    return {
        "PreToolUse": [hook_matcher_cls(hooks=[_on_pretool])],
        "PostToolUse": [hook_matcher_cls(hooks=[_on_posttool])],
        "UserPromptSubmit": [hook_matcher_cls(hooks=[_on_prompt])],
        "Stop": [hook_matcher_cls(hooks=[_on_stop])],
    }
