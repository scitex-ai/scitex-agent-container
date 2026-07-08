"""Fleet MCP cold-start reliability knobs (incident 2026-07-06).

Root cause (root-caused + verified by the fleet): the agents'
``scitex-agent-container`` (``sac mcp start``) and ``scitex-todo``
(``scitex-todo mcp start``) stdio MCP servers INTERMITTENTLY fail to connect
at Claude Code session start. The server itself starts healthy — the failure
is a CLIENT-SIDE connect-timing RACE: the heavy ``fastmcp`` import makes the
stdio server slow to become ready and intermittently exceeds Claude Code's
default MCP startup/connect timeout. Claude Code does NOT auto-reconnect a
failed *stdio* MCP (its 3-initial / 5-mid-session retries are HTTP/SSE ONLY),
so the agent then runs its ENTIRE session missing those tools (``host_exec``,
``agent_spawn``, ``db_*``, and the todo tools).

Two deterministic, config-only levers (both distributed via the to_home
materialization so every agent picks them up on next deploy):

1. ``alwaysLoad: true`` on each critical stdio server entry in ``.mcp.json``
   → forces BLOCKING startup: the session WAITS for the server to connect
   (capped at ``MCP_TIMEOUT``) instead of the default racy background connect.
   This converts "sometimes loses the race" into "deterministically waits for
   ready." Supported by Claude Code v2.1.121+; a harmless no-op on older
   clients (an unknown key is ignored).

2. ``MCP_TIMEOUT`` (milliseconds) raised in the agent launch env → gives the
   slow cold-start room. This is the CLIENT startup-connect-timeout env var,
   DISTINCT from the per-server ``timeout`` field in ``.mcp.json`` (which is a
   per-tool-call wall-clock cap — deliberately NOT touched here).

See ``docs/mcp-cold-start.md`` and the boot self-check
(``sac mcp healthcheck`` / the ``SessionStart`` hook) that heals a server
which still fails to connect.
"""

from __future__ import annotations

# Critical stdio MCP servers whose cold-start MUST win the connect race. These
# names match the server keys in the distributed ``.mcp.json`` (``sac`` is the
# short alias some baselines use for the scitex-agent-container server).
CRITICAL_MCP_SERVERS: tuple[str, ...] = (
    "scitex-agent-container",
    "sac",
    "scitex-todo",
)

# Client MCP startup connect timeout, in milliseconds. Raised to 120 s
# (2026-07-07) for the single ``scitex serve`` aggregator: its cold start
# (matplotlib font-cache build + ~30 heavy scientific peer imports + bounded
# 8 s hung-peer resolve timeouts) can exceed 30 s on a fresh container home,
# and under ``alwaysLoad`` a too-short timeout darkens the whole tool surface
# (the same dark-tool-MCPs failure the aggregator is meant to close). 120 s
# gives cold starts wide margin while still bounding a genuinely dead server.
# Revert toward 30 s once the SIF bakes the matplotlib font cache and the
# hung-import peers (types/resource/orochi) are fixed — both drop the
# aggregator cold start to a few seconds.
MCP_STARTUP_TIMEOUT_MS: str = "120000"

# Env var name Claude Code reads for the MCP startup connect timeout.
MCP_TIMEOUT_ENV_VAR: str = "MCP_TIMEOUT"


def inject_always_load(doc: dict) -> dict:
    """Stamp ``alwaysLoad: true`` onto each critical server in a ``.mcp.json``.

    Mutates and returns ``doc`` (a parsed ``.mcp.json`` mapping). For every
    :data:`CRITICAL_MCP_SERVERS` name present under ``mcpServers`` that does not
    already carry an explicit ``alwaysLoad`` key, sets it to ``True`` so Claude
    Code blocks on startup until that stdio server connects. Servers not present
    are skipped; a pre-existing explicit ``alwaysLoad`` (True or False) is left
    untouched so a deliberate per-agent override still wins. Fail-open: a
    malformed / non-dict ``mcpServers`` is returned unchanged.
    """
    if not isinstance(doc, dict):
        return doc
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        return doc
    for name in CRITICAL_MCP_SERVERS:
        entry = servers.get(name)
        if isinstance(entry, dict):
            entry.setdefault("alwaysLoad", True)
    return doc


def mcp_timeout_env_flags() -> list[str]:
    """Return the apptainer ``--env`` flags that raise the MCP startup timeout.

    Appended by the apptainer runtime's managed-env injector so every agent
    container launches ``claude`` with a generous MCP startup connect timeout.
    """
    return ["--env", f"{MCP_TIMEOUT_ENV_VAR}={MCP_STARTUP_TIMEOUT_MS}"]


__all__ = [
    "CRITICAL_MCP_SERVERS",
    "MCP_STARTUP_TIMEOUT_MS",
    "MCP_TIMEOUT_ENV_VAR",
    "inject_always_load",
    "mcp_timeout_env_flags",
]
