"""Stop-hook completion push for the claude-session runner.

When a turn finishes, the runner's Stop hook PUSHes a completion report
back to whoever dispatched the turn — the *requester* threaded onto the
``TurnEnvelope`` (``from_agent`` + ``dispatch_id``). This realises the
push-feedback north star: a peer that drove a turn into this agent hears
about completion without polling, and it generalises to ANY peer — the
lead is not special-cased.

The push reuses the existing ``sac listen`` ``message:send`` channel
(the same path ``a2a_send`` uses): POST a JSON-RPC ``SendMessage`` to
``{SAC_LISTEN_BASE_URL}/agents/<requester>/message:send`` with the
completion payload as the message text and sac-extension metadata under
``params.metadata``. That path already publishes to the requester's
inbox Broker, persists to ``channel_events``, and — crucially — FAILS
LOUD when ``delivered_subscriber_count == 0`` (no live subscriber). We
honour that: a push that reaches nobody raises :class:`CompletionPushError`
so the failure is visible, never a silent drop.

The completion payload the requester receives is the JSON object
``{agent, dispatch_id, status, summary}``:

* ``agent``       — this agent's own name (who is reporting).
* ``dispatch_id`` — the sender-minted ledger id that resolves *which
  task from which agent* (operator's explicit requirement). ``None``
  when the dispatch was not minted through the ledger.
* ``status``      — honest turn outcome: ``"success"`` (the turn drained
  cleanly), ``"failure"`` (the SDK raised), or ``"unknown"`` (no clear
  signal). NEVER hardcoded — see :func:`build_completion_report`.
* ``summary``     — a bounded slice of the assistant's reply text so the
  requester can read the gist without fetching the full transcript.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)

__all__ = [
    "CompletionPushError",
    "STATUS_FAILURE",
    "STATUS_SUCCESS",
    "STATUS_UNKNOWN",
    "build_completion_report",
    "push_completion",
]

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_UNKNOWN = "unknown"

# Cap the summary so a runaway assistant turn cannot bloat the push body
# (and the requester's inbox row). The full text is always on disk in the
# agent's session.jsonl; the push is a notification, not the transcript.
_SUMMARY_CAP = 2000


class CompletionPushError(RuntimeError):
    """A completion push could NOT reach the requester.

    Raised when the ``message:send`` POST fails to deliver: transport
    error, non-2xx status, or ``delivered_subscriber_count == 0`` (the
    requester has no live inbox subscriber). Mirrors the send-side
    :class:`scitex_agent_container._mcp._channel_tools.SendError` contract
    — fail loud, never a misleading success.
    """


def build_completion_report(
    *,
    agent: str,
    dispatch_id: Optional[str],
    status: str,
    summary_text: str,
) -> dict[str, Any]:
    """Build the ``{agent, dispatch_id, status, summary}`` completion payload.

    ``status`` is validated against the honest vocabulary — an unknown
    value is coerced to :data:`STATUS_UNKNOWN` rather than passed through,
    so a caller bug can never smuggle a fabricated ``"success"``-looking
    string the requester would trust. ``summary_text`` is bounded to
    :data:`_SUMMARY_CAP` chars (the full reply lives in session.jsonl).
    """
    if status not in (STATUS_SUCCESS, STATUS_FAILURE, STATUS_UNKNOWN):
        log.warning(
            "completion report built with non-canonical status %r; coercing to %r",
            status,
            STATUS_UNKNOWN,
        )
        status = STATUS_UNKNOWN
    summary = summary_text or ""
    if len(summary) > _SUMMARY_CAP:
        summary = summary[:_SUMMARY_CAP] + "…"
    return {
        "agent": agent,
        "dispatch_id": dispatch_id,
        "status": status,
        "summary": summary,
    }


def _wrap_completion_send(
    *,
    agent: str,
    report: dict[str, Any],
    dispatch_id: Optional[str],
) -> dict[str, Any]:
    """Wrap the completion report as a JSON-RPC ``SendMessage`` body.

    The report JSON is the message text; sac-extension fields ride under
    ``params.metadata`` (the A2A v1 convention the ``message:send`` route
    reads). ``from_agent`` is THIS agent (who is reporting); ``kind`` marks
    the envelope as a completion so a requester can distinguish it from a
    normal inbound message; ``dispatch_id`` lets the requester correlate.
    """
    metadata: dict[str, Any] = {"from_agent": agent, "kind": "completion"}
    if dispatch_id:
        metadata["dispatch_id"] = dispatch_id
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": json.dumps(report)}],
            },
            "metadata": metadata,
        },
    }


async def push_completion(
    *,
    agent: str,
    requester: str,
    report: dict[str, Any],
    listen_url: str,
    bearer: Optional[str],
    dispatch_id: Optional[str] = None,
) -> dict[str, Any]:
    """POST the completion ``report`` to ``requester``'s inbox; FAIL LOUD.

    Reuses the ``sac listen`` ``message:send`` channel (same path
    ``a2a_send`` uses). Raises :class:`CompletionPushError` — never a
    misleading success — when:

    * the transport raises (requester host down / connection refused),
    * the HTTP status is non-2xx (delivery / ACL error), or
    * the publish reports ``delivered_subscriber_count == 0`` — the
      requester has no live inbox subscriber, so the report woke nobody.

    Returns the parsed ``{status, body}`` dict on success. ``httpx`` is
    imported lazily so the runner stays importable without the HTTP deps
    (the colocated channel adapter guards its own deps the same way).
    """
    import httpx

    payload = _wrap_completion_send(agent=agent, report=report, dispatch_id=dispatch_id)
    base = listen_url.rstrip("/")
    url = f"{base}/agents/{requester}/message:send"
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("completion push to %r failed (transport): %s", requester, exc)
        raise CompletionPushError(
            f"completion push to {requester!r} failed: unreachable ({exc})"
        ) from exc

    status_code = resp.status_code
    try:
        body: Any = resp.json()
    except Exception:  # stx-allow: fallback (reason: non-JSON body tolerated; surfaced verbatim in the loud error)
        body = resp.text
    if status_code < 200 or status_code >= 300:
        log.warning(
            "completion push to %r returned HTTP %s: %s",
            requester,
            status_code,
            body,
        )
        raise CompletionPushError(
            f"completion push to {requester!r} failed: "
            f"listen returned HTTP {status_code} ({body})"
        )
    if isinstance(body, dict):
        delivered = body.get("delivered_subscriber_count")
        if isinstance(delivered, int) and delivered == 0:
            log.warning(
                "completion push to %r reached no subscriber "
                "(delivered_subscriber_count=0)",
                requester,
            )
            raise CompletionPushError(
                f"completion push to {requester!r} reached no live "
                "subscriber (delivered_subscriber_count=0): the requester "
                "is not subscribed to its inbox (down, not started, or its "
                "channel adapter is not connected) — the report woke nobody."
            )
    return {"status": status_code, "body": body}
