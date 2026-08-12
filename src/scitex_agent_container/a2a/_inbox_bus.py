"""Per-agent inbox pub/sub for the A2A channel primitive.

A turn POSTed at ``/agents/<name>`` lands in the SDK dispatcher
(which handles the JSON-RPC reply path), but it also fans out to every
SSE subscriber currently connected to
``/agents/<name>/inbox/stream``.

That stream is what ``sac mcp channel`` consumes inside the agent's
container, turning each fan-out event into a
``notifications/claude/channel`` push so the running Claude session
sees ``<channel source="..." ts="..." msg_id="...">`` tags in real
time. Non-sac A2A clients can poll the same stream — it's plain SSE
over HTTP, no sac dependency.

The bus is intentionally in-process and in-memory:

* One ``Broker`` per sac-listen process (host or per-agent sidecar).
* One bounded ``asyncio.Queue`` per (agent, subscriber). Bounded so a
  slow consumer can't grow the queue without limit; oldest pending
  message drops when the cap is hit.
* No persistence IN THE BUS — a peer that POSTs while no subscriber is
  connected gets nothing buffered *here*.

  READ THAT NARROWLY. It is a statement about THIS MODULE, not about the
  A2A rail, and taking it as the latter is a mistake this docstring has
  already caused (2026-08-08: I told the operator "a2a does not persist",
  and it does). ``_server.py`` calls ``persist_event`` to write the event
  into ``state.db``'s ``channel_events`` BEFORE it calls ``publish`` here,
  precisely so a bus-only drop cannot lose it — and the row id it returns
  is the SSE ``id:``, which a reconnecting client replays from via
  ``Last-Event-ID``. So the RAIL is durable and replayable; the BUS is the
  live fan-out in front of it.

  Verify that by reading ``_server.py`` and querying ``channel_events``,
  not by trusting this paragraph. Documents can lie; the implementation
  and a measurement cannot (operator, 2026-08-08: 「ドキュメントは嘘を
  つけるので、実装と実測を常にエビデンスにしてください」).

The broker has no auth — the routes that publish / subscribe enforce
the same bearer-auth shape as the rest of sac listen.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import defaultdict
from typing import Any

# Per-subscriber queue cap. 64 is plenty for interactive flows; a flood
# beyond that is almost certainly a misbehaving sender — we'd rather
# drop oldest than block the publisher's HTTP handler.
_QUEUE_CAP = 64


class _Keepalive:
    """Sentinel: the stream is IDLE — emit a beat, do NOT close."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "KEEPALIVE"


#: Returned by :meth:`Broker.get_or_close` when no event arrived within
#: ``keepalive_after`` and the broker is NOT closing. Deliberately distinct
#: from ``None`` (closing): "nothing has happened yet" and "stop" are
#: different facts, and collapsing them into one is how a healthy idle
#: stream gets torn down.
KEEPALIVE = _Keepalive()

# How often an IDLE inbox stream emits a keepalive comment frame.
#
# This is not cosmetic. A stream that never writes cannot be told apart from a
# stream that has DIED, and the CLIENT is the one that pays: a connection that
# died SILENTLY (no FIN, no RST — a hard host death, a wedged uvicorn, an idle
# NAT/firewall flow drop) parks the consumer on an unbounded read forever. It
# still believes it is subscribed; the broker holds no subscriber for it; every
# message aimed at that agent lands on an empty bus. That is deafness with no
# error raised anywhere, curable only by restarting the agent — the same shape
# as the 2026-07-01 fleet-comms outage, which lived in the CONNECT path until
# #591 bounded it. The READ path was left unbounded; the beat is what closes it,
# by giving the client bytes so a bounded read deadline can fire and it can
# re-dial (see ``_mcp/channel.py::_consume_sse``).
#
# Server-side, be precise about what this buys, because it is narrower than it
# looks: uvicorn ALREADY reaps a subscriber whose client closes cleanly or
# resets (it sees ``connection_lost`` and cancels the response, running the
# stream's ``finally``). The beat adds nothing there. What it adds is that on an
# idle stream the beat is the ONLY write, so a peer that vanished with NO TCP
# signal at all will eventually error out on write (TCP retransmit timeout)
# rather than holding its subscriber slot indefinitely — which keeps
# ``subscriber_counts`` (and the ``delivered_subscriber_count`` that ``a2a_send``
# fails loudly on) from indefinitely reporting a subscriber nobody can reach.
DEFAULT_KEEPALIVE_INTERVAL_S = 15.0
ENV_KEEPALIVE_INTERVAL_S = "SAC_INBOX_KEEPALIVE_S"


