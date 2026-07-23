"""Generic A2A protocol support for scitex-agent-container.

`A2A <https://a2a-protocol.org/>`_ is an open agent-to-agent protocol —
treat it like HTTP: the *protocol* is open, the *implementation* is
ours. sac knowing A2A does not couple it to any particular fleet
runtime; a single agent can expose its own A2A
endpoint with sac alone, no fleet dependency.

This package provides:

* :mod:`._card` — v3 YAML → A2A AgentCard projection. No fleet-specific
  fields; sac-internal extensions live under ``x-scitex-agent-container``.
* :mod:`._handlers` — pluggable sync ``(name, text) -> str`` handlers
  (``echo`` / ``claude_cli`` / ``exec``) used by the executors.
* :mod:`.executors` — corresponding :class:`a2a.server.agent_execution.AgentExecutor`
  subclasses driven by the official `a2a-sdk`.
* :mod:`._server` — Starlette app tying the dict-card routes and the
  pure SDK JSON-RPC dispatcher together. No legacy compat layer —
  current A2A spec only.

CLI: ``sac a2a serve <agent.yaml> [--port N] [--handler ...]``.
"""

from scitex_agent_container.a2a._card import (
    fleet_card,
    project_card,
    project_card_proto,
)
from scitex_agent_container.a2a._handlers import (
    HANDLERS,
    HandlerError,
    handle_claude_cli,
    handle_echo,
    handle_exec,
)
from scitex_agent_container.a2a._server import build_app, serve
from scitex_agent_container.a2a.executors import (
    EXECUTORS,
    BaseSyncExecutor,
    ClaudeCliExecutor,
    EchoExecutor,
    ExecExecutor,
)

__all__ = [
    "BaseSyncExecutor",
    "ClaudeCliExecutor",
    "EXECUTORS",
    "EchoExecutor",
    "ExecExecutor",
    "HANDLERS",
    "HandlerError",
    "build_app",
    "fleet_card",
    "handle_claude_cli",
    "handle_echo",
    "handle_exec",
    "project_card",
    "project_card_proto",
    "serve",
]
