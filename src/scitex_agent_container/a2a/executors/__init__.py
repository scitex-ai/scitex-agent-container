"""Pluggable A2A ``AgentExecutor`` implementations for sac.

Each executor subclasses :class:`a2a.server.agent_execution.AgentExecutor`
and implements ``async execute(context, event_queue)``. They convert the
behavior previously expressed as sync handlers in
:mod:`scitex_agent_container.a2a._handlers` into the SDK's task-event
streaming model.

The lookup table :data:`EXECUTORS` maps the same yaml ``spec.a2a.handler``
keys (``echo`` / ``claude_session`` / ``claude_cli`` / ``exec``) to
executor classes, so the serve CLI can pick one by name.

``claude_session`` is recommended for new agents — it uses Anthropic's
official ``claude-agent-sdk`` and survives ``--print`` deprecation.
``claude_cli`` remains for back-compat.
"""

from __future__ import annotations

from typing import Type

from scitex_agent_container.a2a.executors._base import BaseSyncExecutor
from scitex_agent_container.a2a.executors._claude_cli import ClaudeCliExecutor
from scitex_agent_container.a2a.executors._claude_session import (
    ClaudeSessionExecutor,
)
from scitex_agent_container.a2a.executors._echo import EchoExecutor
from scitex_agent_container.a2a.executors._exec import ExecExecutor

EXECUTORS: dict[str, Type[BaseSyncExecutor]] = {
    "echo": EchoExecutor,
    "claude_session": ClaudeSessionExecutor,  # recommended (SDK-backed)
    "claude_cli": ClaudeCliExecutor,  # legacy (--print, deprecating)
    "exec": ExecExecutor,
}

__all__ = [
    "BaseSyncExecutor",
    "ClaudeCliExecutor",
    "ClaudeSessionExecutor",
    "EchoExecutor",
    "EXECUTORS",
    "ExecExecutor",
]
