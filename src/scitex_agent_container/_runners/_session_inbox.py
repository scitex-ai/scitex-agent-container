"""Inbound-turn channel for the claude-session runner.

The runner owns one persistent ``ClaudeSDKClient``. Producers (the
mission boot envelope, the colocated A2A HTTP route in PR2) enqueue
envelopes here; the conversation task drains them serially, calling
``client.query(text)`` per turn and resolving the per-envelope future
with the assistant's reply text.

Serial processing — turns wait until the prior turn's
``receive_response()`` drain finishes. That matches Claude Code's own
UX (next prompt waits) and avoids interleaving SDK state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Union


@dataclass
class TurnEnvelope:
    """One user turn to feed into the runner's persistent SDK client.

    ``session_id`` is set by the conversation task once the SDK emits a
    ``ResultMessage`` with the resume id. Consumers (e.g. the HTTP
    sidecar) read it AFTER the ``response`` future resolves to surface
    the SDK session id in their reply body. It stays ``None`` until then
    so a still-running turn cannot leak a stale id.

    ``turn_id`` is an optional caller-supplied uuid that ties together
    the four state-transition rows (``queued`` / ``delivered`` /
    ``read`` / ``responded``) the runner writes into the
    ``state.db.turns`` diary table. ``None`` means the diary is not
    tracking this envelope (legacy / test-only producers).

    ``dispatch_id`` is the SENDER-minted dispatch-ledger id (see
    :mod:`scitex_agent_container._state.dispatch_ledger`). The sender
    stamps it onto the ``/v1/turn`` POST body; the inbound HTTP handler
    threads it here so the receiver side can correlate this turn back to
    the originating dispatch row. ``None`` when the dispatch was not
    minted through the ledger (legacy / test-only producers).

    ``from_agent`` is the REQUESTER identity — the peer (any node, not a
    special-cased lead) that dispatched this turn. The sender stamps it
    onto the ``/v1/turn`` POST body (``peer.post_turn``) or it rides in
    via the channel wake path's body; the inbound HTTP handler threads it
    here so the Stop hook can PUSH a completion report back to whoever
    asked. ``None`` for a mission boot turn or a legacy caller that does
    not declare a requester — the Stop hook then has nobody to address
    and the push is skipped (not an error: a mission turn answers to no
    peer).
    """

    text: str
    response: asyncio.Future = field(repr=False)
    exit_after: bool = False
    session_id: str | None = None
    turn_id: str | None = None
    dispatch_id: str | None = None
    from_agent: str | None = None


@dataclass
class ShutdownEnvelope:
    """Tell the conversation task to stop draining."""


Envelope = Union[TurnEnvelope, ShutdownEnvelope]
Inbox = "asyncio.Queue[Envelope]"


def make_inbox() -> "asyncio.Queue[Envelope]":
    """Build an unbounded queue for envelopes."""
    return asyncio.Queue()


__all__ = [
    "Envelope",
    "Inbox",
    "ShutdownEnvelope",
    "TurnEnvelope",
    "make_inbox",
]
