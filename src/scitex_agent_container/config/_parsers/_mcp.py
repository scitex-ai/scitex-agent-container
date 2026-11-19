"""``spec.mcp_servers`` interpolator.

Only the interpolator currently lives here — the raw mcp_servers dict
is parsed elsewhere (loaders use it verbatim after this pass).
``parse_mcp_servers`` is reserved for future use; today the
``interpolate_mcp_servers`` pass IS the spec.mcp_servers handling.
"""

from __future__ import annotations

from ._helpers import interpolate_metadata


def interpolate_mcp_servers(mcp_raw: dict, metadata: dict) -> dict[str, dict]:
    """Deep-interpolate ${metadata.*} in mcp_servers env values.

    Also validates each entry's SAC-only ``inherit_env`` (bool or a list of
    env names) so a malformed value fails at load, not at launch.
    """
    result: dict[str, dict] = {}
    for server_name, server_def in (mcp_raw or {}).items():
        entry = dict(server_def)
        if "inherit_env" in entry:
            # Lazy: keeps `sac --help`'s import closure unchanged.
            from .._mcp_inherit_env import inherit_mode

            inherit_mode(str(server_name), entry)
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
