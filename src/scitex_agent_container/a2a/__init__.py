"""Generic A2A protocol support for scitex-agent-container.

`A2A <https://a2a-protocol.org/>`_ is an open agent-to-agent protocol —
treat it like HTTP: the *protocol* is open, the *implementation* is
ours. sac knowing A2A does not couple it to any particular fleet
runtime (orochi, etc.); a single agent can expose its own A2A
endpoint with sac alone, no fleet dependency.

This package provides:

* :mod:`._card` — v3 YAML → A2A AgentCard projection. No fleet-specific
  fields; sac-internal extensions live under ``x-scitex-agent-container``
  (NOT ``x-orochi``).
* :mod:`._handlers` — pluggable JSON-RPC ``tasks/send`` handlers
  (``echo`` / ``claude_cli`` / ``exec``) — used by the legacy
  byte-compat path.
* :mod:`.executors` — corresponding :class:`a2a.server.agent_execution.AgentExecutor`
  subclasses driven by the official `a2a-sdk` (Phase 1).
* :mod:`._server` — Starlette app tying the projection, the SDK
  dispatcher (with v0.3 compat for SSE/streaming), and the legacy
  byte-compat path together.

CLI: ``sac a2a serve <agent.yaml> [--port N] [--handler ...]``.
"""

from scitex_agent_container.a2a._card import fleet_card, project_card
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
    "serve",
]
