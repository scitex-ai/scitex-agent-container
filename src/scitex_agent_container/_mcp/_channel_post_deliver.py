"""sac channel adapter — post-delivery receipts dispatcher.

Extracted from :mod:`scitex_agent_container._mcp.channel` to keep the
receive-side adapter under the module line-size budget after the
structural reaction-ack joined the contentless auto-ack as a second
post-delivery side-effect.

Both receipts share the same shape on the caller's side:

* They run AFTER successful delivery (notification push or wake-on-turn).
* They are best-effort — a failed receipt MUST NOT block delivery or
  kill the long-lived SSE consumer. Every failure logs loudly (warning)
  but never re-raises.
* They share the per-sender sliding-window rate cap so any loop or
  storm self-terminates with the same budget.

This module owns the single entry point
:func:`run_post_deliver_receipts` so :mod:`channel` has one call site
instead of two large gated blocks (and re-passes the line ceiling).
"""

from __future__ import annotations

import logging
from typing import Any

from ._channel_auto_ack import (
    _auto_ack_enabled,
    _auto_ack_rate_allow,
    _post_auto_ack,
    _should_auto_ack,
)
from ._channel_reaction_ack import (
    post_reaction_ack,
    reaction_ack_enabled,
    should_emit_reaction_ack,
)

log = logging.getLogger(__name__)

__all__ = ["run_post_deliver_receipts"]


async def run_post_deliver_receipts(
    event: dict[str, Any],
    *,
    agent_name: str | None,
    listen_url: str | None,
    bearer: str | None,
) -> None:
    """Run every post-delivery receipt side-effect for ``event``.

    Order:

    1. **Stage-2 auto-ack** (contentless ``ack=True`` envelope) — the
       legacy receipt. The sender-side noise filter typically drops it
       before the wire so this is a no-op on the wire today, but the
       loop-guard / rate-cap / env gate are still honoured for
       symmetry and forward-compat.
    2. **Structural reaction-ack** (``kind="reaction"`` envelope with
       a non-empty 👀 marker) — the operator's "comm-miss detectable"
       signal (lead a2a 1781e82a, 2026-06-14). Unlike the auto-ack,
       this carries a visible marker so the empty-ack filter does NOT
       suppress it, and threads the original ``dispatch_id`` so the
       sender's adapter marks the matching dispatch row REACTED.

    Both gated calls share the per-sender sliding-window rate cap
    (``_auto_ack_rate_allow``) — a runaway sender that overruns the
    budget is denied BOTH receipts at once, so a structural-ack storm
    cannot mask an auto-ack loop or vice versa.

    Caller-side preconditions: ``agent_name`` and ``listen_url`` must
    be set for either receipt to fire (the channel adapter only runs
    receipts when it knows where to post them). Both are passed
    through verbatim — no validation here; the env-gate / loop-guard
    short-circuit on the missing-config case.
    """
    if agent_name is None or listen_url is None:
        return
    if not _auto_ack_enabled() and not reaction_ack_enabled():
        return

    sender = event.get("from_agent")

    # Stage-2 contentless auto-ack (legacy).
    if (
        _auto_ack_enabled()
        and _should_auto_ack(event)
        and isinstance(sender, str)
        and _auto_ack_rate_allow(sender)
    ):
        try:
            await _post_auto_ack(
                event,
                agent_name=agent_name,
                listen_url=listen_url,
                bearer=bearer,
            )
        except Exception as exc:  # stx-allow: fallback (reason: best-effort auto-ack; a failed receipt must not block injection or kill the SSE consumer — logged loudly, never silent)
            log.warning(
                "sac channel: auto-ack to %r failed: %s",
                sender,
                exc,
            )

    # Structural reaction-ack — the comm-miss-detectable signal.
    if (
        reaction_ack_enabled()
        and should_emit_reaction_ack(event)
        and isinstance(sender, str)
        and _auto_ack_rate_allow(sender)
    ):
        try:
            await post_reaction_ack(
                event,
                agent_name=agent_name,
                listen_url=listen_url,
                bearer=bearer,
            )
        except Exception as exc:  # stx-allow: fallback (reason: best-effort reaction-ack; a failed receipt must not block injection or kill the SSE consumer — logged loudly, never silent)
            log.warning(
                "sac channel: reaction-ack to %r failed: %s",
                sender,
                exc,
            )
