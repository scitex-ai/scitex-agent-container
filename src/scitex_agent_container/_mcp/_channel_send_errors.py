"""The FAIL-LOUD contract for the send-side ``a2a_*`` MCP tools.

Extracted from :mod:`._channel_tools` (which hit the module size budget)
because it is one cohesive responsibility: deciding that a send
demonstrably failed, and rendering that failure so the CALLER CANNOT
MISTAKE IT FOR DELIVERY.

Why this module exists at all
-----------------------------
``a2a_send`` already detected the no-subscriber case and returned a body
carrying ``{"error": ...}``. But the MCP low-level ``Server.call_tool``
decorator wraps whatever a handler returns:

* a plain ``list[TextContent]`` becomes ``CallToolResult(isError=False)``
  — a **SUCCESS**, whatever the text inside happens to say;
* a ``CallToolResult`` is passed through **verbatim**, so a handler that
  wants to fail must say ``isError=True`` itself.

So the old shape was a false green: the tool reported "reached no live
subscriber" inside the body of a result flagged as successful. A caller
that trusted the MCP contract (rather than string-matching the payload)
read it as a delivered message. Agents silently swallowed each other's
messages and nothing in the control plane said so.

The three-state discipline
--------------------------
Delivery has THREE outcomes and they must never be collapsed into two:

* **delivered** — ``delivered_subscriber_count >= 1``. Success.
* **definitively NOT delivered** — an explicit ``0`` from the local
  publish path. The bus fanned out to nobody. This is a hard failure and
  the ONLY one this module raises for. It is EVIDENCE, not an absence of
  evidence.
* **could not determine** — the field is ABSENT (e.g. a cross-host
  forward whose reply does not carry it). Inventing a zero here would be
  a false-positive failure, so an absent count is NEVER read as zero and
  never fails. "I could not check" is rendered as neither success nor
  death.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.types import CallToolResult


class SendError(RuntimeError):
    """A send/push could NOT reach or wake the target agent.

    Raised by the send helper when delivery demonstrably failed:

    * the transport raised (agent down / connection refused),
    * the listen server returned a non-2xx status (delivery error), or
    * the publish reported ``delivered_subscriber_count == 0`` — no live
      inbox subscriber, so the message woke nobody.

    The send-side ``a2a_*`` tools render this via :func:`error_result`
    into an MCP result with ``isError=True`` — never a misleading
    success — and log it.

    ``code`` is the machine-readable failure class (one of the ``ERR_*``
    constants) and ``detail`` carries the structured fields a caller can
    branch on without re-parsing the human message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        target: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.target = target
        self.detail: dict[str, Any] = dict(detail or {})


# Machine-readable failure classes, echoed as ``code`` in the tool's
# error payload so a caller branches on the CLASS of failure instead of
# string-matching a human sentence.
ERR_NO_SUBSCRIBER = "no_live_subscriber"
ERR_UNREACHABLE = "unreachable"
ERR_DELIVERY_ERROR = "delivery_error"
ERR_LOOKUP_FAILED = "lookup_failed"

# What a 0-subscriber send hands the caller instead of a false success.
#
# It deliberately does NOT recommend ``agent_send`` / ``sac agents send``.
# That rail POSTs to the agent's ``/v1/turn`` sidecar port, which —
# measured on the live fleet, 2026-07-14 — only 5 of 47 registered agents
# actually bind. Its own failure path tells callers to use a2a instead
# (see ``cli_pkg._send``), so recommending it here would be a circular
# dead end for most of the fleet.
#
# It also does NOT recommend restarting the target. A 0-subscriber
# reading is a negative signal about ONE transport; it is not an
# observation of agent death. ``--force --fresh`` on it would destroy a
# healthy agent whose only problem is a detached inbox adapter.
NO_SUBSCRIBER_REMEDY: tuple[str, ...] = (
    "NOT LOST: sac listen persists every message to `channel_events` BEFORE "
    "publishing, and the target's inbox stream replays all undelivered rows "
    "on its next connect. Do NOT re-send this message — it would arrive "
    "twice once their adapter reconnects.",
    "To reach them NOW, use a rail that does not depend on their inbox "
    "adapter: a scitex-todo card assigned to them (durable, pull-based), or "
    "escalate to the operator.",
    "Do NOT force-restart the target on this signal. 0 subscribers means "
    "their inbox adapter is not attached; it is NOT evidence that the agent "
    "is dead, and a restart would destroy a healthy session.",
)


