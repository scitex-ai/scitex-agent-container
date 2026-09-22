"""Sender-side verification and absorption of model-authored A2A feedback."""

from __future__ import annotations

import scitex_logging as slogging
from typing import Any

log = slogging.getLogger(__name__)

AGENTIC_ACK_KIND = "agentic_ack"
PROGRESS_KIND = "a2a_progress"


def is_agentic_feedback_event(event: dict[str, Any]) -> bool:
    """Return whether an event is semantic ACK/progress, never mechanical ACK."""
    return event.get("kind") in (AGENTIC_ACK_KIND, PROGRESS_KIND)


def absorb_agentic_feedback(
    event: dict[str, Any], *, agent: str | None = None
) -> bool:
    """Verify exact nonce + expected peer and update sender-owned state.

    Every malformed, stale, wrong-nonce, or wrong-peer envelope is refused.
    Store failures are logged and contained so feedback observability cannot
    kill the long-lived SSE consumer.
    """
    kind = event.get("kind")
    if kind not in (AGENTIC_ACK_KIND, PROGRESS_KIND):
        return False
    extra = event.get("extra")
    from_agent = event.get("from_agent")
    if not isinstance(extra, dict) or not isinstance(from_agent, str) or not from_agent:
        return False
    dispatch_id = extra.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return False
    if kind == AGENTIC_ACK_KIND:
        understood = extra.get("understood")
        owner = extra.get("owner")
        next_checkpoint = extra.get("next_checkpoint")
        if not all(isinstance(value, str) for value in (understood, owner, next_checkpoint)):
            return False
    else:
        status = extra.get("status")
        summary = extra.get("summary")
        if not isinstance(status, str) or not isinstance(summary, str):
            return False
    try:
        from .._state.dispatch_feedback import record_agentic_ack, record_progress

        if kind == AGENTIC_ACK_KIND:
            saved = record_agentic_ack(
                dispatch_id,
                from_agent=from_agent,
                understood=extra.get("understood"),
                owner=extra.get("owner"),
                next_checkpoint=extra.get("next_checkpoint"),
                agent=agent,
            )
        else:
            saved = record_progress(
                dispatch_id,
                from_agent=from_agent,
                status=extra.get("status"),
                summary=extra.get("summary"),
                blocker=extra.get("blocker"),
                agent=agent,
            )
    except (TypeError, ValueError) as exc:
        log.warning("sac channel: refusing malformed %s feedback: %s", kind, exc)
        return False
    except Exception as exc:  # stx-allow: fallback (reason: feedback persistence must not kill the SSE consumer; failure is logged to the MCP process stderr)
        log.warning("sac channel: persisting %s feedback failed: %s", kind, exc)
        return False
    if saved is None:
        log.warning(
            "sac channel: refused %s for stale/wrong nonce %r from %r",
            kind,
            dispatch_id,
            from_agent,
        )
        return False
    return True


__all__ = [
    "AGENTIC_ACK_KIND",
    "PROGRESS_KIND",
    "absorb_agentic_feedback",
    "is_agentic_feedback_event",
]
