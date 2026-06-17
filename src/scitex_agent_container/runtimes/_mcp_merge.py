"""Fail-loud deep-merge of two `.mcp.json` docs (W1 — operator 2026-06-17).

`runtimes/_to_home.deploy_to_home` is a two-pass overlay (shared baseline
`_shared/to_home/` → per-agent `to_home/`). For most files the per-agent layer
full-overwrites the baseline. For `.mcp.json` that is WRONG: it would silently
drop the baseline's default servers (sac / scitex-todo / claude-code-telegrammer)
for any agent that ships its own `.mcp.json`.

This module deep-merges the two `mcpServers` maps instead — every agent inherits
the defaults AND keeps its own servers. A genuine conflict (same server name,
two different definitions) **fails loud** via :class:`McpMergeConflict`; we never
silently pick a winner (operator: fail-fast, fail-loud, no silent fallbacks).
"""

from __future__ import annotations

from typing import Any


class McpMergeConflict(Exception):
    """A server name is defined two different ways across baseline + agent."""


def merge_mcp_json(baseline: dict, overlay: dict) -> dict:
    """Return ``baseline`` deep-merged with ``overlay``.

    * ``mcpServers`` — union by name. Disjoint names combine; an identical
      same-name definition is kept once (idempotent); a CONFLICTING same-name
      definition raises :class:`McpMergeConflict`.
    * Other top-level keys — ``overlay`` wins where present, else ``baseline``
      is preserved.
    """
    merged: dict[str, Any] = dict(baseline)
    servers: dict[str, Any] = dict(baseline.get("mcpServers") or {})
    for name, defn in (overlay.get("mcpServers") or {}).items():
        if name in servers and servers[name] != defn:
            raise McpMergeConflict(
                f"mcpServers['{name}'] is defined differently in the shared "
                f"baseline and the per-agent .mcp.json — resolve it explicitly "
                f"(baseline={servers[name]!r}, agent={defn!r})."
            )
        servers[name] = defn
    for key, value in overlay.items():
        if key != "mcpServers":
            merged[key] = value
    merged["mcpServers"] = servers
    return merged


__all__ = ["McpMergeConflict", "merge_mcp_json"]
