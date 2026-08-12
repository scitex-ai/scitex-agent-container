"""OpenAI SDK session executor — drives ``openai-agents`` (spec.provider: openai).

Sibling of :class:`ClaudeSessionExecutor` for the OpenAI agent-SDK
family (scitex-todo card ``openai-compat-2``). Same wire surface (sync
``(name, text) -> str``); the underlying transport is
``agents.Runner.run_streamed`` normalized through
:class:`scitex_agent_container._runners.openai_session.OpenAIAgentsSession`,
with conversation state persisted in the agent's ``SQLiteSession`` db.

Requires the optional ``[openai]`` extra
(``pip install scitex-agent-container[openai]``); without it the handler
raises a clear ``HandlerError`` at dispatch time — importing this module
stays dependency-free for Claude-only deployments.
"""

from __future__ import annotations

from scitex_agent_container.a2a._handlers import handle_openai_session
from scitex_agent_container.a2a.executors._base import BaseSyncExecutor


class OpenAISessionExecutor(BaseSyncExecutor):
    """Drive OpenAI via ``openai-agents`` and return the assistant text."""

    handler_key = "openai_session"

    def _run_sync(self, agent_name: str, user_text: str) -> str:
        return handle_openai_session(agent_name, user_text)


__all__ = ["OpenAISessionExecutor"]
