"""Sender-side noise filter for contentless delivery acks.

The sac channel adapter emits two-stage receipts: stage 1 is the publish
response's ``delivered_subscriber_count``; stage 2 is an automatic
``a2a_ack`` posted back to the sender on injection. The stage-2 ack
carries an empty body and ``metadata.ack=True``.

A receive-side loop-guard already exists (``channel._should_auto_ack``
refuses to ack an ack), but the operator's stronger contract is to drop
empty-content acks at the **sender** *before* they leave the outbound
queue:

* sender-side filtering avoids the symmetric "did we send?" / "did they
  receive?" doubt that receiver-side filtering would create — the wire
  carries only messages with semantic content;
* it short-circuits the loop one hop earlier, before the network packet
  is ever built or sent.

The marker convention matches the rest of the channel code:
``params.metadata.ack`` truthy AND ``params.message.parts[0].text``
empty/whitespace. An ack carrying actual content (non-empty text) is
NOT a contentless delivery confirmation — it is a normal message and
must pass through untouched. An empty-content message WITHOUT the
``ack`` marker is also untouched (the empty payload is intentional —
e.g. a wake ping or an external protocol). Only the join of both
conditions is suppressed.

This module is intentionally tiny (one pure predicate) so both
``_mcp.channel`` (auto-ack path) and ``_mcp._channel_tools`` (the
explicit ``a2a_ack`` tool) can import it without a circular dependency
or growing either of those modules past their size budget.
"""

from __future__ import annotations

from typing import Any

__all__ = ["envelope_is_contentless_ack"]


def envelope_is_contentless_ack(envelope: dict[str, Any]) -> bool:
    """Return True iff ``envelope`` is an empty-content delivery ack.

    ``envelope`` is the JSON-RPC ``message/send`` (or ``SendMessage``)
    body — ``{"jsonrpc": ..., "method": ..., "params": {"message": ...,
    "metadata": ...}}``. The check is structural (looks at
    ``params.metadata.ack`` and ``params.message.parts[0].text``); any
    structurally-malformed envelope returns False (caller decides what
    to do — this helper is only for the empty-ack case).

    Examples (all from the live ``a2a_ack`` / ``_post_auto_ack`` paths):

    * ``metadata.ack=True`` + ``parts[0].text=""``           → True (drop)
    * ``metadata.ack=True`` + ``parts[0].text="   "``        → True (drop)
    * ``metadata.ack=True`` + ``parts=[]``                   → True (drop)
    * ``metadata.ack=True`` + ``parts[0].text="got it"``     → False (keep)
    * ``metadata.ack=False`` (or absent) + empty text        → False (keep)
    """
    params = envelope.get("params") if isinstance(envelope, dict) else None
    if not isinstance(params, dict):
        return False
    metadata = params.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("ack"):
        return False
    message = params.get("message")
    if not isinstance(message, dict):
        return False
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        # No parts at all is morally an empty body — drop alongside "".
        return True
    first = parts[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        return False
    return text.strip() == ""
