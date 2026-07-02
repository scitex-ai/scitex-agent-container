"""Deep-merge of two `.mcp.json` docs with per-agent precedence.

`runtimes/_to_home.deploy_to_home` is a two-pass overlay (shared baseline
`_shared/to_home/` → per-agent `to_home/`). For most files the per-agent layer
full-overwrites the baseline. For `.mcp.json` that is WRONG: it would silently
drop the baseline's default servers (sac / scitex-todo / claude-code-telegrammer)
for any agent that ships its own `.mcp.json`.

So we DEEP-MERGE the two `mcpServers` maps: disjoint names combine, and a
same-name server is recursively merged with the **per-agent (overlay) value
winning** on any leaf conflict (operator 2026-07-02: a per-agent override of a
baseline server — e.g. claude-code-telegrammer with per-agent identity — is a
legitimate, common need; a fail-loud exception was too strict). A genuine
override is not fatal: it is LOGGED at WARNING (visible, not fail-loud), matching
the rest of the to_home cascade ("higher layer wins on conflict").
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class McpMergeConflict(Exception):
    """Deprecated: retained for import compatibility only.

    No longer raised — a same-name server override now deep-merges with the
    per-agent (overlay) value winning, logging a WARNING instead of aborting.
    """


def _deep_merge(base: Any, overlay: Any, *, name: str, path: str) -> Any:
    """Recursively merge ``overlay`` onto ``base``; overlay wins on leaf conflicts.

    A differing leaf (overlay overrides a non-equal base value) is logged at
    WARNING so a per-agent override of the shared baseline stays visible.
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            if key in base:
                child = f"{path}.{key}" if path else key
                out[key] = _deep_merge(base[key], value, name=name, path=child)
            else:
                out[key] = value
        return out
    if base != overlay:
        logger.warning(
            "mcpServers['%s'].%s: per-agent overrides shared baseline (%r -> %r)",
            name,
            path or "(root)",
            base,
            overlay,
        )
    return overlay


def merge_mcp_json(baseline: dict, overlay: dict) -> dict:
    """Return ``baseline`` deep-merged with ``overlay`` (per-agent precedence).

    * ``mcpServers`` — union by name; a same-name server is deep-merged with the
      overlay (per-agent) winning on leaf conflicts. A differing leaf is
      WARNING-logged, never fatal.
    * Other top-level keys — ``overlay`` wins where present, else ``baseline``
      is preserved.
    """
    merged: dict[str, Any] = dict(baseline)
    servers: dict[str, Any] = dict(baseline.get("mcpServers") or {})
    for name, defn in (overlay.get("mcpServers") or {}).items():
        if name in servers:
            servers[name] = _deep_merge(servers[name], defn, name=name, path="")
        else:
            servers[name] = defn
    for key, value in overlay.items():
        if key != "mcpServers":
            merged[key] = value
    merged["mcpServers"] = servers
    return merged


__all__ = ["McpMergeConflict", "merge_mcp_json"]
