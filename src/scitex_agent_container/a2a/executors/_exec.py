"""Exec executor — runs ``$SAC_A2A_EXEC_COMMAND`` with user text on stdin."""

from __future__ import annotations

from scitex_agent_container.a2a._handlers import handle_exec
from scitex_agent_container.a2a.executors._base import BaseSyncExecutor


class ExecExecutor(BaseSyncExecutor):
    """Dispatch the user message to a configurable subprocess."""

    handler_key = "exec"

    def _run_sync(self, agent_name: str, user_text: str) -> str:
        return handle_exec(agent_name, user_text)


__all__ = ["ExecExecutor"]
