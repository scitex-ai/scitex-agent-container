"""Wake-on-push primitives for the sac MCP **channel** adapter (WI-1).

A pushed A2A message to an IDLE containerized agent must WAKE it and drive
a turn immediately — push must behave like the lead's Telegram channel,
where a pushed message is processed now rather than buffered until some
unrelated next turn. The ``notifications/claude/channel`` push that the
receive-side adapter emits renders a ``<channel>`` tag for an *active* turn
but does NOT advance an idle session's turn.

This module is the wake mechanism: given the agent's own colocated
``/v1/turn`` endpoint URL, it POSTs each qualifying bus event there so the
runner enqueues it onto the persistent SDK conversation and drives a turn
at once. Extracted from :mod:`scitex_agent_container._mcp.channel` (which
hit the module size budget); ``channel`` re-exports these for the historical
import path.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["_should_wake_turn", "_wake_text", "_wake_turn"]

# Bounded client-side wait for the wake POST.
#
# Load-resilience fix (incident 2026-07-09): the pre-fix value was
# ``timeout=None`` (INFINITE). Under a host load spike the colocated runner's
# ``/v1/turn`` was wedged and never returned, so the wake POST blocked the SSE
# consumer FOREVER — a slow/hung upstream must never let an MCP-side handler hang
# without bound. The runner answers within its own bounded per-turn deadline
# (``_runners/_session_http.DEFAULT_TURN_TIMEOUT_S`` = 120 s) and a 504, so we
# wait that plus a margin. Env-overridable for deployments that raise the runner
# turn timeout via ``SAC_A2A_TURN_TIMEOUT_S``. See ``docs/mcp-load-resilience.md``.
_WAKE_TIMEOUT_DEFAULT_S: float = 180.0
_WAKE_TIMEOUT_ENV_VAR = "SAC_MCP_WAKE_TIMEOUT_S"


def _resolve_wake_timeout() -> float:
    """Return the bounded client-side wait (seconds) for the wake POST.

    ``SAC_MCP_WAKE_TIMEOUT_S`` overrides :data:`_WAKE_TIMEOUT_DEFAULT_S`; an
    unparseable value is ignored (logged) and the default stands. Always a
    FINITE, positive bound — never ``None`` — so a wedged ``/v1/turn`` cannot
    block the SSE consumer indefinitely.
    """
    raw = os.environ.get(_WAKE_TIMEOUT_ENV_VAR, "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            log.warning(
                "wake: ignoring invalid %s=%r (not a float seconds value)",
                _WAKE_TIMEOUT_ENV_VAR,
                raw,
            )
        else:
            if val > 0:
                return val
            log.warning(
                "wake: ignoring non-positive %s=%r; using default %.0fs",
                _WAKE_TIMEOUT_ENV_VAR,
                raw,
                _WAKE_TIMEOUT_DEFAULT_S,
            )
    return _WAKE_TIMEOUT_DEFAULT_S


def _should_wake_turn(event: dict[str, Any]) -> bool:
    """Whether a received bus event should DRIVE a turn (wake-on-push).

    A pushed message to an idle agent must wake it and be processed now —
    push ≡ Telegram. We drive a turn for normal inbound messages but skip:

    - **acks** (truthy ``ack`` flag): a stage-2 read-receipt carries no
      content for the agent to act on; driving a turn on every ack would
      burn a turn (and tokens) per receipt and could ping-pong with the
      auto-ack side-effect.
    - **infra kinds** (``kind`` in ``completion`` / ``reaction``): a
      completion report is the END of a request the requester already made
      — it informs, it is not a new request. Waking a turn on it restarts
      the cycle: two peers each finish a turn, each Stop hook pushes a
      completion report that wakes the other, and they ping-pong forever
      emitting "Holding" (observed 2026-06-24, neurovista ⇆ scitex-writer).
      Reactions are structural receipts too. Both still DELIVER as a
      ``<channel>`` notification — they just do not DRIVE a fresh turn.
    - **empty content**: nothing to feed the SDK as turn input.

    The notification push (the ``<channel>`` tag) still fires for these in
    the no-wake path — only the turn-driving wake is gated here.
    """
    if event.get("ack"):
        return False
    if event.get("kind") in ("completion", "reaction"):
        return False
    content = event.get("content")
    return isinstance(content, str) and content.strip() != ""


def _wake_text(event: dict[str, Any]) -> str:
    """Render the turn input fed to the agent's ``/v1/turn`` on wake.

    Mirrors the ``<channel ...>`` framing Claude renders for an in-session
    push so a woken (idle) agent sees the same shape it would have seen had
    the notification arrived mid-turn — source, msg_id, and the message
    body. This keeps the wake path behaviourally identical to the lead's
    Telegram channel: the message is processed as a real turn, attributed
    to its sender.
    """
    source = event.get("from_agent", "unknown")
    msg_id = event.get("msg_id", "")
    content = event.get("content", "")
    return f'<channel source="{source}" msg_id="{msg_id}">\n{content}\n</channel>'


async def _wake_turn(
    event: dict[str, Any],
    *,
    turn_url: str,
    bearer: str | None,
    timeout: float | None = None,
) -> None:
    """POST ``event`` to the agent's own ``/v1/turn`` to DRIVE a turn now.

    This is the wake-on-push primitive: the colocated runner's ``/v1/turn``
    endpoint enqueues the text onto the persistent SDK conversation and
    drives a turn immediately, so a push to an IDLE agent is processed at
    once rather than buffered until some unrelated next turn. Raises on any
    transport/HTTP failure so the caller can decide whether to surface or
    contain it (WI-2 fail-loud) — the SSE consumer's ``on_event`` wrapper
    catches it, logs loudly, and keeps the long-lived stream alive.

    Requester identity rides into the body so the woken turn's
    ``TurnEnvelope`` carries it through to the Stop hook, which PUSHes a
    completion report back to whoever asked. ``from_agent`` is the event's
    sender (the requesting peer — generalizes to ANY peer, the lead is not
    special-cased); ``dispatch_id`` is the sender-minted ledger id when the
    sender minted one. Both are tolerated-absent: an event with no sender /
    no ledger id simply drives a turn the Stop hook cannot address.

    ``timeout`` bounds the client-side wait (seconds). ``None`` (the default)
    resolves via :func:`_resolve_wake_timeout` to a FINITE bound just above the
    runner's own per-turn deadline — never ``None``/infinite (incident
    2026-07-09: a wedged ``/v1/turn`` under load hung the unbounded POST forever,
    stalling the SSE consumer). A generous-but-finite bound still lets a
    legitimately long turn complete while guaranteeing recovery from a wedged
    runner.
    """
    import httpx

    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    payload: dict[str, Any] = {"text": _wake_text(event)}
    requester = event.get("from_agent")
    if isinstance(requester, str) and requester and requester != "unknown":
        # ``mint_event`` defaults a missing sender to the literal
        # ``"unknown"`` — that is not an addressable peer, so don't thread
        # it as a requester (the Stop hook would otherwise try to push to a
        # node named "unknown").
        payload["from_agent"] = requester
    dispatch_id = event.get("dispatch_id")
    if isinstance(dispatch_id, str) and dispatch_id:
        payload["dispatch_id"] = dispatch_id
    effective_timeout = _resolve_wake_timeout() if timeout is None else timeout
    async with httpx.AsyncClient(timeout=effective_timeout) as client:
        resp = await client.post(turn_url, json=payload, headers=headers)
        resp.raise_for_status()