def keepalive_interval_s() -> float:
    """Seconds between keepalive beats on an idle inbox stream.

    Read from the environment at CALL time, never baked into a module-level
    constant at import: an import-time ``float(os.environ[...])`` cannot be
    redirected by a test (or an operator) that sets the env afterwards, and a
    knob that silently ignores its own env var is worse than no knob at all.

    A malformed or non-positive value falls back to the default rather than
    disabling the beat. Disabling it is precisely the silent-deafness footgun
    this exists to prevent, so a typo must never buy it.
    """
    raw = os.environ.get(ENV_KEEPALIVE_INTERVAL_S)
    if raw is None:
        return DEFAULT_KEEPALIVE_INTERVAL_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_KEEPALIVE_INTERVAL_S
    return value if value > 0 else DEFAULT_KEEPALIVE_INTERVAL_S


# Canonical ``from_agent`` (sender) value for messages sac's OWN
# daemon originates — as opposed to an agent's a2a reasoning, which
# keeps its own ``from_agent``.
#
# Operator directive (2026-07-05, bracket form): the channel tag an
# agent sees renders as ``<- <source> [<sender>]`` where ``source``
# stays the CLEAN, unsuffixed channel name (sac's channel identity,
# e.g. ``sac``) and the SENDER (bracket) says whether the frame came
# from the daemon or a peer agent. ``_build_notification`` projects
# the event's ``from_agent`` into ``meta.source`` (the bracket), so a
# sac daemon frame with ``from_agent=DAEMON_SENDER`` renders as
# ``<- sac [daemon]`` and an agent frame renders ``<- sac [<agent>]``.
#
# This tag is applied ONLY to messages sac itself originates as the
# daemon with no sender-supplied source. A message that already
# carries a sender's ``from_agent`` (agent a2a) or another channel's
# tag passes through UNCHANGED — sac never re-tags those, and the
# source is never suffixed.
DAEMON_SENDER = "daemon"


