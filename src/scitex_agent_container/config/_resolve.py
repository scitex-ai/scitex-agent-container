"""Resolve agent name or path to a config file path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"


def _search_dirs() -> Tuple[Path, List[Path]]:
    """Return (primary_dir, env_dirs) with ~ expansion.

    ``primary_dir`` is ``~/.scitex/agent-container/agents/`` (sac's own root).
    ``env_dirs`` is the colon-separated list from
    ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — the plugin port that external
    orchestrators (orochi, etc.) use to extend sac's search scope without
    sac knowing about them.
    """
    home = Path(os.path.expanduser("~"))
    primary = home / ".scitex" / "agent-container" / "agents"
    env_raw = os.environ.get(_ENV_VAR, "")
    env_dirs = [Path(os.path.expanduser(p)) for p in env_raw.split(":") if p.strip()]
    return primary, env_dirs


def _try_dir(base: Path, name: str) -> str | None:
    """Try <base>/<name>.yaml|yml and <base>/<name>/<name>.yaml|yml."""
    for ext in (".yaml", ".yml"):
        cand = base / f"{name}{ext}"
        if cand.exists():
            return str(cand)
        cand = base / name / f"{name}{ext}"
        if cand.exists():
            return str(cand)
    return None


def resolve_config(name_or_path: str) -> str:
    """Resolve agent name or path to a config file path.

    Search order for short names (no slash, no .yaml/.yml suffix):
      1. ~/.scitex/agent-container/agents/<name>.yaml
         or ~/.scitex/agent-container/agents/<name>/<name>.yaml
      2. Each dir in $SCITEX_AGENT_CONTAINER_YAML_DIRS (colon-separated).
         Plugin port for external orchestrators to extend the search scope.

    Absolute paths and explicit .yaml/.yml paths are returned as-is if they
    exist.
    """
    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")

    primary, env_dirs = _search_dirs()

    hit = _try_dir(primary, name_or_path)
    if hit:
        return hit
    for d in env_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit

    env_line = (
        f"  (env ${_ENV_VAR}: "
        f"{', '.join(str(d) for d in env_dirs) if env_dirs else '<unset>'})"
    )
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found. Searched:\n"
        f"  {primary}/{name_or_path}.yaml\n"
        f"  {primary}/{name_or_path}/{name_or_path}.yaml\n"
        f"{env_line}"
    )
