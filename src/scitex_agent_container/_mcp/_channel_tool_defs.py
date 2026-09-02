"""Tool schema definitions for the send-side ``a2a_*`` MCP tools.

Extracted from :mod:`._channel_tools` (module size budget) — pure data,
no closures, so it has no business living inside ``register_tools``.
"""

from __future__ import annotations

from mcp.types import Tool

__all__ = ["build_tool_list"]


def build_tool_list() -> list[Tool]:
    """Return the ``a2a_*`` tool schemas ``list_tools`` advertises."""
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


# EOF
