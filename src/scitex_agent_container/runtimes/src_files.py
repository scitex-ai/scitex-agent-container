"""Deploy src_CLAUDE.md and src_mcp.json from agent definition directory."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from ..config import AgentConfig

logger = logging.getLogger(__name__)


def _definition_dir(config: AgentConfig) -> Path | None:
    """Return the directory containing the agent YAML, or None."""
    if not config.config_path:
        return None
    return Path(config.config_path).parent


def _interpolate_env(text: str) -> str:
    """Resolve ${VAR} references from os.environ."""
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def _interpolate_metadata(text: str, config: AgentConfig) -> str:
    """Resolve ${metadata.name} and ${metadata.labels.*} references."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return config.name
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            return config.labels.get(label) or m.group(0)
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, text)


def deploy_src_claude_md(config: AgentConfig, workdir: str) -> None:
    """Inject src_CLAUDE.md content into {workdir}/CLAUDE.md.

    Uses section tags to merge without destroying agent-written content.
    If src_CLAUDE.md does not exist, does nothing.
    """
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_CLAUDE.md"
    if not src.exists():
        return

    section_content = src.read_text().strip()
    if not section_content:
        return

    # Interpolate metadata references
    section_content = _interpolate_metadata(section_content, config)

    dest = Path(workdir) / "CLAUDE.md"
    existing = dest.read_text() if dest.exists() else ""

    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_tag = (
        f"<!-- Start of scitex-agent-container generated section ({timestamp}) -->"
    )
    end_tag = "<!-- End of scitex-agent-container generated section -->"

    # Strip existing tags from src content if present (any format)
    section_content = re.sub(
        r"<!--.*?scitex-agent-container.*?-->\n?", "", section_content
    ).strip()
    section_content = f"{start_tag}\n{section_content}\n{end_tag}"

    # Match any variant of the start/end tags for replacement
    pattern = (
        r"<!-- Start of scitex-agent-container generated section.*?-->.*?"
        r"<!-- End of scitex-agent-container generated section -->"
    )

    if re.search(pattern, existing, re.DOTALL):
        updated = re.sub(pattern, section_content, existing, flags=re.DOTALL)
    else:
        separator = (
            "\n\n"
            if existing and not existing.endswith("\n\n")
            else ("\n" if existing and not existing.endswith("\n") else "")
        )
        updated = existing + separator + section_content + "\n"

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(updated)
    logger.info("Deployed src_CLAUDE.md for %s to %s", config.name, dest)


def cleanup_src_claude_md(config: AgentConfig, workdir: str) -> None:
    """Remove the agent-container section from {workdir}/CLAUDE.md."""
    dest = Path(workdir) / "CLAUDE.md"
    if not dest.exists():
        return

    existing = dest.read_text()

    pattern = (
        r"\n*<!-- Start of scitex-agent-container generated section.*?-->.*?"
        r"<!-- End of scitex-agent-container generated section -->\n?"
    )
    updated = re.sub(pattern, "", existing, flags=re.DOTALL)

    if updated != existing:
        dest.write_text(updated)
        logger.info("Cleaned up CLAUDE.md for %s at %s", config.name, dest)


def deploy_src_mcp_json(config: AgentConfig, workdir: str) -> None:
    """Copy src_mcp.json to {workdir}/.mcp.json with interpolation.

    Resolves ${metadata.*} and ${ENV_VAR} references.
    If src_mcp.json does not exist, does nothing.
    """
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_mcp.json"
    if not src.exists():
        return

    text = src.read_text().strip()
    if not text:
        return

    # Interpolate metadata, then env vars
    text = _interpolate_metadata(text, config)
    text = _interpolate_env(text)

    # Validate JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", src, exc)
        return

    dest = Path(workdir) / ".mcp.json"

    # Merge with existing .mcp.json if present
    existing: dict = {}
    if dest.exists():
        try:
            existing = json.loads(dest.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    if not isinstance(existing, dict):
        existing = {}

    # Expand ~ in args for each server
    for server in data.get("mcpServers", {}).values():
        if "args" in server and isinstance(server["args"], list):
            server["args"] = [
                str(Path(a).expanduser()) if a.startswith("~") else a
                for a in server["args"]
            ]

    # Merge mcpServers
    src_servers = data.get("mcpServers", {})
    existing.setdefault("mcpServers", {}).update(src_servers)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info(
        "Deployed src_mcp.json for %s to %s (servers: %s)",
        config.name,
        dest,
        ", ".join(src_servers.keys()),
    )


def cleanup_src_mcp_json(config: AgentConfig, workdir: str) -> None:
    """Remove servers defined in src_mcp.json from {workdir}/.mcp.json."""
    defdir = _definition_dir(config)
    if defdir is None:
        return

    src = defdir / "src_mcp.json"
    if not src.exists():
        return

    try:
        src_data = json.loads(src.read_text())
    except (json.JSONDecodeError, OSError):
        return

    keys_to_remove = list(src_data.get("mcpServers", {}).keys())
    if not keys_to_remove:
        return

    dest = Path(workdir) / ".mcp.json"
    if not dest.exists():
        return

    try:
        data = json.loads(dest.read_text())
    except (json.JSONDecodeError, OSError):
        return

    servers = data.get("mcpServers", {})
    for key in keys_to_remove:
        servers.pop(key, None)

    if not servers:
        dest.unlink(missing_ok=True)
        logger.info("Removed empty .mcp.json at %s", dest)
    else:
        data["mcpServers"] = servers
        dest.write_text(json.dumps(data, indent=2) + "\n")
        logger.info("Cleaned up .mcp.json at %s", dest)
