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
    """

    text: str
    response: asyncio.Future = field(repr=False)
    exit_after: bool = False
    session_id: str | None = None


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
