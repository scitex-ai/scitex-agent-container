"""Auto-generate .mcp.json from agent orochi config."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from ..config import AgentConfig

logger = logging.getLogger(__name__)

_MCP_SERVER_KEY = "scitex-orochi"


def _find_mcp_channel_ts() -> str | None:
    """Locate the orochi MCP channel TypeScript entry point.

    Strategy:
    1. Import scitex_orochi and derive ts/mcp_channel.ts relative to its package root.
    2. Fallback: check ~/proj/scitex-orochi/ts/mcp_channel.ts.

    Returns the absolute path as a string, or None if not found.
    """
    # Strategy 1: derive from installed package
    try:
        result = subprocess.run(
            ["python3", "-c", "import scitex_orochi; print(scitex_orochi.__file__)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            init_path = Path(result.stdout.strip())
            # __init__.py is at src/scitex_orochi/__init__.py
            # ts/mcp_channel.ts is at the repo root: ../../ts/mcp_channel.ts
            repo_root = init_path.parent.parent.parent
            candidate = repo_root / "ts" / "mcp_channel.ts"
            if candidate.exists():
                return str(candidate.resolve())
    except Exception:
        pass

    # Strategy 2: common path
    fallback = Path.home() / "proj" / "scitex-orochi" / "ts" / "mcp_channel.ts"
    if fallback.exists():
        return str(fallback.resolve())

    return None


def setup_mcp_config(config: AgentConfig, workdir: str) -> None:
    """Generate or merge .mcp.json in *workdir* when orochi is configured.

    Does nothing if ``config.orochi.enabled`` is False or no hosts are
    defined.  Merges with an existing ``.mcp.json`` so other MCP servers
    are preserved.
    """
    if not config.orochi.enabled or not config.orochi.hosts:
        return

    mcp_channel_path = _find_mcp_channel_ts()
    if mcp_channel_path is None:
        logger.warning(
            "Orochi MCP channel TypeScript not found; skipping .mcp.json generation "
            "for agent '%s'. Install scitex-orochi or place it at ~/proj/scitex-orochi.",
            config.name,
        )
        return

    host = config.orochi.hosts[0]
    port = str(config.orochi.port)

    channels = (
        ",".join(config.orochi.channels) if config.orochi.channels else "#general"
    )
    token_env = config.orochi.token_env or "SCITEX_OROCHI_TOKEN"
    token_val = os.environ.get(token_env, "")

    server_entry = {
        "type": "stdio",
        "command": "bun",
        "args": ["run", mcp_channel_path],
        "env": {
            "SCITEX_OROCHI_HOST": host,
            "SCITEX_OROCHI_PORT": port,
            "SCITEX_OROCHI_AGENT": config.name,
            "SCITEX_OROCHI_CHANNELS": channels,
            "SCITEX_OROCHI_TOKEN": token_val,
        },
    }

    mcp_path = Path(workdir) / ".mcp.json"

    # Merge with existing .mcp.json if present
    existing: dict = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read existing .mcp.json at %s: %s — overwriting",
                mcp_path,
                exc,
            )

    if not isinstance(existing, dict):
        existing = {}

    servers = existing.setdefault("mcpServers", {})
    servers[_MCP_SERVER_KEY] = server_entry

    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(existing, indent=2) + "\n")

    logger.info(
        "Generated .mcp.json for agent '%s' at %s (host=%s, port=%s)",
        config.name,
        mcp_path,
        host,
        port,
    )


def cleanup_mcp_config(config: AgentConfig, workdir: str) -> None:
    """Remove the scitex-orochi entry from .mcp.json on agent stop.

    If the file becomes empty (no other servers), it is deleted entirely.
    """
    if not config.orochi.enabled:
        return

    mcp_path = Path(workdir) / ".mcp.json"
    if not mcp_path.exists():
        return

    try:
        data = json.loads(mcp_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    servers = data.get("mcpServers", {})
    if _MCP_SERVER_KEY not in servers:
        return

    del servers[_MCP_SERVER_KEY]

    if not servers:
        # No servers left — remove the file
        mcp_path.unlink(missing_ok=True)
        logger.info("Removed empty .mcp.json at %s", mcp_path)
    else:
        data["mcpServers"] = servers
        mcp_path.write_text(json.dumps(data, indent=2) + "\n")
        logger.info("Removed %s entry from .mcp.json at %s", _MCP_SERVER_KEY, mcp_path)
