"""Project neutral channel envelopes into one resident runner inbox."""

from __future__ import annotations

import asyncio
from typing import Any

from .._mcp._channel_wake import _wake_text
from ._session_inbox import Inbox, TurnEnvelope


async def dispatch_to_session(event: dict[str, Any], *, inbox: Inbox) -> None:
    """Admit one durable event and wait for the harness-issued receipt.

    ``WakeableInbox`` is the common harness boundary.  Claude's driver,
    Hermes' external turn endpoint, and Codex app-server each own the action
    after admission.  In particular, Codex maps an arrival during a live turn
    to native ``turn/steer`` and resolves this future only after that RPC is
    accepted (PR #1411).  A transport consumer may therefore ACK its durable
    row only after this coroutine returns.
    """
    loop = asyncio.get_running_loop()
    requester = event.get("from_agent")
    dispatch_id = event.get("dispatch_id")
    envelope = TurnEnvelope(
        text=_wake_text(event),
        response=loop.create_future(),
        from_agent=(
            requester
            if isinstance(requester, str) and requester and requester != "unknown"
            else None
        ),
        dispatch_id=(
            dispatch_id if isinstance(dispatch_id, str) and dispatch_id else None
        ),
    )
    await inbox.put(envelope)
    await envelope.response


__all__ = ["dispatch_to_session"]
