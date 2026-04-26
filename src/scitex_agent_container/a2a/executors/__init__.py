"""Pluggable A2A ``AgentExecutor`` implementations for sac.

Each executor subclasses :class:`a2a.server.agent_execution.AgentExecutor`
and implements ``async execute(context, event_queue)``. They convert the
behavior previously expressed as sync handlers in
:mod:`scitex_agent_container.a2a._handlers` into the SDK's task-event
streaming model.

The lookup table :data:`EXECUTORS` maps the same yaml ``spec.a2a.handler``
keys (``echo`` / ``claude_cli`` / ``exec``) to executor classes, so the
serve CLI can pick one by name.
"""

from __future__ import annotations

from typing import Type

from scitex_agent_container.a2a.executors._base import BaseSyncExecutor
from scitex_agent_container.a2a.executors._claude_cli import ClaudeCliExecutor
from scitex_agent_container.a2a.executors._echo import EchoExecutor
from scitex_agent_container.a2a.executors._exec import ExecExecutor

EXECUTORS: dict[str, Type[BaseSyncExecutor]] = {
    "echo": EchoExecutor,
    "claude_cli": ClaudeCliExecutor,
    "exec": ExecExecutor,
}

__all__ = [
    "BaseSyncExecutor",
    "ClaudeCliExecutor",
    "EchoExecutor",
    "EXECUTORS",
    "ExecExecutor",
]