def no_subscriber_error(target: str) -> SendError:
    """Build the loud error for an explicit ``delivered_subscriber_count == 0``.

    The message states exactly what was observed (the bus fanned out to
    zero subscribers) and — just as importantly — what was NOT observed
    (that the agent is dead, or that the message is gone).
    """
    return SendError(
        f"NOT DELIVERED — send to {target!r} reached no live subscriber "
        "(delivered_subscriber_count=0). The message woke nobody: "
        f"{target!r} has no inbox adapter attached to the channel bus "
        "(its session is not running `sac mcp channel`, or that adapter's "
        "SSE stream is not connected). Being listed as a running/`active` "
        "peer does NOT mean an agent is subscribed — registered is not "
        "reachable. Check `inbox_subscribers` on a2a_peers before "
        "handing work to a peer.",
        code=ERR_NO_SUBSCRIBER,
        target=target,
        detail={
            "delivered": False,
            "delivered_subscriber_count": 0,
            "durably_queued": True,
            "what_to_do": list(NO_SUBSCRIBER_REMEDY),
        },
    )


def unreachable_error(target: str, exc: Exception) -> SendError:
    """Build the loud error for a transport failure (connection refused …)."""
    return SendError(
        f"NOT DELIVERED — send to {target!r} failed: agent unreachable ({exc})",
        code=ERR_UNREACHABLE,
        target=target,
        detail={"delivered": False, "transport_error": str(exc)},
    )


def delivery_error(target: str, status: Any, body: Any) -> SendError:
    """Build the loud error for a server-side (5xx / bogus status) failure."""
    return SendError(
        f"NOT DELIVERED — send to {target!r} failed: listen returned "
        f"HTTP {status} ({body})",
        code=ERR_DELIVERY_ERROR,
        target=target,
        detail={"delivered": False, "http_status": status, "http_body": body},
    )


def _error_payload(exc: SendError) -> dict[str, Any]:
    """Project a :class:`SendError` onto the tool's JSON error body."""
    payload: dict[str, Any] = {
        "error": str(exc),
        "code": exc.code,
        "target": exc.target,
        "delivered": False,
    }
    payload.update(exc.detail)
    return payload


def error_result(exc: SendError) -> "CallToolResult":
    """Render a :class:`SendError` as an MCP result the caller CANNOT read
    as success.

    Returns a ``CallToolResult`` with ``isError=True``. The MCP low-level
    server passes a ``CallToolResult`` through verbatim (unlike a bare
    ``list[TextContent]``, which it stamps ``isError=False``), so this is
    the seam where "the message was not delivered" actually becomes a
    tool-level failure the calling model sees as one.

    The structured detail (target, failure ``code``, subscriber count,
    and what to do instead) is preserved in the payload — failing loudly
    must not cost the caller the information it needs to recover.
    """
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(_error_payload(exc), indent=2))
        ],
        isError=True,
    )


def lookup_error_result(message: str, *, target: str = "") -> "CallToolResult":
    """Render a non-send failure (unknown msg_id, unknown sender, unknown
    tool) as an ``isError=True`` result.

    These never delivered anything either, so they must not come back as
    a success body — same rule, different cause.
    """
    return error_result(
        SendError(message, code=ERR_LOOKUP_FAILED, target=target, detail={})
    )


__all__ = [
    "ERR_DELIVERY_ERROR",
    "ERR_LOOKUP_FAILED",
    "ERR_NO_SUBSCRIBER",
    "ERR_UNREACHABLE",
    "NO_SUBSCRIBER_REMEDY",
    "SendError",
    "delivery_error",
    "error_result",
    "lookup_error_result",
    "no_subscriber_error",
    "unreachable_error",
]
