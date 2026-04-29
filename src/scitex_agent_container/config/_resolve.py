"""Resolve agent name or path to a config file path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"


def _search_dirs() -> Tuple[Path, List[Path]]:
    """Return (primary_dir, builtin_fallbacks, env_dirs) with ~ expansion.

    Search order:
      1. ``~/.scitex/agent-container/agents/`` — sac's own install root.
      2. Built-in fallbacks: orochi shared-agents dir + dotfiles-orochi agents dir.
         Covers fresh-host recovery where the sac agents/ dir is empty but the
         orochi shared tree (or dotfiles) already has the yaml.
      3. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — plugin port for external
         orchestrators to extend the search scope without touching sac.
    """
    home = Path(os.path.expanduser("~"))
    primary = home / ".scitex" / "agent-container" / "agents"
    builtin_fallbacks = [
        home / ".scitex" / "orochi" / "shared" / "agents",
        home / ".dotfiles" / "src" / ".scitex" / "orochi" / "agents",
    ]
    env_raw = os.environ.get(_ENV_VAR, "")
    env_dirs = [Path(os.path.expanduser(p)) for p in env_raw.split(":") if p.strip()]
    return primary, builtin_fallbacks, env_dirs


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
      1. ~/.scitex/agent-container/agents/<name>.yaml  (sac install root)
      2. ~/.scitex/orochi/shared/agents/<name>/         (orochi shared tree)
      3. ~/.dotfiles/src/.scitex/orochi/agents/<name>/  (dotfiles orochi tree)
      4. Each dir in $SCITEX_AGENT_CONTAINER_YAML_DIRS (colon-separated).

    Fallbacks 2 and 3 cover fresh-host/Spartan recovery where sac's own
    agents/ dir is empty but the orochi yaml already exists. Pass an
    explicit path (with / or .yaml/.yml) to bypass the search entirely.
    """
    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")

    primary, builtin_fallbacks, env_dirs = _search_dirs()

    hit = _try_dir(primary, name_or_path)
    if hit:
        return hit
    for d in builtin_fallbacks:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit
    for d in env_dirs:
        hit = _try_dir(d, name_or_path)
        if hit:
            return hit

    searched = [
        f"  {primary}/{name_or_path}.yaml",
        f"  {primary}/{name_or_path}/{name_or_path}.yaml",
    ]
    for d in builtin_fallbacks:
        searched.append(f"  {d}/{name_or_path}.yaml  (built-in fallback)")
    if env_dirs:
        env_line = f"  (env ${_ENV_VAR}: {', '.join(str(d) for d in env_dirs)})"
    else:
        env_line = f"  (env ${_ENV_VAR}: <unset>)"
    searched.append(env_line)
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found. Searched:\n" + "\n".join(searched)
    )
