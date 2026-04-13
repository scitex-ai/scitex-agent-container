"""Resolve agent name or path to a config file path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple


_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"


def _search_dirs() -> Tuple[Path, List[Path], Path]:
    """Return (legacy_dir, env_dirs, dotfiles_dir) with ~ expansion."""
    home = Path(os.path.expanduser("~"))
    legacy = home / ".scitex" / "agent-container" / "agents"
    env_raw = os.environ.get(_ENV_VAR, "")
    env_dirs = [
        Path(os.path.expanduser(p)) for p in env_raw.split(":") if p.strip()
    ]
    dotfiles = home / ".dotfiles" / "src" / ".scitex" / "orochi" / "agents"
    return legacy, env_dirs, dotfiles


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
      1. ~/.scitex/agent-container/agents/<name>.yaml  (operator override)
      2. $SCITEX_AGENT_CONTAINER_YAML_DIRS (colon-separated extra dirs)
      3. ~/.dotfiles/src/.scitex/orochi/agents/<name>/<name>.yaml (fleet fallback)

    Absolute paths and explicit .yaml/.yml paths are returned as-is if they
    exist (unchanged behavior).
    """
    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")

    legacy, env_dirs, dotfiles = _search_dirs()

    hit = _try_dir(legacy, name_or_path)
    if hit:
        return hit
    for d in env_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit
    hit = _try_dir(dotfiles, name_or_path)
    if hit:
        return hit

    env_line = (
        f"  (env ${_ENV_VAR}: "
        f"{', '.join(str(d) for d in env_dirs) if env_dirs else '<unset>'})"
    )
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found. Searched:\n"
        f"  {legacy}/{name_or_path}.yaml\n"
        f"{env_line}\n"
        f"  {dotfiles}/{name_or_path}/{name_or_path}.yaml"
    )
