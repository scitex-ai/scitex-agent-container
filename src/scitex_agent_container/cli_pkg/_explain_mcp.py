"""``sac agents explain`` — per-MCP-entry env NAMES and ``inherit_env`` mode.

Names only, never values: the env an entry receives can carry store DSNs and
credentials. The inherited names are computed with the same
:func:`..config._mcp_inherit_env.received_env_names` selection the
``.mcp.json`` bake uses, so the plan cannot drift from what is written.
"""

from __future__ import annotations

from typing import Any

from ..config._mcp_inherit_env import describe_mode, inherit_mode, received_env_names


def mcp_env_lines(config: Any) -> list[str]:
    """One line per ``spec.mcp_servers`` entry: mode + received env names."""
    servers = getattr(config, "mcp_servers", None) or {}
    if not servers:
        return []
    spec_env = [str(k) for k in (getattr(config, "env", None) or {})]
    lines = ["", "MCP servers (env NAMES each entry receives; values never shown):"]
    for name in sorted(servers):
        entry = servers[name] if isinstance(servers[name], dict) else {}
        mode = inherit_mode(name, entry)
        label = f"  {name}: inherit_env={describe_mode(mode)}"
        if str(entry.get("type") or "stdio").lower() != "stdio":
            lines.append(f"{label} — remote transport, no child env")
            continue
        declared = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        names = received_env_names(mode, map(str, declared), spec_env)
        lines.append(f"{label} — env: {', '.join(names) or '(none)'}")
    return lines


__all__ = ["mcp_env_lines"]
