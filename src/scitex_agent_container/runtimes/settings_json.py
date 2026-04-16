"""Pre-configure .claude/settings.local.json to suppress interactive prompts.

Claude Code shows TUI prompts for --dangerously-skip-permissions and
--dangerously-load-development-channels on startup. These prompts block
headless agents because screen/tmux keystroke injection is unreliable
in Claude Code's raw terminal mode.

The fix: write the right settings *before* launching Claude Code so it
never shows these prompts at all.

Settings written:
- skipDangerousModePermissionPrompt: true
    Skips the "Bypass Permissions" radio selector.
- enableAllProjectMcpServers: true
    Auto-enables MCP servers from .mcp.json without asking.
- enabledMcpjsonServers: [<server names>]
    Explicitly whitelists the MCP servers defined in .mcp.json.

The file is merged (not overwritten) so user-added settings survive.
Cleanup removes only the keys this module manages.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import AgentConfig

logger = logging.getLogger(__name__)

# Keys managed by this module — cleanup removes exactly these.
_MANAGED_KEYS = frozenset(
    {
        "skipDangerousModePermissionPrompt",
        "enableAllProjectMcpServers",
        "enabledMcpjsonServers",
    }
)


def _mcp_server_names(config: AgentConfig, workdir: str) -> list[str]:
    """Collect MCP server names from config and on-disk .mcp.json."""
    names: set[str] = set()

    # From config.mcp_servers (v2 path)
    if config.mcp_servers:
        names.update(config.mcp_servers.keys())

    # From on-disk .mcp.json (may have been written by setup_mcp_config or
    # deploy_src_mcp_json earlier in the start flow)
    mcp_path = Path(workdir) / ".mcp.json"
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text())
            names.update(data.get("mcpServers", {}).keys())
        except (json.JSONDecodeError, OSError):
            pass

    return sorted(names)


def _needs_skip_permissions(config: AgentConfig) -> bool:
    """Check if config uses --dangerously-skip-permissions."""
    return any(
        "--dangerously-skip-permissions" in f for f in config.claude.flags
    )


def _needs_dev_channels(config: AgentConfig) -> bool:
    """Check if config uses --dangerously-load-development-channels."""
    return any(
        "--dangerously-load-development-channels" in f
        for f in config.claude.flags
    )


def setup_settings_json(config: AgentConfig, workdir: str) -> None:
    """Write .claude/settings.local.json to pre-accept interactive prompts.

    Merges with any existing content so user settings are preserved.
    Only writes keys that are relevant to the agent's flags.
    """
    settings: dict = {}

    if _needs_skip_permissions(config):
        settings["skipDangerousModePermissionPrompt"] = True

    if _needs_dev_channels(config):
        settings["enableAllProjectMcpServers"] = True
        server_names = _mcp_server_names(config, workdir)
        if server_names:
            settings["enabledMcpjsonServers"] = server_names

    if not settings:
        return

    settings_path = Path(workdir) / ".claude" / "settings.local.json"

    # Merge with existing file
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    if not isinstance(existing, dict):
        existing = {}

    # For enabledMcpjsonServers, merge lists rather than replace
    if "enabledMcpjsonServers" in settings and "enabledMcpjsonServers" in existing:
        merged = set(existing["enabledMcpjsonServers"])
        merged.update(settings["enabledMcpjsonServers"])
        settings["enabledMcpjsonServers"] = sorted(merged)

    existing.update(settings)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n")

    logger.info(
        "Pre-configured .claude/settings.local.json for %s at %s (keys: %s)",
        config.name,
        settings_path,
        ", ".join(sorted(settings.keys())),
    )


def cleanup_settings_json(config: AgentConfig, workdir: str) -> None:
    """Remove managed keys from .claude/settings.local.json on agent stop.

    Preserves any user-added keys. Deletes the file only if it becomes
    empty after cleanup.
    """
    settings_path = Path(workdir) / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return

    try:
        data = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    if not isinstance(data, dict):
        return

    removed = []
    for key in _MANAGED_KEYS:
        if key in data:
            del data[key]
            removed.append(key)

    if not removed:
        return

    if not data:
        settings_path.unlink(missing_ok=True)
        logger.info(
            "Removed empty .claude/settings.local.json at %s", settings_path
        )
    else:
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        logger.info(
            "Removed managed keys from .claude/settings.local.json at %s: %s",
            settings_path,
            ", ".join(removed),
        )
