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

from typing import Any

__all__ = ["_should_wake_turn", "_wake_text", "_wake_turn"]


def _should_wake_turn(event: dict[str, Any]) -> bool:
    """Whether a received bus event should DRIVE a turn (wake-on-push).

    A pushed message to an idle agent must wake it and be processed now —
    push ≡ Telegram. We drive a turn for normal inbound messages but skip:

    - **acks** (truthy ``ack`` flag): a stage-2 read-receipt carries no
      content for the agent to act on; driving a turn on every ack would
      burn a turn (and tokens) per receipt and could ping-pong with the
      auto-ack side-effect.
    - **empty content**: nothing to feed the SDK as turn input.

    The notification push (the ``<channel>`` tag) still fires for these in
    the no-wake path — only the turn-driving wake is gated here.
    """
    if event.get("ack"):
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
) -> None:
    """POST ``event`` to the agent's own ``/v1/turn`` to DRIVE a turn now.

    This is the wake-on-push primitive: the colocated runner's ``/v1/turn``
    endpoint enqueues the text onto the persistent SDK conversation and
    drives a turn immediately, so a push to an IDLE agent is processed at
    once rather than buffered until some unrelated next turn. Raises on any
    transport/HTTP failure so the caller can decide whether to surface or
    contain it (WI-2 fail-loud).
    """
    import httpx

    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    payload = {"text": _wake_text(event)}
    # The wake POST returns only after the driven turn completes (the runner
    # awaits the SDK reply before responding). Use no client-side deadline —
    # a short client timeout would abort a legitimately long turn; the runner
    # imposes its own bounded per-turn timeout and answers with a 504.
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(turn_url, json=payload, headers=headers)
        resp.raise_for_status()
