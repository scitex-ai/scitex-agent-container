"""Base ``AgentExecutor`` for sac — wraps a sync ``(name, text) -> str``
function as an a2a-sdk executor.

Phase 1 keeps the handlers sync (echo / claude_cli / exec) — they don't
stream natively. The base wraps the call in ``asyncio.to_thread`` so the
event loop isn't blocked, then enqueues a single artifact + a terminal
``completed``/``failed`` status event. Subclasses override
:meth:`agent_name` and :meth:`run` (or just :attr:`handler`).

Phase 2 (long-running handlers — claude_session / tmux_inject) will
introduce truly async/streaming subclasses that emit incremental
artifact events as output appears.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Role, Task, TaskState, TaskStatus
from a2a.types.a2a_pb2 import Message
from a2a.types.a2a_pb2 import Part as PbPart

from scitex_agent_container.a2a._handlers import HandlerError

log = logging.getLogger(__name__)


def _text_part(text: str) -> PbPart:
    """Build a single text-only proto ``Part`` from a Python string."""
    return PbPart(text=text)


def _agent_text_message(text: str, task_id: str, context_id: str) -> Message:
    """Build a proto ``Message`` (role=agent) carrying a single text part."""
    msg = Message(role=Role.ROLE_AGENT, task_id=task_id, context_id=context_id)
    msg.parts.append(_text_part(text))
    return msg


class BaseSyncExecutor(AgentExecutor):
    """Bridge between a sync ``(name, text) -> str`` handler and the SDK."""

    #: Yaml key used to select this executor (``spec.a2a.handler``).
    handler_key: str = "base"

    def __init__(self, agent_name: str, **kwargs: Any) -> None:
        self.agent_name = agent_name
        self.kwargs = kwargs

    # --- abstract -----------------------------------------------------

    @abstractmethod
    def _run_sync(self, agent_name: str, user_text: str) -> str:
        """Sync work — produce the agent's reply text. Raise ``HandlerError`` on failure."""

    # --- AgentExecutor interface --------------------------------------

    async def execute(  # type: ignore[override]
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Pull task identifiers — the SDK's RequestContext fills these in.
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        user_text = context.get_user_input()

        # SDK 1.0 requires the executor to enqueue an initial Task event
        # before any TaskStatusUpdateEvent — the framework will not
        # synthesize one. (Compare: the v0.3 LegacyRequestHandler did
        # this implicitly.)
        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )

        updater = TaskUpdater(event_queue, task_id=task_id, context_id=context_id)

        # Announce we're working — useful for SSE consumers.
        await updater.start_work()

        try:
            reply = await asyncio.to_thread(self._run_sync, self.agent_name, user_text)
        except HandlerError as exc:
            log.warning("a2a executor %r failed: %s", self.handler_key, exc)
            await updater.failed(
                message=_agent_text_message(str(exc), task_id, context_id)
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("a2a executor %r crashed", self.handler_key)
            await updater.failed(
                message=_agent_text_message(
                    f"executor crashed: {exc}", task_id, context_id
                )
            )
            return

        await updater.add_artifact(parts=[_text_part(reply)], name="reply")
        await updater.complete(message=_agent_text_message(reply, task_id, context_id))

    async def cancel(  # type: ignore[override]
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Phase 1 sync handlers can't be interrupted mid-flight; just ack
        # the cancellation so the framework clears state.
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        updater = TaskUpdater(event_queue, task_id=task_id, context_id=context_id)
        await updater.update_status(
            TaskState.TASK_STATE_CANCELED,
            message=_agent_text_message(
                "cancelled (sync handler)", task_id, context_id
            ),
        )


__all__ = ["BaseSyncExecutor", "_text_part", "_agent_text_message"]
