"""sac MCP **channel** send-side ``a2a_*`` tool surface.

Extracted from :mod:`scitex_agent_container._mcp.channel` (which grew past
the module size budget). Hosts the send-side tools the agent calls
explicitly — ``a2a_send``, ``a2a_reply``, ``a2a_ack``, ``a2a_peers``,
``a2a_inbox`` — while the receive-side adapter (SSE consumer, session
push, the automatic ``a2a_ack`` side-effect) stays in ``channel``.

The two halves share the per-process inbox ring buffer ``_recent``,
which lives in ``channel`` (the receive side fills it; these tools read
it). Importing it from ``channel`` keeps a single source of truth and
avoids a circular import (``channel`` imports *this* module, not the
other way around at module load).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .._listen._inbox_fault import FAULT_NOT_RUNNING
from ._channel_send_errors import (
    SendError,
    delivery_error,
    error_result,
    lookup_error_result,
    no_subscriber_error,
    not_running_error,
    unknown_target_error,
    unreachable_error,
)
from ._channel_target_lookup import (
    fault_of,
    is_registered,
    names_of,
    rows_from_agents_body,
)
from .channel import _recent

log = logging.getLogger(__name__)


def register_tools(
    server, *, agent_name: str, listen_url: str, bearer: str | None
) -> None:
    """Wire the a2a_* tools onto the channel server.

    All tools speak HTTP to local `sac listen`; receivers' inbox state
    (for reply/ack lookups) lives in the shared ``_recent`` ring.
    Tools-as-contract: each tool sets `from_agent`, `ts`, `msg_id`,
    `conversation_id` correctly so the calling agent can't get it wrong.
    """
    import uuid as _uuid

    from mcp.types import CallToolResult, TextContent, Tool

    from .._state.dispatch_ledger import (
        STATUS_DELIVERED,
        STATUS_FAILED,
        new_dispatch_id,
        record_dispatch,
        update_dispatch_status,
    )

    def _ledger_record(
        *, to_agent: str, content: str, conversation_id: str | None
    ) -> str:
        """Mint + record an outbound dispatch row; return its dispatch_id.

        Ledger writes are observability — a state.db hiccup must not break
        the actual a2a send, so failures log loudly (never silent) and the
        send proceeds with a freshly-minted id that simply has no row.
        """
        did = new_dispatch_id()
        try:
            record_dispatch(
                from_agent=agent_name,
                to_agent=to_agent,
                text=content,
                conversation_id=conversation_id,
                dispatch_id=did,
            )
        except Exception as exc:  # stx-allow: fallback (reason: ledger is observability; a DB write failure must not break the a2a send — logged loudly, never silent)
            log.warning("dispatch-ledger record (a2a_send) failed: %s", exc)
        return did

    def _ledger_update(dispatch_id: str, status: str) -> None:
        try:
            update_dispatch_status(dispatch_id, status)
        except Exception as exc:  # stx-allow: fallback (reason: ledger is observability; a status-update failure must not break the a2a send — logged loudly, never silent)
            log.warning("dispatch-ledger status update (a2a_send) failed: %s", exc)

    base = listen_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{base}{path}", json=payload, headers=headers)
            try:
                return {"status": resp.status_code, "body": resp.json()}
            except Exception:  # stx-allow: fallback (reason: non-JSON body tolerated)
                return {"status": resp.status_code, "body": resp.text}

    async def _get(path: str) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}{path}", headers=headers)
            try:
                return {"status": resp.status_code, "body": resp.json()}
            except Exception:  # stx-allow: fallback (reason: non-JSON body tolerated)
                return {"status": resp.status_code, "body": resp.text}

    async def _send_or_raise(
        target: str, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST a send/push and FAIL LOUDLY when it cannot reach/wake (WI-2).

        Returns the parsed ``{status, body}`` on success. Raises
        :class:`SendError` — which the caller renders as an MCP result
        with ``isError=True``, never a misleading success — when:

        * the transport raises (target agent down / connection refused),
        * the HTTP status is 5xx (server / infra delivery error), or
        * ``body.delivered_subscriber_count == 0`` — no live inbox
          subscriber, so the push woke nobody.

        A **4xx** is passed through verbatim (returned, not raised). A 4xx —
        notably the 403 ACL deny carrying ``body.reason`` — is a deliberate,
        structured policy/client response, already loud and actionable for
        the agent ("denial is the policy working"). Reshaping it into an
        opaque error string would lose the structured ``reason`` and is not
        the silent-drop/misleading-success the fail-loud rule targets.

        KNOWN GAP (deliberate, not an oversight): a 4xx therefore still comes
        back with ``isError=False``, even though it delivered nothing. It is
        far less dangerous than the 0-subscriber case this fail-loud path was
        written for — a 403 body is self-evidently a failure, whereas a
        no-subscriber publish returned an HTTP **200** and read as success by
        every conventional measure. Flipping 4xx to ``isError=True`` (keeping
        the body verbatim, so the original "don't lose the structured reason"
        objection would not apply) is a one-line change, but it alters the ACL
        contract and is pinned by ``tests/smoke/test_node_comms_e2e_mcp.py``,
        so it belongs in its own PR rather than riding along with this one.

        ``delivered_subscriber_count`` ABSENT is NOT treated as zero: some
        responses (cross-host forwards) don't carry it, and inventing a
        zero would be a false-positive failure. Only an explicit ``0``
        from the local publish path is the no-subscriber signal.

        **Sender-side empty-ack noise filter:** before the outbound POST,
        the assembled envelope is checked against
        :func:`._channel_ack_filter.envelope_is_contentless_ack`. A
        contentless ack (empty body + ``metadata.ack=True``) is dropped
        silently with a debug log and a synthetic
        ``{"status": 200, "body": {"suppressed": "empty_ack", ...}}``
        result is returned — the operator's contract is to keep empty
        delivery confirmations off the wire (sender-side, never receiver-
        side, to avoid the symmetric "did we send? / did they receive?"
        doubt). Non-empty acks and empty-content non-ack messages pass
        through untouched (the join of empty body AND ``ack=True`` is
        the only suppressed shape).
        """
        import httpx

        from ._channel_ack_filter import envelope_is_contentless_ack

        if envelope_is_contentless_ack(payload):
            log.debug(
                "sac a2a: suppressing empty-content ack to %r at %s "
                "— sender-side noise filter (operator contract)",
                target,
                path,
            )
            return {
                "status": 200,
                "body": {
                    "suppressed": "empty_ack",
                    "reason": (
                        "empty-content delivery ack dropped at sender "
                        "(operator contract: no contentless acks on the wire)"
                    ),
                },
            }

        try:
            res = await _post(path, payload)
        except httpx.HTTPError as exc:
            log.warning("sac a2a: send to %r failed (transport): %s", target, exc)
            raise unreachable_error(target, exc) from exc

        status = res.get("status")
        # 5xx (and any non-int / sub-200) = server/infra delivery failure.
        # 4xx passes through (deliberate policy/client response — see above).
        if isinstance(status, int) and status >= 500:
            body = res.get("body")
            log.warning(
                "sac a2a: send to %r returned HTTP %s: %s", target, status, body
            )
            raise delivery_error(target, status, body)
        if not isinstance(status, int) or status < 200:
            body = res.get("body")
            log.warning(
                "sac a2a: send to %r returned unexpected status %r: %s",
                target,
                status,
                body,
            )
            raise delivery_error(target, status, body)

        body = res.get("body")
        if isinstance(body, dict):
            delivered = body.get("delivered_subscriber_count")
            if isinstance(delivered, int) and delivered == 0:
                log.warning(
                    "sac a2a: send to %r reached no subscriber "
                    "(delivered_subscriber_count=0) — NOT DELIVERED",
                    target,
                )
                # A 0-subscriber count has THREE causes needing DIFFERENT
                # actions, and the count alone tells them apart in none:
                #
                #   registered agent, adapter detached -> WAIT (it replays)
                #   registered agent, NOT RUNNING      -> DO NOT WAIT; there
                #                                         is no session left
                #                                         to reconnect
                #   name never registered (a typo)     -> FIX THE NAME
                #
                # The third was split out after the `sac-04` incident; the
                # second after 2026-08-12, when 9 of 15 registered rows on this
                # host were STOPPED agents whose senders were being told, in
                # bold, to wait for a reconnect with no process to happen in.
                # See ``._channel_target_lookup``.
                #
                # Only ask the registry on this failure path, so the happy
                # path pays nothing for the distinction.
                rows = await _registered_rows()
                if not is_registered(target, rows):
                    raise unknown_target_error(target, names_of(rows))
                if fault_of(target, rows) == FAULT_NOT_RUNNING:
                    raise not_running_error(target)
                raise no_subscriber_error(target)
        return res

    async def _registered_rows() -> list[dict[str, Any]]:
        """Registry rows, from the same ``/agents`` view a2a_peers uses.

        Rows rather than names, so this ONE fetch answers both questions the
        failure path asks — is the name real, and is the agent running.
        ``[]`` when the registry cannot be read; the caller must treat that as
        "could not determine", never as "no agents exist".
        """
        try:
            res = await _get("/agents")
        except Exception:  # noqa: BLE001 - registry unreadable is not a send failure
            return []
        return rows_from_agents_body(res.get("body"))

    async def _registered_names() -> list[str]:
        """Registered agent names — for callers outside the send failure path."""
        return names_of(await _registered_rows())

    def _find(msg_id: str) -> dict[str, Any] | None:
        for ev in reversed(_recent):
            if ev.get("msg_id") == msg_id:
                return ev
        return None

    def _wrap_message_send(content: str, **extra: Any) -> dict[str, Any]:
        # sac-extension fields (from_agent, conversation_id, ...) live
        # under ``params.metadata`` per A2A v1 — the SDK's strict proto
        # validator rejects unknown top-level params fields, so we
        # CANNOT splat them at the params root.
        metadata: dict[str, Any] = {"from_agent": agent_name}
        # Operator #16: every outbound a2a message carries the sender's
        # account + live quota (used_pct_5h, used_pct_7d, token_ttl_hours)
        # as STRUCTURED metadata. Peers (especially the lead) consume the
        # fields programmatically for back-pressure decisions — "this peer
        # is about to hit the 5h cap, route around it" — without parsing
        # free-form text. ``build_a2a_metadata()`` returns ``{}`` when no
        # quota entry is resolvable (no env, no cache file, no matching
        # account), so unpinned agents emit clean payloads with no fake
        # metadata. Reads at SEND time → fresh quota every message.
        from .._account.quota_cache import build_a2a_metadata

        metadata.update(build_a2a_metadata())
        metadata.update({k: v for k, v in extra.items() if v is not None})
        params: dict[str, Any] = {
            "message": {
                "message_id": _uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": content}],
            },
            "metadata": metadata,
        }
        return {
            "jsonrpc": "2.0",
            "id": _uuid.uuid4().hex,
            # v1 gRPC-style method name; sac's `_publish_channel_event`
            # accepts both `SendMessage` and legacy `message/send`.
            "method": "SendMessage",
            "params": params,
        }

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="a2a_send",
                description=(
                    "Send a message to another agent on this sac listen. "
                    "Sets from_agent automatically; mints conversation_id "
                    "when omitted. FAILS (isError) when the message reached "
                    "no live inbox subscriber — a peer listed as running is "
                    "NOT necessarily subscribed. Check `inbox_subscribers` "
                    "via a2a_peers before handing work to a peer."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["target", "content"],
                    "properties": {
                        "target": {"type": "string"},
                        "content": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["low", "normal", "high"],
                        },
                        "requires_reply": {"type": "boolean"},
                    },
                },
            ),
            Tool(
                name="a2a_reply",
                description=(
                    "Reply to a received message. Looks up the original "
                    "sender by msg_id; carries the same conversation_id."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["in_reply_to", "content"],
                    "properties": {
                        "in_reply_to": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="a2a_ack",
                description=(
                    "Acknowledge a received message without content. "
                    "Cheap 'got it' to a sender that set requires_reply=true."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["msg_id"],
                    "properties": {"msg_id": {"type": "string"}},
                },
            ),
            Tool(
                name="a2a_peers",
                description=(
                    "List agents known to this sac listen. REGISTERED IS NOT "
                    "REACHABLE: a row can show a pid, a port and group "
                    "'active' while having NO inbox subscriber, in which case "
                    "a2a_send to it delivers nothing. Each row carries "
                    "`inbox_subscribers` (live subscriber count) and "
                    "`inbox_reachable` ('reachable' / 'unreachable' / "
                    "'unknown' when it lives on another host and this listen "
                    "cannot observe it). Only 'reachable' means a message "
                    "will actually wake them."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="a2a_inbox",
                description=(
                    "Return up to `limit` most recent received messages "
                    "from this agent's inbox buffer (default 20)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> "list[TextContent] | CallToolResult":
        """Dispatch one ``a2a_*`` call.

        Return-type contract (the fix for the swallowed-message false
        green): a SUCCESS returns ``list[TextContent]``, which the MCP
        low-level server stamps ``isError=False``. A FAILURE returns a
        ``CallToolResult`` carrying ``isError=True``, which the server
        passes through verbatim. A caller therefore cannot mistake a
        message that reached nobody for a delivered one — previously
        BOTH shapes came back as ``isError=False`` and the failure was
        just a field in the body that a caller could (and did) miss.
        """
        if name == "a2a_send":
            target = arguments["target"]
            content = arguments["content"]
            conversation_id = arguments.get("conversation_id") or _uuid.uuid4().hex
            dispatch_id = _ledger_record(
                to_agent=target,
                content=content,
                conversation_id=conversation_id,
            )
            payload = _wrap_message_send(
                content,
                conversation_id=conversation_id,
                dispatch_id=dispatch_id,
                priority=arguments.get("priority"),
                requires_reply=arguments.get("requires_reply"),
            )
            try:
                res = await _send_or_raise(
                    target, f"/agents/{target}/message:send", payload
                )
            except SendError as exc:
                _ledger_update(dispatch_id, STATUS_FAILED)
                return error_result(exc)
            _ledger_update(dispatch_id, STATUS_DELIVERED)
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_reply":
            mid = arguments["in_reply_to"]
            orig = _find(mid)
            if orig is None:
                return lookup_error_result(f"unknown msg_id {mid} (inbox window)")
            target = orig.get("from_agent", "")
            if not target:
                return lookup_error_result("original sender unknown")
            payload = _wrap_message_send(
                arguments["content"],
                conversation_id=orig.get("conversation_id"),
                in_reply_to=mid,
            )
            try:
                res = await _send_or_raise(
                    target, f"/agents/{target}/message:send", payload
                )
            except SendError as exc:
                return error_result(exc)
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_ack":
            mid = arguments["msg_id"]
            orig = _find(mid)
            if orig is None:
                return lookup_error_result(f"unknown msg_id {mid} (inbox window)")
            target = orig.get("from_agent", "")
            if not target:
                return lookup_error_result("original sender unknown")
            payload = _wrap_message_send(
                "",
                conversation_id=orig.get("conversation_id"),
                in_reply_to=mid,
                ack=True,
            )
            try:
                res = await _send_or_raise(
                    target, f"/agents/{target}/message:send", payload
                )
            except SendError as exc:
                return error_result(exc)
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_peers":
            # No trailing slash: sac listen registers `/agents` and a GET to
            # `/agents/` 307-redirects, which httpx does not follow by default
            # (a2a_peers then returns a bare 307 with empty body).
            #
            # Each row carries `inbox_subscribers` + `inbox_reachable` (see
            # ``_listen/_reachability.py``) so a caller can tell REGISTERED
            # from REACHABLE. Reading only pid/groups is what let a deaf peer
            # look alive-and-able.
            res = await _get("/agents")
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_inbox":
            limit = int(arguments.get("limit") or 20)
            items = list(_recent)[-limit:]
            return [
                TextContent(
                    type="text", text=json.dumps({"count": len(items), "items": items})
                )
            ]

        return lookup_error_result(f"unknown tool: {name}")


__all__ = ["SendError", "register_tools"]
