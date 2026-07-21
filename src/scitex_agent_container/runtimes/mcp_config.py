"""Auto-generate .mcp.json from agent config's ``spec.mcp_servers``.

Only v2's explicit ``spec.mcp_servers`` block is supported. Legacy v1
auto-generation from an ``orochi`` block has been dropped — external
orchestrators declare their MCP servers explicitly in v2 YAML.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..config import AgentConfig

logger = logging.getLogger(__name__)


def _resolve_env_refs(value: str) -> str:
    """Substitute ``${VAR}`` from ``os.environ``; unset refs stay literal.

    The literal survival makes an unresolved ref a VISIBLE artefact for the
    caller to validate (``assert_expanded``) instead of a silent empty
    string. Same semantics the entry-env resolution below always had.
    """
    import re

    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def _setup_mcp_from_servers(
    servers: dict[str, dict],
    workdir: str,
    agent_name: str,
    spec_env: dict | None = None,
) -> None:
    """Write mcp_servers entries directly to .mcp.json (v2 path).

    ``spec_env`` (the agent's ``spec.env``) is baked into each stdio
    entry's ``env`` block as literal values — entry-declared keys win. The
    first spawn receives spec env by process inheritance (the tmux launch
    exports it), but a mid-session MCP reconnect RESPAWN through the
    sanitized stdio transport env only receives the entry's own ``env``
    block, so the values must be durable there (P1, card
    sac-env-injection-lost-on-mcp-reconnect-20260721).
    """
    if not servers:
        return

    mcp_path = Path(workdir) / ".mcp.json"

    existing: dict = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
        except (
            json.JSONDecodeError,
            OSError,
        ):  # stx-allow: fallback (reason: malformed JSON tolerated)
            pass

    if not isinstance(existing, dict):
        existing = {}

    mcp_servers = existing.setdefault("mcpServers", {})

    for name, entry in servers.items():
        # Resolve ${VAR} env references from os.environ at write time
        resolved = dict(entry)
        if "env" in resolved and isinstance(resolved["env"], dict):
            resolved["env"] = {
                k: _resolve_env_refs(str(v)) for k, v in resolved["env"].items()
            }
        # Expand ~ in args
        if "args" in resolved and isinstance(resolved["args"], list):
            resolved["args"] = [
                str(Path(a).expanduser()) if a.startswith("~") else a
                for a in resolved["args"]
            ]
        mcp_servers[name] = resolved

    # Durable spec env: bake ``spec.env`` literals into every stdio entry so
    # a reconnect respawn (which does not inherit the launch env) still
    # receives them. Entry-declared env keys win. Values are validated —
    # an unexpanded ``${VAR}`` fails here, at build time, not in a child.
    from ._mcp_spec_env import bake_spec_env_values

    if spec_env:
        from ._board_identity_env import assert_expanded

        resolved_spec_env: dict[str, str] = {}
        for key, val in spec_env.items():
            sval = _resolve_env_refs(str(val))
            assert_expanded(str(key), sval)
            resolved_spec_env[str(key)] = sval
        bake_spec_env_values(mcp_servers, resolved_spec_env)

    # Cold-start race fix (fleet incident 2026-07-06): force blocking startup for
    # the critical stdio MCP servers (see ``_mcp_reliability``). Idempotent.
    from ._mcp_reliability import inject_always_load

    inject_always_load(existing)

    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(existing, indent=2) + "\n")

    logger.info(
        "Generated .mcp.json for agent '%s' at %s (servers: %s)",
        agent_name,
        mcp_path,
        ", ".join(servers.keys()),
    )


def setup_mcp_config(config: AgentConfig, workdir: str) -> None:
    """Write ``spec.mcp_servers`` to ``<workdir>/.mcp.json`` (merging).

    No-op if the agent has no ``mcp_servers`` entries. Merges with an
    existing ``.mcp.json`` so other MCP servers are preserved. ``spec.env``
    is baked into each stdio entry's ``env`` block (durable across MCP
    reconnect respawns — see :func:`_setup_mcp_from_servers`).
    """
    if not config.mcp_servers:
        return
    _setup_mcp_from_servers(
        config.mcp_servers,
        workdir,
        config.name,
        spec_env=dict(getattr(config, "env", None) or {}),
    )


def cleanup_mcp_config(config: AgentConfig, workdir: str) -> None:
    """Remove MCP entries declared by this agent from ``<workdir>/.mcp.json``.

    If the file becomes empty (no other servers remain), it is deleted.
    """
    if not config.mcp_servers:
        return
    keys_to_remove = list(config.mcp_servers.keys())

    mcp_path = Path(workdir) / ".mcp.json"
    if not mcp_path.exists():
        return

    try:
        data = json.loads(mcp_path.read_text())
    except (
        json.JSONDecodeError,
        OSError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return

    servers = data.get("mcpServers", {})
    removed = []
    for key in keys_to_remove:
        if key in servers:
            del servers[key]
            removed.append(key)

    if not removed:
        return

    if not servers:
        mcp_path.unlink(missing_ok=True)
        logger.info("Removed empty .mcp.json at %s", mcp_path)
    else:
        data["mcpServers"] = servers
        mcp_path.write_text(json.dumps(data, indent=2) + "\n")
        logger.info("Removed %s from .mcp.json at %s", ", ".join(removed), mcp_path)
