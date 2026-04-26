"""Claude CLI executor — runs ``claude --print`` once with the user text."""

from __future__ import annotations

from scitex_agent_container.a2a._handlers import handle_claude_cli
from scitex_agent_container.a2a.executors._base import BaseSyncExecutor


class ClaudeCliExecutor(BaseSyncExecutor):
    """Run ``claude --print``, capture stdout, return as agent reply."""

    handler_key = "claude_cli"

    def _run_sync(self, agent_name: str, user_text: str) -> str:
        return handle_claude_cli(agent_name, user_text)


__all__ = ["ClaudeCliExecutor"]