class Broker:
    """In-memory pub/sub keyed by agent name."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # Set on graceful shutdown so the SSE inbox-stream loops stop
        # parking on ``queue.get()`` and return promptly (card
        # ``sac-listen-sigterm-sse-shutdown-hang``). ``asyncio.Event``
        # binds its loop lazily on first ``wait()``, so constructing it
        # here (outside any running loop, mirroring ``self._lock``) is
        # safe.
        self._closing = asyncio.Event()

    def closing_event(self) -> asyncio.Event:
        """Return the shutdown Event the SSE loops race ``get()`` against."""
        return self._closing

    def is_closing(self) -> bool:
        """True once :meth:`close` has fired (graceful shutdown started)."""
        return self._closing.is_set()

    def close(self) -> None:
        """Signal every SSE subscriber loop to stop promptly.

        Idempotent. Sets the shutdown Event the inbox-stream loops race
        their ``queue.get()`` against (see :meth:`get_or_close`), so a
        graceful ``sac listen`` shutdown (SIGTERM) cancels in-flight SSE
        connections at once instead of leaving them parked on
        ``queue.get()`` until uvicorn force-cancels / ``restart --force``
        escalates to SIGKILL after 10 s.
        """
        self._closing.set()

    async def get_or_close(
        self,
        q: asyncio.Queue[dict[str, Any]],
        *,
        keepalive_after: float | None = None,
    ) -> dict[str, Any] | _Keepalive | None:
        """Await the next event on ``q``. Three outcomes, never two.

        * an **event** — deliver it;
        * :data:`KEEPALIVE` — ``keepalive_after`` seconds passed with no event
          and the broker is NOT closing. The stream is healthy and idle: beat,
          do not close. Only returned when ``keepalive_after`` is set;
        * ``None`` — the broker is closing. Stop.

        The SSE inbox-stream handlers loop on this instead of a bare
        ``await q.get()``. A bare ``get()`` parks forever when no event is
        flowing, so uvicorn's graceful shutdown (SIGTERM) waits on the
        in-flight stream until it force-cancels / ``sac listen restart
        --force`` escalates to SIGKILL after 10 s (card
        ``sac-listen-sigterm-sse-shutdown-hang``). Racing ``get()`` against
        the shutdown Event lets the loop return the instant :meth:`close`
        fires, so the daemon exits cleanly.

        ``keepalive_after`` adds the third outcome so an idle stream can emit a
        beat (see :func:`keepalive_interval_s` for why a silent stream is a
        deafness bug in both directions). ``None`` keeps the original two-state
        contract verbatim for callers that do not want beats.

        **An event is never lost to a beat.** A live event still in flight when
        the timeout (or close) fires is returned, not dropped: we check
        ``get_task`` for a result BEFORE treating the wait as idle. And
        cancelling a pending ``Queue.get()`` does not consume an item —
        ``asyncio.Queue.get`` only calls ``get_nowait()`` after its getter
        future resolves, so an item that arrives in the cancellation race stays
        queued and the next call picks it up.
        """
        if self._closing.is_set():
            return None
        get_task = asyncio.ensure_future(q.get())
        close_task = asyncio.ensure_future(self._closing.wait())
        try:
            await asyncio.wait(
                {get_task, close_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=keepalive_after,
            )
        except asyncio.CancelledError:
            # The whole stream is being cancelled (client gone, or
            # uvicorn's timeout-graceful-shutdown force path). Clean up
            # both child futures before re-raising so neither is left
            # pending.
            get_task.cancel()
            close_task.cancel()
            raise
        # Prefer a delivered event even if close (or the keepalive timeout)
        # fired on the same tick — an event already pulled off the queue must
        # not be dropped.
        if (
            get_task.done()
            and not get_task.cancelled()
            and get_task.exception() is None
        ):
            close_task.cancel()
            return get_task.result()
        # No event. Abandon the pending get (the item, if any, stays queued)
        # and decide WHY we woke: shutdown, or merely an idle stream.
        get_task.cancel()
        close_task.cancel()
        if self._closing.is_set():
            return None
        if keepalive_after is not None:
            # Idle, not closing. This is the distinction the whole ternary
            # exists for: reporting "closed" here would tear down a perfectly
            # healthy stream every keepalive interval.
            return KEEPALIVE
        return None

    async def subscribe(self, agent: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_CAP)
        async with self._lock:
            self._subs[agent].add(q)
        return q

    async def unsubscribe(self, agent: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subs.get(agent, set()).discard(q)
            if not self._subs.get(agent):
                self._subs.pop(agent, None)

    async def publish(self, agent: str, event: dict[str, Any]) -> int:
        """Fan ``event`` out to every subscriber for ``agent``.

        Returns the count of subscribers that accepted the event. When
        a queue is full we drop the oldest entry to make room — slow
        consumer cannot back up the publisher.
        """
        async with self._lock:
            queues = list(self._subs.get(agent, ()))
        delivered = 0
        for q in queues:
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                # Drop oldest; retry once. If still full the consumer
                # is wedged — skip this delivery (next event has a
                # fresh shot at fitting after the drop).
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
                try:
                    q.put_nowait(event)
                    delivered += 1
                except asyncio.QueueFull:  # pragma: no cover
                    pass
        return delivered

    async def subscriber_count(self, agent: str) -> int:
        async with self._lock:
            return len(self._subs.get(agent, ()))

    async def subscriber_counts(self) -> dict[str, int]:
        """Snapshot every agent's live subscriber count in ONE lock take.

        This is the control plane's only OBSERVATION of reachability — the
        registry can say an agent is running and ``active`` while its inbox
        adapter is not attached here, in which case :meth:`publish` fans out
        to nobody and the message wakes no one. ``GET /agents`` reports this
        count per row so "registered" and "reachable" stay distinguishable
        (see ``_listen/_reachability.py``).

        One lock acquisition rather than N (as repeated
        :meth:`subscriber_count` calls would take) so annotating a whole
        peer list stays O(1) in lock round-trips and cannot stall the
        ``/agents`` route.

        Agents with zero subscribers are simply absent from ``_subs``
        (:meth:`unsubscribe` pops the empty set), so a name missing from the
        returned dict means zero — callers must treat "absent" as 0, which
        :func:`_listen._reachability.annotate_reachability` does.
        """
        async with self._lock:
            return {agent: len(queues) for agent, queues in self._subs.items()}


def mint_event(
    agent: str,
    content: str,
    *,
    from_agent: str | None = None,
    conversation_id: str | None = None,
    in_reply_to: str | None = None,
    priority: str = "normal",
    requires_reply: bool = False,
    ack: bool = False,
    extra: dict[str, Any] | None = None,
    kind: str | None = None,
    dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Mint the event shape sac's channel publishes.

    Sender side fills ``content`` + optional metadata; sac listen
    enriches with ``msg_id`` and ``ts`` so receivers can ack/reply
    against stable identifiers.

    ``ack`` marks the event as itself a read-receipt (the same flag
    the ``a2a_ack`` tool stamps under ``metadata.ack``). It must
    survive minting so the receiving adapter's auto-ack loop-guard
    can recognise an ack and decline to ack it back — otherwise two
    auto-ack adapters ping-pong forever.

    ``dispatch_id`` is the SENDER-minted dispatch-ledger id (see
    :mod:`scitex_agent_container._state.dispatch_ledger`). It rides on
    the event so the channel wake path can thread it onto the woken
    turn's ``/v1/turn`` body, letting the receiver's Stop hook correlate
    its completion push back to the originating dispatch row. Omitted
    when the sender did not mint a ledger id.
    """
    event: dict[str, Any] = {
        "msg_id": uuid.uuid4().hex,
        "to_agent": agent,
        "from_agent": from_agent or "unknown",
        "ts": time.time(),
        "content": content,
        "priority": priority,
        "requires_reply": requires_reply,
        "ack": ack,
    }
    if conversation_id:
        event["conversation_id"] = conversation_id
    if in_reply_to:
        event["in_reply_to"] = in_reply_to
    if dispatch_id:
        event["dispatch_id"] = dispatch_id
    if extra:
        event["extra"] = extra
    if kind:
        event["kind"] = kind
    return event


