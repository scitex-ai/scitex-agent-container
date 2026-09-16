"""Periodic sender-supervision reconciler for durable A2A lifecycles."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 30.0
ENV_DISABLED = "SAC_DELEGATION_RECONCILER_DISABLED"
ENV_INTERVAL_S = "SAC_DELEGATION_RECONCILER_INTERVAL_S"


async def reconcile_once(
    *,
    publish: Callable[..., Awaitable[dict[str, Any]]],
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Notify senders at-least-once for expired deadlines/failures.

    The marker is appended after the durable channel publish.  A crash in
    between can therefore duplicate a notice, but cannot permanently suppress
    it; ``notice_id`` is stable so consumers can deduplicate that narrow case.
    """
    from .._state.delegation_lifecycle import (
        FAILURE_REPORTED,
        PROGRESS_NUDGE_SENT,
        RECEIPT_NUDGE_SENT,
        due_supervision_actions,
        record_stage,
    )

    current = time.time() if now is None else float(now)
    actions = await asyncio.to_thread(due_supervision_actions, now=current)
    emitted: list[dict[str, Any]] = []
    for action in actions:
        marker = str(action["action"])
        dispatch_id = str(action["dispatch_id"])
        sender = str(action["sender"])
        assignee = str(action["assignee"])
        if marker == RECEIPT_NUDGE_SENT:
            summary = f"No agent-observed receipt from {assignee!r} before deadline."
        elif marker == PROGRESS_NUDGE_SENT:
            summary = f"No terminal progress from {assignee!r} before deadline."
        elif marker == FAILURE_REPORTED:
            summary = f"Delegated execution by {assignee!r} reported failure."
        else:  # defensive: due_supervision_actions owns the closed set
            continue
        payload = {
            "kind": "delegation_supervision",
            "notice_id": f"{dispatch_id}:{marker}",
            "dispatch_id": dispatch_id,
            "correlation_id": action["correlation_id"],
            "lineage_id": action["lineage_id"],
            "assignee": assignee,
            "stage": marker,
            "message": summary,
        }
        await publish(
            agent=sender,
            body=json.dumps(payload, sort_keys=True),
            from_agent="daemon",
            kind="delegation_supervision",
            extra={
                "dispatch_id": dispatch_id,
                "correlation_id": action["correlation_id"],
                "lineage_id": action["lineage_id"],
            },
        )
        await asyncio.to_thread(
            record_stage, dispatch_id, marker, detail=summary, now=current
        )
        emitted.append(payload)
    return emitted


async def delegation_reconciler_loop(
    app_state: Any,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
) -> None:
    """Run reconciliation off-loop forever; errors are loud and retryable."""
    from ._notify import publish_to_agent

    async def _publish(**kwargs: Any) -> dict[str, Any]:
        return await publish_to_agent(app_state.inbox, **kwargs)

    while True:
        await asyncio.sleep(interval_s)
        try:
            await reconcile_once(publish=_publish)
        except asyncio.CancelledError:
            raise
        except (
            Exception
        ):  # stx-allow: fallback (a failed tick must not kill reconciliation)
            logger.exception("delegation lifecycle reconciliation failed")


__all__ = [
    "DEFAULT_INTERVAL_S",
    "ENV_DISABLED",
    "ENV_INTERVAL_S",
    "delegation_reconciler_loop",
    "reconcile_once",
]
