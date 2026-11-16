"""Project the harness-neutral agent layer into sac's Hermes profiles (ADR-0033).

Hermes agents get the neutral layer's skills and MCP servers:

* skills — copied (never linked: host links dangle inside a container) into
  ``<profile>/neutral-skills``, exposed through ``skills.external_dirs``.
  Names in ``settings.yaml: external_skills`` are skipped; for Hermes those
  (e.g. ``scitex``) arrive through the installed-package projection.
* MCP servers — ``mcp.yaml`` servers are added, loaded eagerly; an agent's own
  ``.mcp.json`` entry wins on a name clash.

Instructions are deliberately NOT projected: every agent already carries its
own ``AGENTS.md`` role prompt, and the operator's personal ``AGENTS.md`` would
otherwise land in every fleet agent's system prompt.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from .._neutral_config import (
    Layer,
    find_project_root,
    load_layer,
    merge_layers,
    project_layer_root,
    user_layer_root,
)

SKILLS_DIR = "neutral-skills"
MARKER = ".sac-neutral-skills"


def neutral_layer_for(config: Any) -> Layer:
    """The merged user + project layer for an agent's workdir."""
    user = load_layer(user_layer_root())
    workdir = getattr(config, "workdir", None)
    root = find_project_root(Path(workdir)) if workdir else None
    project = (
        load_layer(project_layer_root(root))
        if root
        else load_layer(Path("/nonexistent-layer"))
    )
    return merge_layers(user, project)


def _skills_to_project(layer: Layer) -> dict[str, Path]:
    external = set(layer.settings.get("external_skills") or [])
    return {name: path for name, path in layer.skills.items() if name not in external}


def materialize_neutral_skills(layer: Layer, profiles: list[Path]) -> list[str]:
    """Copy the layer's skills into each profile; returns external_dirs entries."""
    skills = _skills_to_project(layer)
    for profile in profiles:
        profile.mkdir(parents=True, exist_ok=True)
        target = profile / SKILLS_DIR
        if target.exists() and not (target / MARKER).is_file():
            raise ValueError(f"Refusing to replace unmanaged skill directory: {target}")
        if not skills:
            if target.exists():
                shutil.rmtree(target)
            continue
        with tempfile.TemporaryDirectory(
            prefix=".neutral-skills-", dir=profile
        ) as scratch:
            staging = Path(scratch) / "projection"
            staging.mkdir()
            for name, path in skills.items():
                shutil.copytree(
                    path,
                    staging / name,
                    symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", ".git", "*.log"),
                )
            (staging / MARKER).write_text(
                "managed by sac (ADR-0033)\n", encoding="utf-8"
            )
            previous = Path(scratch) / "previous"
            if target.exists():
                target.rename(previous)
            try:
                staging.rename(target)
            except OSError:
                if previous.exists():
                    previous.rename(target)
                raise
    # Relative to HERMES_HOME, like the installed-package projection.
    return [SKILLS_DIR] if skills else []


def neutral_mcp_servers(layer: Layer) -> dict[str, dict[str, Any]]:
    """Neutral ``mcp.yaml`` servers in Hermes's ``mcp_servers`` shape."""
    out: dict[str, dict[str, Any]] = {}
    for name, spec in layer.mcp_servers.items():
        if spec.get("transport", "stdio") == "stdio":
            keys = ("command", "args", "env")
        else:
            keys = ("url", "headers")
        out[name] = {k: spec[k] for k in keys if spec.get(k) is not None}
    return out


def merge_neutral_mcp(
    layer: Layer, servers: dict[str, dict[str, Any]], eager_toolsets: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Add neutral servers the agent does not define itself (agent wins)."""
    merged = dict(servers)
    toolsets = list(eager_toolsets)
    for name, entry in neutral_mcp_servers(layer).items():
        if name in merged:
            continue
        merged[name] = entry
        toolsets.append(f"mcp-{name}")
    return merged, toolsets


__all__ = [
    "MARKER",
    "SKILLS_DIR",
    "materialize_neutral_skills",
    "merge_neutral_mcp",
    "neutral_layer_for",
    "neutral_mcp_servers",
]
