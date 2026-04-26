"""Echo executor — canned reply, zero deps. Default ``spec.a2a.handler``."""

from __future__ import annotations

from scitex_agent_container.a2a._handlers import handle_echo
from scitex_agent_container.a2a.executors._base import BaseSyncExecutor


class EchoExecutor(BaseSyncExecutor):
    """Echo back the user text — proves the protocol surface end-to-end."""

    handler_key = "echo"

    def _run_sync(self, agent_name: str, user_text: str) -> str:
        return handle_echo(agent_name, user_text)


__all__ = ["EchoExecutor"]
