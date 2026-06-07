"""Inbound-turn channel for the claude-session runner.

The runner owns one persistent ``ClaudeSDKClient``. Producers (the
mission boot envelope, the colocated A2A HTTP route in PR2) enqueue
envelopes here; the conversation task drains them serially, calling
``client.query(text)`` per turn and resolving the per-envelope future
with the assistant's reply text.

Serial processing — turns wait until the prior turn's
``receive_response()`` drain finishes. That matches Claude Code's own
UX (next prompt waits) and avoids interleaving SDK state.

Wake-on-inbound (#41)
---------------------
:class:`WakeableInbox` is :class:`asyncio.Queue` plus a sibling
:class:`asyncio.Event` (``_not_empty``) that fires the moment an
item is put and clears the moment the queue is fully drained. The
conversation loop uses the event-side via
:meth:`WakeableInbox.wait_for_item` to detect that an envelope
arrived MID-TURN — i.e. while the SDK iterator is still streaming
for the prior envelope. A dedicated wake task then calls
``client.interrupt()`` so the running turn winds down cleanly, the
conversation loop reaches ``inbox.get()`` again, and the queued
envelope is dequeued promptly instead of waiting for the in-flight
SDK monitor / bash tool to return on its own (the wedge mode the
operator hit on proj-paper-scitex-clew + proj-neurovista,
2026-06-07 — lead a2a ``f39bdcc5``).

The wrapper exposes the SAME async-public surface as
:class:`asyncio.Queue` (``put`` / ``put_nowait`` / ``get`` /
``qsize`` / ``empty``) so every existing call site — HTTP handler,
mission boot, tests — keeps working byte-equivalently. Only the
new wake task uses :meth:`wait_for_item`.
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


class WakeableInbox:
    """``asyncio.Queue`` + a non-destructive "queue is non-empty" event.

    Public surface mirrors :class:`asyncio.Queue` so existing callers
    that ``await inbox.put(env)`` / ``await inbox.get()`` /
    ``inbox.qsize()`` / ``inbox.empty()`` work byte-equivalently.

    The new surface is :meth:`wait_for_item`: an async no-arg coroutine
    that blocks until at least one envelope is currently queued. Unlike
    :meth:`get`, it does NOT consume the envelope — multiple callers can
    wait_for_item() concurrently and all wake on the same put. The wake
    task in the conversation loop uses this so the same envelope can
    later be consumed by the actual ``inbox.get()`` of the next
    iteration. Per #41 / lead a2a ``f39bdcc5``.

    Thread-safety contract is the same as :class:`asyncio.Queue` —
    coordinator and producers must share one event loop. The HTTP
    sidecar and the conversation loop already do (the sidecar is
    spawned on the same loop as the conversation task in
    ``_session_http.start_sidecar``).
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[Envelope] = asyncio.Queue()
        self._not_empty: asyncio.Event = asyncio.Event()

    async def put(self, item: Envelope) -> None:
        """Enqueue and signal the wake event. Mirrors
        :meth:`asyncio.Queue.put` (unbounded queue → never blocks)."""
        await self._q.put(item)
        self._not_empty.set()

    def put_nowait(self, item: Envelope) -> None:
        """Synchronous enqueue + signal. Mirrors
        :meth:`asyncio.Queue.put_nowait`."""
        self._q.put_nowait(item)
        self._not_empty.set()

    async def get(self) -> Envelope:
        """Dequeue one item, blocking until available. Clears the wake
        event IFF the queue is fully drained after this get."""
        item = await self._q.get()
        if self._q.empty():
            self._not_empty.clear()
        return item

    def get_nowait(self) -> Envelope:
        """Dequeue one item synchronously or raise :class:`asyncio.QueueEmpty`.

        Mirrors :meth:`asyncio.Queue.get_nowait`. Used by
        ``_drain_failed_inbox`` in :mod:`_session_conversation` to
        resolve every queued envelope's response future with the
        startup exception when the SDK fails to import — that path is
        synchronous (no event loop running for the queued futures), so
        we cannot ``await`` here. Clears the wake event IFF the queue
        is fully drained after this get.
        """
        item = self._q.get_nowait()
        if self._q.empty():
            self._not_empty.clear()
        return item

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()

    async def wait_for_item(self) -> None:
        """Block until at least one envelope is currently queued.

        Non-destructive: the envelope stays in the queue and is still
        the next return from :meth:`get`. Returns immediately if the
        queue is already non-empty at call time. Multiple awaiters
        wake on the same put.
        """
        await self._not_empty.wait()


# Type alias for callers that still annotate against the abstract
# "inbox" type. WakeableInbox is the concrete shape today; the union
# preserves the asyncio.Queue annotation for back-compat with any
# external test fixture that constructs its own bare queue.
Inbox = Union[WakeableInbox, "asyncio.Queue[Envelope]"]


def make_inbox() -> WakeableInbox:
    """Build an unbounded :class:`WakeableInbox` for envelopes."""
    return WakeableInbox()


__all__ = [
    "Envelope",
    "Inbox",
    "ShutdownEnvelope",
    "TurnEnvelope",
    "WakeableInbox",
    "make_inbox",
]
