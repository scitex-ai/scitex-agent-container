"""Resolve agent name or path to a config file path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"


def _resolve_host() -> str:
    """Return ${SCITEX_OROCHI_HOSTNAME:-$(hostname -s)} (empty on failure)."""
    env = os.environ.get("SCITEX_OROCHI_HOSTNAME", "").strip()
    if env:
        return env
    try:
        import socket

        hn = socket.gethostname()
        return hn.split(".", 1)[0] if hn else ""
    except Exception:
        return ""


def _search_dirs() -> Tuple[Path, List[Path], List[Path]]:
    """Return (legacy_dir, env_dirs, fleet_dirs) with ~ expansion.

    ``fleet_dirs`` is the ordered list of fleet locations searched after
    env overrides. Order = host-specific first, then shared, then
    legacy-flat (both under ~/.scitex/orochi and ~/.dotfiles/src/.scitex/orochi).
    """
    home = Path(os.path.expanduser("~"))
    legacy = home / ".scitex" / "agent-container" / "agents"
    env_raw = os.environ.get(_ENV_VAR, "")
    env_dirs = [Path(os.path.expanduser(p)) for p in env_raw.split(":") if p.strip()]

    host = _resolve_host()
    fleet_roots = [
        home / ".scitex" / "orochi",
        home / ".dotfiles" / "src" / ".scitex" / "orochi",
    ]
    fleet_dirs: list[Path] = []
    for root in fleet_roots:
        if host:
            fleet_dirs.append(root / host / "agents")
        fleet_dirs.append(root / "shared" / "agents")
        fleet_dirs.append(root / "agents")
    return legacy, env_dirs, fleet_dirs


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
      3. Fleet layout — for each root in
         (~/.scitex/orochi, ~/.dotfiles/src/.scitex/orochi):
             a. ``<root>/<HOST>/agents/<name>/<name>.yaml`` (host override)
             b. ``<root>/shared/agents/<name>/<name>.yaml`` (shared default)
             c. ``<root>/agents/<name>/<name>.yaml`` (legacy flat layout)

    Absolute paths and explicit .yaml/.yml paths are returned as-is if they
    exist (unchanged behavior).
    """
    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")

    legacy, env_dirs, fleet_dirs = _search_dirs()

    hit = _try_dir(legacy, name_or_path)
    if hit:
        return hit
    for d in env_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit
    for d in fleet_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit

    env_line = (
        f"  (env ${_ENV_VAR}: "
        f"{', '.join(str(d) for d in env_dirs) if env_dirs else '<unset>'})"
    )
    fleet_lines = "\n".join(
        f"  {d}/{name_or_path}/{name_or_path}.yaml" for d in fleet_dirs
    )
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found. Searched:\n"
        f"  {legacy}/{name_or_path}.yaml\n"
        f"{env_line}\n"
        f"{fleet_lines}"
    )
