"""Claude SDK session executor — drives ``claude-agent-sdk`` (no ``--print``).

Recommended replacement for :class:`ClaudeCliExecutor`. Same wire surface
(sync ``(name, text) -> str``), but the underlying transport is
Anthropic's official structured-streaming SDK that survives
``--print`` deprecation.
"""

from __future__ import annotations

from scitex_agent_container.a2a._handlers import handle_claude_session
from scitex_agent_container.a2a.executors._base import BaseSyncExecutor


class ClaudeSessionExecutor(BaseSyncExecutor):
    """Drive Claude via ``claude_agent_sdk.query`` and return assistant text."""

    handler_key = "claude_session"

    def _run_sync(self, agent_name: str, user_text: str) -> str:
        return handle_claude_session(
            agent_name,
            user_text,
            channels=self.kwargs.get("channels") or [],
            a2a_port=self.kwargs.get("a2a_port"),
            permission_mode=self.kwargs.get("permission_mode"),
        )


__all__ = ["ClaudeSessionExecutor"]
