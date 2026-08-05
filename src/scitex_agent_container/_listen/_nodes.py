"""External-node registry + comms broker for ``sac listen`` (WI-3).

External nodes are first-class on the sac comms graph: an *identity*
+ *inbox* + *ACL*, with **no spec and no lifecycle**. The handoff
``HANDOFF_AGENT_COMMS_2026-05-19.md`` §2 captures the model:

  > Two kinds [of node], distinguished *only* by who owns the
  > lifecycle:
  >   - *sac-managed* — sac owns the lifecycle (spec, container,
  >     start/stop/health).
  >   - *external* — sac does **not** own the lifecycle. Typically
  >     a plain ``claude`` CLI session. ... An external node has an
  >     identity + an inbox, joins via ``sac mcp channel``, and is
  >     never started or stopped by sac.

This module is the in-process state for external nodes attached to
ONE ``sac listen`` host. It composes two pieces:

* :class:`Broker` (re-exported from :mod:`a2a._inbox_bus`) — the
  same in-memory pub/sub the per-agent A2A server uses; ``sac listen``
  re-uses it so external nodes get the exact same delivery semantics
  as sac-managed agents.

* :class:`NodeRegistry` — caches the synthesised AgentCard for each
  external node. Registration is implicit: the first time a name
  appears on ``/agents/<name>/{message:send,inbox/stream}`` the
  registry mints + caches a minimal A2A v1 AgentCard for that node
  (handoff §4: "sac must synthesize a minimal AgentCard at
  registration"). Subsequent
  ``GET /agents/<name>/.well-known/agent-card.json`` lookups return
  the cached card.

Durability and ACL are intentionally *not* in this module — they
land in WI-1 and WI-2 respectively. Keeping the seam clean means
those layers slot in without restructuring this one.
"""

from __future__ import annotations

import threading
from typing import Any

from ..a2a._inbox_bus import Broker  # re-exported for convenience

__all__ = ["Broker", "NodeRegistry", "synthesize_external_card"]


# Tag that distinguishes an external-node AgentCard from a YAML-backed
# one. Consumers (orchestrators, fleet viz) read this to decide whether to
# show lifecycle controls. Public so tests can import it.
EXTERNAL_NODE_KIND = "external"


class NodeRegistry:
    """Track external nodes attached to one ``sac listen`` host.

    Thread-safe (the listen server is async, but some callers reach
    in from sync paths).
    """

    def __init__(self) -> None:
        self._cards: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register(self, name: str, base_url: str) -> dict[str, Any]:
        """Idempotent: synthesise + cache an external-node AgentCard.

        Returns the cached card on subsequent calls.
        """
        with self._lock:
            existing = self._cards.get(name)
            if existing is not None:
                return existing
            card = synthesize_external_card(name, base_url)
            self._cards[name] = card
            return card

    def card(self, name: str) -> dict[str, Any] | None:
        """Return the cached card for ``name`` or ``None`` if unknown."""
        with self._lock:
            return self._cards.get(name)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._cards

    def names(self) -> list[str]:
        """Sorted list of currently-registered external-node names."""
        with self._lock:
            return sorted(self._cards.keys())


# --- AgentCard synthesis ---------------------------------------------------

# Imported here (not at module top) to defer the dependency on the
# A2A SDK proto types until the listen server is actually used —
# keeps ``import scitex_agent_container`` lean.


def synthesize_external_card(name: str, base_url: str) -> dict[str, Any]:
    """Return a minimal A2A v1 AgentCard for an external node.

    Carries identity + the inbox endpoint + the v1-required capability
    fields, and **nothing runtime/container-shaped** — external nodes
    have no spec (handoff §4 "A2A compliance without a YAML"). The
    ``x-scitex-agent-container.node_kind`` extension field marks the
    card as external so downstream tooling can branch on it without
    consulting the registry.
    """
    base = base_url.rstrip("/")
    agent_base = f"{base}/agents/{name}"
    return {
        "name": name,
        "description": (
            f"external node {name!r} — identity + inbox only, no sac-managed lifecycle"
        ),
        # Version field is required by the proto. Use the v3 marker with
        # an ``-external`` suffix so it's obvious in JSON dumps that the
        # card is synthesised (not projected from a YAML).
        "version": "scitex-agent-container/v3-external",
        "supportedInterfaces": [
            {
                "url": agent_base,
                "protocolBinding": "HTTP+JSON",
                "tenant": name,
                "protocolVersion": "1.0",
            }
        ],
        "provider": {
            "organization": "scitex-agent-container",
            "url": "https://scitex.ai",
        },
        "capabilities": {
            # Streaming SSE is always on for the inbox stream.
            "streaming": True,
            # An external node opts into the sac push channel by running
            # ``sac mcp channel --name <id> --listen-url ...`` — same
            # delivery shape as a sac-managed agent.
            "pushNotifications": True,
            "extendedAgentCard": False,
            "extensions": [
                {
                    "uri": "https://scitex.ai/a2a/extensions/sac-push-channel/v1",
                    "description": (
                        "In-session MCP push: ``sac mcp channel`` subscribes "
                        "to ``/agents/<name>/inbox/stream`` and delivers "
                        "events as ``notifications/claude/channel`` to the "
                        "external node's Claude session."
                    ),
                    "required": False,
                    "params": {
                        "sse_path": f"/agents/{name}/inbox/stream",
                        "send_path": f"/agents/{name}/message:send",
                        "mcp_tools": [
                            "a2a_send",
                            "a2a_reply",
                            "a2a_ack",
                            "a2a_peers",
                            "a2a_inbox",
                        ],
                    },
                }
            ],
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        # External nodes declare no skills — they are addressable
        # identities, not catalogued capabilities. Empty list keeps the
        # proto happy (the field is required, not its content).
        "skills": [],
        "x-scitex-agent-container": {
            "node_kind": EXTERNAL_NODE_KIND,
        },
    }
