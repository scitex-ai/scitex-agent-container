"""Per-agent ``CCT_STATE_DIR`` override for the telegrammer MCP server.

Incident (2026-07-01): every agent wired for the ``claude-code-telegrammer``
stdio MCP ships the SAME hardcoded ``CCT_STATE_DIR``
(``/home/agent/.claude-code-telegrammer-dev``) in its ``.mcp.json``. The
telegrammer process keys its pidfile + ``messages.db`` on that dir, so all
agents collide on one state dir and its newest-wins takeover leaves only ONE
agent connected.

Fix (deterministic fleet rule, applied at ``.mcp.json`` materialization time):
when the merged ``mcpServers`` carries a ``claude-code-telegrammer`` entry,
rewrite its ``env.CCT_STATE_DIR`` to a per-agent value derived from the agent
name — ``/home/agent/.claude-code-telegrammer-<AGENT_NAME>``. Distinct agent
name ⇒ distinct dir ⇒ no collision ⇒ all agents connect.

``/home/agent`` is the literal CONTAINER ``$HOME`` (agents run as
``HOME=/home/agent``), so the prefix is hardcoded on purpose — it must NOT be
resolved against the host home.

Only ``CCT_STATE_DIR`` is touched; the other env (``CCT_BOT_TOKEN`` /
``CCT_AGENT_ID``, which stay as their literal ``$VAR`` for runtime expansion)
is left untouched. Non-telegrammer agents are a strict no-op.
"""

from __future__ import annotations

from typing import Any

# The telegrammer MCP server key in ``mcpServers``.
_TELEGRAMMER_SERVER_NAME = "claude-code-telegrammer"

# The env var the telegrammer process keys its pidfile + messages.db on.
_STATE_DIR_ENV_KEY = "CCT_STATE_DIR"

# Literal CONTAINER $HOME prefix — agents run as HOME=/home/agent. Hardcoded
# on purpose: must NOT be resolved against the host home (no expanduser).
_CONTAINER_HOME = "/home/agent"


def per_agent_state_dir(agent_name: str) -> str:
    """The per-agent ``CCT_STATE_DIR`` value for ``agent_name``."""
    return f"{_CONTAINER_HOME}/.claude-code-telegrammer-{agent_name}"


def apply_per_agent_state_dir(merged: dict, agent_name: str) -> dict:
    """Override the telegrammer server's ``CCT_STATE_DIR`` in ``merged``.

    Mutates and returns the merged ``.mcp.json`` dict. When the merged
    ``mcpServers`` contains a ``claude-code-telegrammer`` entry, its
    ``env.CCT_STATE_DIR`` is set to :func:`per_agent_state_dir` — always (the
    source value is always the buggy shared ``-dev``, so this is unconditional).
    No-op when the server is absent or ``agent_name`` is empty.
    """
    if not agent_name:
        return merged
    servers = merged.get("mcpServers")
    if not isinstance(servers, dict):
        return merged
    server = servers.get(_TELEGRAMMER_SERVER_NAME)
    if not isinstance(server, dict):
        return merged
    env: Any = server.get("env")
    if not isinstance(env, dict):
        env = {}
        server["env"] = env
    env[_STATE_DIR_ENV_KEY] = per_agent_state_dir(agent_name)
    return merged


__all__ = ["apply_per_agent_state_dir", "per_agent_state_dir"]
