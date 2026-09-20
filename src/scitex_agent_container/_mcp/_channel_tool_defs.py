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
                "Emit the legacy contentless mechanical receipt for a received "
                "message. It does NOT prove model understanding and can never "
                "advance agentic_acked; use a2a_agentic_ack after understanding."
            ),
            inputSchema={
                "type": "object",
                "required": ["msg_id"],
                "properties": {"msg_id": {"type": "string"}},
            },
        ),
        Tool(
            name="a2a_agentic_ack",
            description=(
                "After understanding an inbound request, intentionally acknowledge "
                "its exact dispatch_id nonce with a bounded summary, owner, and "
                "concrete next checkpoint. Mechanical delivery/ACK/reaction does "
                "not call this tool and does not prove understanding."
            ),
            inputSchema={
                "type": "object",
                "required": ["dispatch_id", "understood", "owner", "next_checkpoint"],
                "properties": {
                    "dispatch_id": {"type": "string", "minLength": 1},
                    "understood": {"type": "string", "minLength": 1, "maxLength": 500},
                    "owner": {"type": "string", "minLength": 1, "maxLength": 120},
                    "next_checkpoint": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                },
            },
        ),
        Tool(
            name="a2a_progress",
            description=(
                "Report real progress for an agentically acknowledged dispatch_id. "
                "Use in_progress periodically, or completed/failed terminally; "
                "include a blocker only when one actually exists."
            ),
            inputSchema={
                "type": "object",
                "required": ["dispatch_id", "status", "summary"],
                "properties": {
                    "dispatch_id": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": ["in_progress", "completed", "failed"],
                    },
                    "summary": {"type": "string", "minLength": 1, "maxLength": 500},
                    "blocker": {"type": "string", "minLength": 1, "maxLength": 500},
                },
            },
        ),
        Tool(
            name="a2a_dispatch_status",
            description=(
                "Read one outbound dispatch's verified lifecycle and latest feedback. "
                "Flags missing agentic acknowledgement after timeout so delivery or "
                "reaction cannot be mistaken for understanding."
            ),
            inputSchema={
                "type": "object",
                "required": ["dispatch_id"],
                "properties": {
                    "dispatch_id": {"type": "string", "minLength": 1},
                    "agentic_ack_timeout_s": {"type": "number", "minimum": 0},
                },
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
