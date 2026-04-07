"""Orochi MCP config generation for Claude Code agents.

When orochi is enabled in agent config, this module generates a temporary
MCP config JSON file that launches the orochi-push TypeScript MCP server,
and provides the CLI flags needed for Claude Code to use it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AgentConfig

logger = logging.getLogger(__name__)


def find_orochi_push_ts() -> str | None:
    """Locate the orochi_push.ts MCP server script.

    Resolution order:
    1. SCITEX_OROCHI_PUSH_TS env var (explicit override)
    2. Relative to the scitex_orochi Python package (dev installs)
    3. Well-known path /opt/scitex-orochi/ts/orochi_push.ts
    """
    # 1. Explicit env override
    env_path = os.environ.get("SCITEX_OROCHI_PUSH_TS")
    if env_path and Path(env_path).is_file():
        return env_path

    # 2. Resolve from scitex_orochi package location (dev install layout)
    try:
        import scitex_orochi

        pkg_file = Path(scitex_orochi.__file__)
        # src/scitex_orochi/__init__.py -> ../../ts/orochi_push.ts
        candidate = pkg_file.parent.parent.parent / "ts" / "orochi_push.ts"
        if candidate.is_file():
            return str(candidate.resolve())
    except ImportError:
        pass

    # 3. Well-known system path
    system_path = Path("/opt/scitex-orochi/ts/orochi_push.ts")
    if system_path.is_file():
        return str(system_path)

    return None


def build_orochi_mcp_config(config: AgentConfig) -> dict | None:
    """Build the MCP server config dict for orochi-push.

    Returns None if orochi is not enabled or the TS file cannot be found.
    """
    if not config.orochi.is_enabled:
        return None

    ts_path = find_orochi_push_ts()
    if ts_path is None:
        logger.warning(
            "Orochi enabled but orochi_push.ts not found. "
            "Set SCITEX_OROCHI_PUSH_TS env var or install scitex-orochi."
        )
        return None

    orochi = config.orochi
    host = orochi.hosts[0] if orochi.hosts else "localhost"

    # Resolve channels: prefer orochi.channels, fall back to claude.channels
    channels = orochi.channels or config.claude.channels or ["#general"]
    channels_str = ",".join(channels)

    # Resolve token from env or config.env
    token = os.environ.get(orochi.token_env, "")
    if not token:
        token = config.env.get(orochi.token_env, "")

    env_block = {
        "SCITEX_OROCHI_HOST": host,
        "SCITEX_OROCHI_PORT": str(orochi.port),
        "SCITEX_OROCHI_AGENT": config.name,
        "SCITEX_OROCHI_CHANNELS": channels_str,
    }
    if token:
        env_block["SCITEX_OROCHI_TOKEN"] = token

    return {
        "mcpServers": {
            "orochi-push": {
                "command": "bun",
                "args": [ts_path],
                "env": env_block,
            }
        }
    }


def write_mcp_config_file(config: AgentConfig) -> str | None:
    """Generate a temporary MCP config JSON file for Claude Code.

    Returns the file path, or None if orochi is not enabled.
    The caller is responsible for cleanup (though the file is small and
    in /tmp, so it is acceptable to leave it for OS cleanup).
    """
    mcp_config = build_orochi_mcp_config(config)
    if mcp_config is None:
        return None

    # Write to a deterministic path so restarts reuse the same file
    config_dir = Path(tempfile.gettempdir()) / "scitex-agent-container"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"mcp-{config.name}.json"

    config_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
    logger.info(
        "Orochi MCP config written to %s (agent=%s, host=%s)",
        config_path,
        config.name,
        mcp_config["mcpServers"]["orochi-push"]["env"]["SCITEX_OROCHI_HOST"],
    )
    return str(config_path)


def get_orochi_claude_flags(config: AgentConfig) -> list[str]:
    """Return extra CLI flags for Claude Code when orochi is enabled.

    Generates the MCP config file and returns flags like:
        --mcp-config /tmp/.../mcp-agent.json
        --dangerously-load-development-channels server:orochi-push
    """
    if not config.orochi.is_enabled:
        return []

    mcp_path = write_mcp_config_file(config)
    if mcp_path is None:
        return []

    flags = [
        f"--mcp-config '{mcp_path}'",
        "--dangerously-load-development-channels server:orochi-push",
    ]
    return flags
