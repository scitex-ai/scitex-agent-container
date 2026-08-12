"""A hook sac ARMS must be a hook sac can RUN.

``settings.json`` arms most hooks by BARE PATH, with no interpreter prefix::

    "command": "$HOME/.claude/hooks/pre-tool-use/enforce_telegram_no_bare_issue.sh"

Claude Code execs that path directly, so a file without the execute bit cannot
run at all. Its bytes are perfect, it is present at exactly the armed path, and
it is dead — which makes this invisible to every check that compares content.
``git diff`` does not show it either; the mode lives in the index, not the
patch.

This is the SECOND way a merged, tested, correct hook lands inert. The first is
the copy nobody updated (see :mod:`._baseline_hook_assets`); this is the copy
nobody could execute. Both defeat the same work, so both are closed in the same
place: on agent start, where sac already knows what it armed and where it put
it.

Measured in the dotfiles baseline that feeds the ``to_home`` cascade
(2026-08-12, read-only sweep): 46 armed hook commands, 43 of them bare-path.
Of those 43, **18 are tracked ``100644``** — they run on that host only because
the working copy was chmod'd at some point, and a fresh clone, a new host, or a
rebuilt container checks out 0644 and arms 18 dead hooks. Among them
``force_background_bash.sh``, ``forbidden_words.sh``, ``enforce_git_dash_C.sh``
— and ``ensure_executable.sh``, the hook whose own job is to repair this, which
on a fresh clone cannot run either.

Scope, stated narrowly on purpose: this asserts a property of SAC'S OWN OUTPUT
— "every hook I armed by bare path is executable" — over paths that resolve
INSIDE the agent's own ``$HOME``. It is not a permission sweep of a home
directory, and it will not follow a symlink out to a host file and chmod that.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# Mode for a runnable hook: owner-writable, everyone read+execute.
HOOK_MODE = 0o755

# Marks a token as a path into the agent's hook tree.
_HOOKS_MARKER = ".claude/hooks/"


def is_executable(path: Path) -> bool:
    """True iff ``path`` carries an execute bit for its owner."""
    return bool(path.stat().st_mode & stat.S_IXUSR)


def armed_bare_path_commands(settings: Path) -> "list[str]":
    """Hook commands in ``settings`` that are invoked as a BARE PATH.

    A command with an interpreter prefix (``python3 <path>``) runs whatever the
    interpreter can read, so its mode does not matter and it is excluded. A
    bare path is exec'd directly and is dead without the bit.

    The scan walks the whole document rather than assuming a shape: the hook
    fragments in this repo already use two different layouts (a bare
    ``"PreToolUse": [...]`` and a nested ``"hooks": {...}``), and a parser tied
    to one of them would silently skip the other.
    """
    try:
        data = json.loads(settings.read_text())
    except (OSError, ValueError) as exc:  # stx-allow: fallback (unreadable settings -> nothing to assert)
        logger.warning("armed-hook exec check: cannot read %s: %s", settings, exc)
        return []

    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "command" and isinstance(node.get("command"), str):
                found.append(node["command"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return [
        tokens[0]
        for cmd in found
        if (tokens := cmd.split()) and _HOOKS_MARKER in tokens[0]
    ]


def ensure_armed_hooks_executable(workspace_home: "Path | str") -> "list[str]":
    """Repair the execute bit on every bare-path-armed hook under ``$HOME``.

    Returns the list of repaired paths (empty when everything was already
    runnable). NEVER RAISES: an agent that boots with one unfixable hook is
    strictly better than an agent that does not boot, and this runs on the path
    that arms the guard on the operator's only channel.
    """
    home = Path(workspace_home)
    repaired: list[str] = []
    try:
        settings = home / ".claude" / "settings.json"
        if not settings.is_file():
            return repaired
        home_real = home.resolve()
        for raw in armed_bare_path_commands(settings):
            rel = raw.replace("${HOME}/", "").replace("$HOME/", "").lstrip("~/")
            target = home / rel
            if not target.is_file():
                continue
            try:
                resolved = target.resolve()
                resolved.relative_to(home_real)
            except (OSError, ValueError):  # stx-allow: fallback (resolves outside this home -> not ours)
                continue
            if is_executable(resolved):
                continue
            os.chmod(resolved, HOOK_MODE)
            repaired.append(str(target))
    except Exception as exc:  # stx-allow: fallback (must never block a start)
        logger.error("armed-hook exec check failed for %s: %s", workspace_home, exc)
        return repaired

    if repaired:
        logger.warning(
            "armed-hook exec check: %d hook(s) were armed by bare path but were "
            "NOT executable, so they could not run at all; mode repaired to "
            "%o: %s",
            len(repaired),
            HOOK_MODE,
            ", ".join(Path(p).name for p in repaired),
        )
    return repaired


__all__ = [
    "HOOK_MODE",
    "armed_bare_path_commands",
    "ensure_armed_hooks_executable",
    "is_executable",
]
