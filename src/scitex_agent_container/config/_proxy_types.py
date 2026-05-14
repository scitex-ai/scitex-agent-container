"""ProxySpec dataclass for ``kind: AgentProxy`` agents.

Lives in its own module to keep ``_types.py`` under the project's
512-line cap. Re-exported from :mod:`scitex_agent_container.config`
alongside the rest of the spec dataclasses.

# kind: AgentProxy — forward POST /v1/turn to an external A2A endpoint.
#
# A proxy agent has NO SDK. The runner is a thin Starlette app that:
#   * forwards POST /v1/turn to `upstream` (no Claude in our container)
#   * re-projects the upstream AgentCard at our own /.well-known/...
#     (overriding name + url + x-scitex-agent-container.kind)
#
# Trust levels are advisory — they're surfaced on the AgentCard so
# operators downstream can route accordingly, but they DON'T change
# the egress story (that's covered by the proxy runner's own
# allowlist-only-this-host policy).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProxySpec:
    """Configuration for kind: AgentProxy agents."""

    upstream: str = ""
    """REQUIRED. Full URL to the upstream A2A AgentCard endpoint.
    Either an explicit .well-known path or a base URL (we'll fetch
    ``<base>/.well-known/agent-card.json`` if a base is given)."""

    trust: str = "untrusted"
    """One of ``untrusted`` (default — operator must opt in to anything
    more permissive), ``local-mesh`` (peers on the same private network
    you control), ``trusted`` (cryptographically verified — reserved
    for future mTLS work). Surfaced on the AgentCard's
    ``x-scitex-agent-container.trust`` field."""

    redact: list[str] = field(default_factory=list)
    """Substring tokens; any inbound `text` field containing one is
    refused with HTTP 400. Cheap defense-in-depth against accidentally
    forwarding secrets to an untrusted upstream — NOT a substitute for
    proper output filtering at the source."""

    timeout_s: float = 30.0
    """Per-turn upstream HTTP timeout. Forwarded turns that take longer
    than this surface as 504 to the caller."""


_VALID_TRUST_LEVELS = frozenset({"untrusted", "local-mesh", "trusted"})


def is_valid_trust(value: str) -> bool:
    """True if ``value`` is one of the accepted ``spec.proxy.trust`` strings."""
    return value in _VALID_TRUST_LEVELS


__all__ = ["ProxySpec", "is_valid_trust"]
