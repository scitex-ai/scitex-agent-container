"""Where the neutral layers live (ADR-0033 §Layers).

* user layer:    ``~/.scitex/agent-container/.agents`` — fleet-wide default,
  delivered to every host by dotfiles. Override with ``SAC_AGENTS_DIR``.
* project layer: ``<git-root>/.scitex/agent-container/.agents`` — wins over
  the user layer.

Both hold config only. Secrets never live here; reference them as ``${VAR}``.
"""

from __future__ import annotations

import os
from pathlib import Path

LAYER_REL = Path(".scitex") / "agent-container" / ".agents"
USER_ENV = "SAC_AGENTS_DIR"


def user_layer_root() -> Path:
    """The user layer path (not resolved: ``~/.scitex`` may be a symlink)."""
    override = os.environ.get(USER_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / LAYER_REL


def find_project_root(start: Path | None = None) -> Path | None:
    """Nearest ancestor holding ``.git`` (dir or worktree file), else None."""
    here = Path(start or Path.cwd()).absolute()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def project_layer_root(project_root: Path) -> Path:
    return Path(project_root) / LAYER_REL


__all__ = [
    "LAYER_REL",
    "USER_ENV",
    "find_project_root",
    "project_layer_root",
    "user_layer_root",
]
