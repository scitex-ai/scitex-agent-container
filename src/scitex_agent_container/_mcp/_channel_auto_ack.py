"""sac channel adapter — auto-ack subsystem.

Extracted from :mod:`scitex_agent_container._mcp.channel` to keep that
module under the line-size budget. Hosts the stage-2 read-receipt
machinery the receive-side adapter triggers after a successful
inbox-event injection:

* :func:`_auto_ack_enabled` — env-gated master switch
  (``SAC_CHANNEL_AUTO_ACK``).
* :func:`_should_auto_ack` — per-event loop-guard predicate (never ack an
  ack, never ack an event with no identifiable sender).
* :func:`_auto_ack_rate_limits` / :func:`_auto_ack_rate_allow` and the
  ``_auto_ack_window`` / ``_auto_ack_tripped`` state — belt-and-suspenders
  sliding-window rate cap so any regression in loop-guarding self-
  terminates after the budget is exhausted.
* :func:`_post_auto_ack` — the outbound POST itself. Honors the operator's
  sender-side empty-ack noise filter (drops contentless acks BEFORE they
  leave the outbound queue — see :mod:`._channel_ack_filter`).

``channel.py`` re-exports every name here for historical import paths
(``from scitex_agent_container._mcp.channel import _post_auto_ack``).
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any

from .._env import getenv as _sac_env

log = logging.getLogger(__name__)

__all__ = [
    "_AUTO_ACK_RATE_MAX_DEFAULT",
    "_AUTO_ACK_RATE_WINDOW_DEFAULT",
    "_auto_ack_enabled",
    "_auto_ack_rate_allow",
    "_auto_ack_rate_limits",
    "_auto_ack_tripped",
    "_auto_ack_window",
    "_post_auto_ack",
    "_should_auto_ack",
]


def _auto_ack_enabled() -> bool:
    """Whether the receive-side adapter emits an automatic ``a2a_ack`` the
    moment it injects a received bus event into the session.

    Two-stage receipt, infra-automatic: the receiving agent calls nothing —
    the channel adapter does. Stage 1 ("delivered") is the send response's
    ``delivered_subscriber_count``; stage 2 ("read") is this auto-ack,
    emitted on injection. Default ON; set ``SAC_CHANNEL_AUTO_ACK`` to one of
    ``0/false/no/off`` (case-insensitive) to disable.
    """
    raw = os.environ.get("SAC_CHANNEL_AUTO_ACK")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _should_auto_ack(event: dict[str, Any]) -> bool:
    """Loop-guard for the auto-ack side-effect.

    An auto-ack is itself a message that lands in the sender's inbox and is
    injected by *their* adapter — which would auto-ack back, ad infinitum.
    Break the cycle:

    - never auto-ack an event that is itself an ack (truthy ``ack`` flag —
      the same metadata flag the ``a2a_ack`` tool stamps), and
    - never auto-ack an event with no identifiable original sender
      (``from_agent`` missing/empty): there is nowhere to send the receipt.
    """
    if not event.get("from_agent"):
        return False
    if event.get("ack"):
        return False
    return True


# --- Auto-ack rate limiter (belt-and-suspenders loop breaker) -------------
# Even with the ack-flag loop-guard (``_should_auto_ack``), a future
# regression in flag propagation could restart an ack-on-ack cycle. This
# sliding-window cap ensures any such loop self-terminates: once a sender
# exceeds the budget within the window we stop auto-acking it and log
# loudly (fail-loud, never a silent drop).
_AUTO_ACK_RATE_MAX_DEFAULT = 20
_AUTO_ACK_RATE_WINDOW_DEFAULT = 60.0
# Per-sender sliding window of auto-ack emission timestamps.
_auto_ack_window: "dict[str, deque[float]]" = {}
# Senders currently over budget — latches the loud log to once per trip.
_auto_ack_tripped: "set[str]" = set()


def _auto_ack_rate_limits() -> "tuple[int, float]":
    """Resolve ``(max_acks, window_seconds)`` for the auto-ack cap.

    Configurable via ``SAC_AUTO_ACK_RATE_MAX`` / ``SAC_AUTO_ACK_RATE_WINDOW_S``
    (and their ``SCITEX_AGENT_CONTAINER_*`` aliases). A non-positive max
    disables the cap (explicit opt-out).
    """
    raw_max = _sac_env("AUTO_ACK_RATE_MAX")
    raw_win = _sac_env("AUTO_ACK_RATE_WINDOW_S")
    try:
        max_n = int(raw_max) if raw_max is not None else _AUTO_ACK_RATE_MAX_DEFAULT
    except (TypeError, ValueError):
        max_n = _AUTO_ACK_RATE_MAX_DEFAULT
    try:
        window = (
            float(raw_win) if raw_win is not None else _AUTO_ACK_RATE_WINDOW_DEFAULT
        )
    except (TypeError, ValueError):
        window = _AUTO_ACK_RATE_WINDOW_DEFAULT
    return max_n, window


def _auto_ack_rate_allow(sender: str, *, now: "float | None" = None) -> bool:
    """Whether an auto-ack to ``sender`` is within the rate budget.

    Sliding-window cap keyed by sender. Returns ``True`` and records the
    emission when within budget; returns ``False`` (logging loudly, once
    per trip) when the cap is exceeded inside the window. After the window
    clears, emission resumes and the loud-log latch resets so a fresh loop
    is reported again. ``now`` is injectable for deterministic tests.
    """
    max_n, window = _auto_ack_rate_limits()
    if max_n <= 0:
        return True
    ts = time.monotonic() if now is None else now
    dq = _auto_ack_window.setdefault(sender, deque())
    cutoff = ts - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= max_n:
        if sender not in _auto_ack_tripped:
            _auto_ack_tripped.add(sender)
            log.warning(
                "sac channel: auto-ack rate cap hit for sender %r "
                "(%d auto-acks within %.0fs) — suppressing further "
                "auto-acks to this sender until the window clears. "
                "Possible ack loop.",
                sender,
                len(dq),
                window,
            )
        return False
    dq.append(ts)
    _auto_ack_tripped.discard(sender)
    return True


async def _post_auto_ack(
    event: dict[str, Any],
    *,
    agent_name: str,
    listen_url: str,
    bearer: str | None,
) -> None:
    """POST an automatic ``a2a_ack`` back to the sender of ``event``.

    Reuses the exact ``/agents/<sender>/message:send`` path and metadata
    shape the ``a2a_ack`` tool uses (``ack=True``, ``in_reply_to`` the
    original ``msg_id``, same ``conversation_id``). ``ack=True`` is the
    loop-guard marker: the sender's adapter sees it and won't ack back.

    Caller guarantees ``event`` passed :func:`_should_auto_ack` (so
    ``from_agent`` is present and the event is not itself an ack).

    **Sender-side empty-ack noise filter:** before the outbound POST is
    built and sent, the assembled envelope is checked against
    :func:`._channel_ack_filter.envelope_is_contentless_ack`. A
    contentless ack (empty body + ``metadata.ack=True``) is dropped
    silently with a debug log — it carries no semantic payload and the
    operator's contract is to keep noise off the wire. The receive-side
    ``_should_auto_ack`` loop-guard above stays in place as belt-and-
    suspenders.
    """
    import uuid as _uuid

    import httpx

    from ._channel_ack_filter import envelope_is_contentless_ack

    target = event["from_agent"]
    metadata: dict[str, Any] = {"from_agent": agent_name, "ack": True}
    msg_id = event.get("msg_id")
    if msg_id:
        metadata["in_reply_to"] = msg_id
    conversation_id = event.get("conversation_id")
    if conversation_id:
        metadata["conversation_id"] = conversation_id
    payload = {
        "jsonrpc": "2.0",
        "id": _uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": _uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": ""}],
            },
            "metadata": metadata,
        },
    }
    if envelope_is_contentless_ack(payload):
        log.debug(
            "sac channel: suppressing empty-content auto-ack to %r "
            "(in_reply_to=%s) — sender-side noise filter (operator contract)",
            target,
            msg_id,
        )
        return
    base = listen_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{base}/agents/{target}/message:send", json=payload, headers=headers
        )
        resp.raise_for_status()
