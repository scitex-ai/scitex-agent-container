"""The long-lived SSE inbox consumer, and the reconnect policy it enforces.

Extracted from :mod:`.channel` (which mixes this with the MCP-session adapter
that pushes received events into the running Claude session, and sat over the
per-file cap). This half deserves to stand alone: it is the component whose
failure DEAFENS an agent, and every past outage in this area has been a bug in
exactly one of the three deadlines below.

THE INVARIANT: an agent that survives ``sac listen`` going away must
re-subscribe on its own, with **no agent restart**. The only way to hold that is
for every way a connection can end to route back to the top of the retry loop.
Three of them, each historically unbounded, each one an outage:

``connect``  bounded by :data:`_SSE_CONNECT_TIMEOUT_S`.
    Until #591 this was ``timeout=None``. During the 2026-07-01 outage every
    reconnect hung forever inside ``client.stream(...)``, so the backoff loop
    never came around: agents stayed alive and permanently unsubscribed, curable
    only by restarting all 14 of them.

``read``     bounded by :func:`_sse_read_timeout_s`.
    #591 left this unbounded. A stream that is merely QUIET is byte-for-byte
    indistinguishable from one that has died SILENTLY (no FIN, no RST — a hard
    host death, a wedged uvicorn, an idle NAT/firewall flow drop), so the
    consumer parked here forever, still believing it was subscribed while the
    broker held no subscriber for it. ``sac listen`` now beats ``: keepalive``
    down every idle stream (``a2a._inbox_bus.keepalive_interval_s``), so silence
    past the deadline is real evidence of death — and evidence is what a
    reconnect needs.

``backoff``  capped by :data:`_SSE_BACKOFF_CAP_S`, and JITTERED.
    Every agent on a host subscribes to the same listen, so they all lose the
    stream in the same instant and climb an identical ladder in lockstep. See
    :func:`_jittered_backoff`.

Reconnecting is cheap and IDEMPOTENT — the stream replays undelivered rows on
connect — so every deadline here errs toward re-dialling. A false positive costs
one reconnect; a false negative costs the agent every message it will ever be
sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Any

log = logging.getLogger(__name__)

# Bound the SSE CONNECT phase (see module docstring — #591).
_SSE_CONNECT_TIMEOUT_S: float = 30.0

# ...and bound the READ, which #591 left unbounded. Must exceed the server's
# keepalive beat (default 15s) by a wide margin so several missed beats are
# tolerated before a healthy stream is torn down.
_DEFAULT_SSE_READ_TIMEOUT_S: float = 60.0
_ENV_SSE_READ_TIMEOUT_S = "SAC_MCP_SSE_READ_TIMEOUT_S"

# Ceiling on the retry ladder. Bounds the worst-case window between listen
# coming back and this adapter noticing. Messages are NOT lost in that window —
# they are persisted before publish and replayed on connect — so the cost of the
# cap is latency, not delivery.
_SSE_BACKOFF_CAP_S: float = 30.0

_SSE_BACKOFF_START_S: float = 0.5


def _sse_read_timeout_s() -> float:
    """Seconds of total silence before the SSE read is declared dead.

    Read from the env at CALL time, never baked into a module-level constant at
    import: an import-time ``float(os.environ[...])`` cannot be redirected by a
    test (or an operator) that sets the var afterwards, and a knob that silently
    ignores its own env var is worse than no knob.

    A malformed or non-positive value falls back to the default. Never returns
    ``None``: "wait forever" is the bug, not a configuration.
    """
    raw = os.environ.get(_ENV_SSE_READ_TIMEOUT_S)
    if raw is None:
        return _DEFAULT_SSE_READ_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SSE_READ_TIMEOUT_S
    return value if value > 0 else _DEFAULT_SSE_READ_TIMEOUT_S


def _jittered_backoff(backoff: float) -> float:
    """Spread a retry across the BACK HALF of its backoff window.

    Every agent on a host subscribes to the same ``sac listen``. When it goes
    away they all lose the stream in the same instant and climb an IDENTICAL
    ladder — 0.5s, 1s, 2s, 4s … — re-dialling in lockstep. That is a thundering
    herd aimed at a process which is, by definition, in the middle of coming
    back up: the fleet's ~14 adapters land on it simultaneously at every rung,
    which is an excellent way to knock over the very thing they are all waiting
    for (and this fleet HAS watched a listen die and take every inbox with it).

    Equal jitter — half the window, plus a random half — decorrelates them while
    keeping a floor, so recovery latency is unchanged but the arrivals are
    smeared across the window instead of stacked on its edge.
    """
    half = backoff / 2.0
    return half + random.random() * half


async def _consume_sse(
    url: str,
    bearer: str | None,
    on_event: "callable[[dict[str, Any]], asyncio.Future[None]]",
) -> None:
    """Long-lived SSE consumer. Reconnects with jittered backoff on disconnect.

    Each ``event: message`` frame's ``data:`` line is JSON-decoded and handed to
    ``on_event``. Comment frames (``: ...``) are ignored as CONTENT — sac listen
    emits one on connect (``: sac-channel ready``) and then beats ``: keepalive``
    down every idle stream. Ignored as content is NOT ignored as signal: each
    beat is bytes arriving, which resets the read deadline and is how this
    consumer tells a quiet stream from a dead one.

    This loop must never exit: the process has no other path back to subscribed.
    Even a non-200 (say, a stale bearer) is logged and retried.
    """
    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        # Catch broadly: optional deps can fail at *import time* with
        # non-ImportError errors (e.g. a misconfigured transitive dep
        # raising RuntimeError). Surface them as an actionable
        # ImportError so the caller knows install/upgrade is needed.
        raise ImportError(
            "httpx is required for sac mcp channel — install with `pip install httpx`"
        ) from exc

    headers = {"Accept": "text/event-stream"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    # Replay cursor: the row id of the last event we actually
    # dispatched, echoed back as ``Last-Event-ID`` so a reconnect resumes
    # where we stopped instead of at "now".
    #
    # WITHOUT THIS, EVERY DISCONNECT SILENTLY DROPS MESSAGES. The server
    # side has always supported replay — ``a2a/_inbox_stream.py`` stamps the
    # row id on every frame's ``id:`` line and replays ``id > Last-Event-ID``
    # from ``channel_events`` — but this consumer parsed only ``data:``,
    # discarded the ``id:`` line, and rebuilt no header on reconnect. So the
    # durable log kept everything and the agent was simply never handed the
    # events that arrived while it was re-dialing.
    #
    # Measured 2026-08-09: across ONE `sac listen` restart my a2a_inbox went
    # 10 items -> 2 and scitex-storage's 2 -> 0, and we jointly filed it as a
    # durability defect ("it cost a real message"). It had not: channel_events
    # held 355 rows, 51 addressed to me, spanning the restart, 350 stamped
    # delivered — including the message I reported lost. The loss was in the
    # asking, not in the storing.
    #
    # None means "no cursor yet" (first connect) and is DISTINCT from 0, which
    # would be a valid row id meaning "replay everything". Only send the header
    # once we hold an id the server gave us: the server 400s a malformed
    # cursor, and inventing one would turn a reconnect into a hard failure.
    last_event_id: str | None = None

    backoff = _SSE_BACKOFF_START_S
    sse_timeout = httpx.Timeout(_SSE_CONNECT_TIMEOUT_S, read=_sse_read_timeout_s())
    while True:
        try:
            # Rebuild per attempt so the cursor advances across reconnects.
            # A dict built once outside the loop would pin the FIRST cursor
            # forever and replay the same window on every re-dial.
            attempt_headers = dict(headers)
            if last_event_id is not None:
                attempt_headers["Last-Event-ID"] = last_event_id
            async with httpx.AsyncClient(timeout=sse_timeout) as client:
                async with client.stream("GET", url, headers=attempt_headers) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        log.warning(
                            "sac channel SSE %s returned %d: %s",
                            url,
                            resp.status_code,
                            body[:200],
                        )
                    else:
                        backoff = _SSE_BACKOFF_START_S
                        data_lines: list[str] = []
                        frame_id: str | None = None
                        async for line in resp.aiter_lines():
                            if not line:
                                # frame separator — dispatch what we have
                                if data_lines:
                                    payload = "\n".join(data_lines)
                                    data_lines = []
                                    pending_id = frame_id
                                    frame_id = None
                                    try:
                                        event = json.loads(payload)
                                    except json.JSONDecodeError:
                                        log.warning(
                                            "sac channel SSE bad JSON: %r",
                                            payload[:200],
                                        )
                                        continue
                                    await on_event(event)
                                    # Advance the cursor ONLY after on_event
                                    # returns. Advancing on receipt would ack
                                    # an event we then failed to hand over —
                                    # the reconnect would skip past it and the
                                    # message would be lost for good, which is
                                    # the exact failure this whole change
                                    # exists to close.
                                    if pending_id is not None:
                                        last_event_id = pending_id
                                else:
                                    frame_id = None
                                continue
                            if line.startswith(":"):
                                continue  # comment frame (incl. the keepalive beat)
                            if line.startswith("id:"):
                                frame_id = line[3:].strip()
                                continue
                            if line.startswith("data:"):
                                data_lines.append(line[5:].lstrip())
        except Exception as exc:  # stx-allow: fallback (reason: long-lived SSE — must retry on any transient error, including the ReadTimeout that a silently-dead stream now raises)
            log.warning(
                "sac channel SSE error (%s); reconnecting in ~%.1fs", exc, backoff
            )
        await asyncio.sleep(_jittered_backoff(backoff))
        backoff = min(backoff * 2, _SSE_BACKOFF_CAP_S)


__all__ = [
    "_consume_sse",
    "_jittered_backoff",
    "_sse_read_timeout_s",
    "_SSE_BACKOFF_CAP_S",
    "_SSE_CONNECT_TIMEOUT_S",
]
