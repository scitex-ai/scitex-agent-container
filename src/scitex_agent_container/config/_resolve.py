"""Resolve agent name or path to a config file path."""

from __future__ import annotations

from pathlib import Path


def resolve_config(name_or_path: str) -> str:
    """Resolve agent name or path to a config file path."""
    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")
    user_dir = Path.home() / ".scitex" / "agent-container" / "agents"
    for ext in (".yaml", ".yml"):
        candidate = user_dir / f"{name_or_path}{ext}"
        if candidate.exists():
            return str(candidate)
        # Subdirectory convention: agents/<name>/<name>.yaml
        candidate = user_dir / name_or_path / f"{name_or_path}{ext}"
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found in ~/.scitex/agent-container/agents/\n"
        f"  Create: cp templates/... "
        f"~/.scitex/agent-container/agents/{name_or_path}.yaml"
    )
