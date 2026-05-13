"""``spec.mcp_servers`` interpolator.

Only the interpolator currently lives here — the raw mcp_servers dict
is parsed elsewhere (loaders use it verbatim after this pass).
``parse_mcp_servers`` is reserved for future use; today the
``interpolate_mcp_servers`` pass IS the spec.mcp_servers handling.
"""

from __future__ import annotations

from ._helpers import interpolate_metadata


def interpolate_mcp_servers(mcp_raw: dict, metadata: dict) -> dict[str, dict]:
    """Deep-interpolate ${metadata.*} in mcp_servers env values."""
    result: dict[str, dict] = {}
    for server_name, server_def in (mcp_raw or {}).items():
        entry = dict(server_def)
        if "env" in entry and isinstance(entry["env"], dict):
            entry["env"] = {
                k: interpolate_metadata(str(v), metadata)
                for k, v in entry["env"].items()
            }
        if "args" in entry and isinstance(entry["args"], list):
            entry["args"] = [
                interpolate_metadata(str(a), metadata) for a in entry["args"]
            ]
        result[server_name] = entry
    return result
