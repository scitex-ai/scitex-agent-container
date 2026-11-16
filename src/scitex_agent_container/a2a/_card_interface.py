"""Public transport descriptor for SAC's per-agent SDK JSON-RPC route."""

from __future__ import annotations


def agent_interface(
    base_url: str, name: str, *, protocol_binding: str = "HTTP+JSON"
) -> dict[str, str]:
    """Preserve legacy transport bases; JSONRPC needs the exact POST URL."""
    suffix = "/message:send" if protocol_binding == "JSONRPC" else ""
    return {
        "url": f"{base_url.rstrip('/')}/agents/{name}{suffix}",
        "protocolBinding": protocol_binding,
        "tenant": name,
        "protocolVersion": "1.0",
    }
