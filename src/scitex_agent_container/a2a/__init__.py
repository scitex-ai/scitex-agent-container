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
  (``echo`` / ``claude_cli`` / ``exec``).
* :mod:`._server` — stdlib HTTP server tying the projection and the
  handler together.

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
from scitex_agent_container.a2a._server import serve

__all__ = [
    "HANDLERS",
    "HandlerError",
    "fleet_card",
    "handle_claude_cli",
    "handle_echo",
    "handle_exec",
    "project_card",
    "serve",
]
