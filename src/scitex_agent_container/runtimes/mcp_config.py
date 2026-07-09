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


def _setup_mcp_from_servers(
    servers: dict[str, dict], workdir: str, agent_name: str
) -> None:
    """Write mcp_servers entries directly to .mcp.json (v2 path)."""
    if not servers:
        return

    mcp_path = Path(workdir) / ".mcp.json"

    existing: dict = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, OSError):  # stx-allow: fallback (reason: malformed JSON tolerated)
            pass

    if not isinstance(existing, dict):
        existing = {}

    mcp_servers = existing.setdefault("mcpServers", {})

    for name, entry in servers.items():
        # Resolve ${VAR} env references from os.environ at write time
        resolved = dict(entry)
        if "env" in resolved and isinstance(resolved["env"], dict):
            import re

            resolved["env"] = {
                k: re.sub(
                    r"\$\{(\w+)\}",
                    lambda m: os.environ.get(m.group(1), m.group(0)),
                    str(v),
                )
                for k, v in resolved["env"].items()
            }
        # Expand ~ in args
        if "args" in resolved and isinstance(resolved["args"], list):
            resolved["args"] = [
                str(Path(a).expanduser()) if a.startswith("~") else a
                for a in resolved["args"]
            ]
        mcp_servers[name] = resolved

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
    existing ``.mcp.json`` so other MCP servers are preserved.
    """
    if not config.mcp_servers:
        return
    _setup_mcp_from_servers(config.mcp_servers, workdir, config.name)


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
    except (json.JSONDecodeError, OSError):  # stx-allow: fallback (reason: malformed JSON tolerated)
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