def mint_deny_notification(
    *,
    target: str,
    from_agent: str | None,
    reason: str,
) -> dict[str, Any]:
    """Mint a denied-attempt notification event for the RECEIVER.

    Comms item D (fail-loud on ACL-denied sends): when ``check_send_acl``
    refuses an inbound ``message:send``, the sender already gets a 403
    with the reason — but the receiver was previously told nothing,
    leaving them unable to decide whether to grant the sender. This
    helper produces the notification the receiver sees instead.

    The shape mirrors a normal :func:`mint_event` envelope so SSE
    consumers and ``channel_events`` persistence work unchanged, with
    two deliberate differences:

    * ``content`` is the empty string — **the message body never
      leaks** to an unauthorized receiver. Only attempt metadata
      (from / to / reason / timestamp) is published.
    * ``kind`` is ``"denied_attempt"`` so receivers can distinguish
      it from a real message at a glance, and the deny reason rides
      under ``extra.deny_reason``.

    ``from_agent`` should be the *effective* sender identity from the
    ACL decision (authenticated bearer name, or the claimed name on
    the admin-caller path). ``None`` is rendered as ``"unknown"`` by
    ``mint_event`` — that's the only honest answer when neither was
    presented (the "no identity at all" deny).
    """
    return mint_event(
        target,
        content="",
        from_agent=from_agent,
        priority="normal",
        kind="denied_attempt",
        extra={"deny_reason": reason},
    )


def mint_acl_deny_synthetic_notification(
    *,
    target: str,
    sender: str,
    reason: str,
) -> dict[str, Any]:
    """Mint the rate-limited synthetic system notification for ACL-deny.

    sac-comms item D (lead a2a ``c42b3e3c`` — merged with
    ``lead-sac-acl-blocked-attempt-notification``). When an outbound
    ``a2a_send(sender, target)`` is ACL-denied, the receiver gets ONE
    synthetic system-level notification per cool-down window (default
    30 min, see :mod:`_state.state_db_acl_deny_notify`).

    Differences from :func:`mint_deny_notification`:

    * ``from_agent`` is the canonical daemon sender
      :data:`DAEMON_SENDER` (``"daemon"``, not the would-be sender) —
      the receiver MUST know the notification did not originate from a
      granted peer (the message body never leaks pre-decision, and
      surfacing the sender as the apparent author is the same leak in a
      different shape). Rendered in the channel bracket as
      ``<- sac [daemon]`` so the receiver distinguishes this SAC daemon
      frame from an agent-authored message.
    * ``content`` carries a human-readable, operator-actionable
      string embedding the exact ``sac a2a grant`` command keyed to
      the actual ``<sender>`` / ``<target>`` names. This is the
      "synthetic notification that bypasses ACL" the operator can
      act on without scrolling structured fields.
    * ``kind`` is ``"acl_deny_notify"`` so receivers can distinguish
      a SYNTHETIC system frame from a real (granted-and-delivered)
      message or the per-attempt ``"denied_attempt"`` envelope.

    The ``extra`` block carries the structured fields a richer
    client (Telegram bridge / dedicated UI) can branch on without
    re-parsing the content string.

    REPLACES the prior parent/child auto-grant policy: the operator
    sees the attempt and grants if intended, rather than the system
    auto-granting on a lineage heuristic.
    """
    content = (
        f"[system] Sender {sender!r} attempted a send to {target!r} "
        "and was blocked by ACL.\n"
        f"Grant via `sac a2a grant {sender} {target}` if intended."
    )
    return mint_event(
        target,
        content=content,
        from_agent=DAEMON_SENDER,
        priority="normal",
        kind="acl_deny_notify",
        extra={
            "acl_deny_notify": True,
            "blocked_sender": sender,
            "blocked_target": target,
            "deny_reason": reason,
            "grant_command": f"sac a2a grant {sender} {target}",
        },
    )


__all__ = [
    "DAEMON_SENDER",
    "DEFAULT_KEEPALIVE_INTERVAL_S",
    "ENV_KEEPALIVE_INTERVAL_S",
    "KEEPALIVE",
    "Broker",
    "keepalive_interval_s",
    "mint_event",
    "mint_deny_notification",
    "mint_acl_deny_synthetic_notification",
]
