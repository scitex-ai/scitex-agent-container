"""Explicit model-authored ACK/progress MCP tool implementations."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from ._channel_send_errors import SendError, error_result, lookup_error_result

SUMMARY_LIMIT = 500
OWNER_LIMIT = 120
_PROGRESS_STATUSES = {"in_progress", "completed", "failed"}


def _text(arguments: dict[str, Any], key: str, limit: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{key} exceeds {limit} characters")
    return value


async def send_agentic_ack(
    arguments: dict[str, Any],
    orig: dict[str, Any],
    *,
    wrap: Callable[..., dict[str, Any]],
    send: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]],
):
    """Send one intentional ACK and mark this inbound event locally acknowledged."""
    from mcp.types import TextContent

    target = orig.get("from_agent", "")
    if not target:
        return lookup_error_result("original sender unknown")
    try:
        feedback = {
            "dispatch_id": _text(arguments, "dispatch_id", 256),
            "understood": _text(arguments, "understood", SUMMARY_LIMIT),
            "owner": _text(arguments, "owner", OWNER_LIMIT),
            "next_checkpoint": _text(arguments, "next_checkpoint", SUMMARY_LIMIT),
        }
    except ValueError as exc:
        return lookup_error_result(str(exc))
    payload = wrap(
        json.dumps(feedback, sort_keys=True),
        conversation_id=orig.get("conversation_id"),
        in_reply_to=orig.get("msg_id"),
        ack=True,
        kind="agentic_ack",
        extra=feedback,
    )
    try:
        res = await send(target, f"/agents/{target}/message:send", payload)
    except SendError as exc:
        return error_result(exc)
    orig["_agentic_ack"] = dict(feedback)
    res["agentic_ack"] = feedback
    return [TextContent(type="text", text=json.dumps(res))]


async def send_progress(
    arguments: dict[str, Any],
    orig: dict[str, Any],
    *,
    wrap: Callable[..., dict[str, Any]],
    send: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]],
):
    """Send typed progress only after this process issued the nonce-bound ACK."""
    from mcp.types import TextContent

    target = orig.get("from_agent", "")
    if not target:
        return lookup_error_result("original sender unknown")
    dispatch_id = arguments.get("dispatch_id")
    local_ack = orig.get("_agentic_ack")
    if not isinstance(local_ack, dict) or local_ack.get("dispatch_id") != dispatch_id:
        return lookup_error_result(
            "a2a_progress requires a successful a2a_agentic_ack for this dispatch_id"
        )
    status = arguments.get("status")
    if status not in _PROGRESS_STATUSES:
        return lookup_error_result(
            "status must be one of: in_progress, completed, failed"
        )
    try:
        feedback = {
            "dispatch_id": _text(arguments, "dispatch_id", 256),
            "status": status,
            "summary": _text(arguments, "summary", SUMMARY_LIMIT),
        }
        blocker = arguments.get("blocker")
        if blocker is not None:
            feedback["blocker"] = _text(arguments, "blocker", SUMMARY_LIMIT)
    except ValueError as exc:
        return lookup_error_result(str(exc))
    payload = wrap(
        json.dumps(feedback, sort_keys=True),
        conversation_id=orig.get("conversation_id"),
        in_reply_to=orig.get("msg_id"),
        ack=True,
        kind="a2a_progress",
        extra=feedback,
    )
    try:
        res = await send(target, f"/agents/{target}/message:send", payload)
    except SendError as exc:
        return error_result(exc)
    res["progress"] = feedback
    return [TextContent(type="text", text=json.dumps(res))]


def read_dispatch_status(arguments: dict[str, Any], *, agent: str):
    """Render the durable sender-side status/timeout query as an MCP result."""
    from mcp.types import TextContent

    from .._state.dispatch_feedback import dispatch_status

    dispatch_id = arguments["dispatch_id"]
    try:
        state = dispatch_status(
            dispatch_id,
            agent=agent,
            agentic_ack_timeout_s=float(
                arguments.get("agentic_ack_timeout_s", 120.0)
            ),
        )
    except (TypeError, ValueError) as exc:
        return lookup_error_result(str(exc))
    except Exception as exc:  # stx-allow: fallback (reason: surface store read failure to MCP caller rather than crashing channel server)
        return lookup_error_result(f"dispatch status unavailable: {exc}")
    if state is None:
        return lookup_error_result(f"unknown dispatch_id {dispatch_id}")
    return [TextContent(type="text", text=json.dumps(state))]


__all__ = ["read_dispatch_status", "send_agentic_ack", "send_progress"]
