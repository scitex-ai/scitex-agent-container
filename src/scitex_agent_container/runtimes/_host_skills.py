"""Deploy a curated set of the operator's host ``~/.claude/skills/`` as an agent baseline.

Claude Code dev-rule skills (the ``ywatanabe`` and ``scitex`` skill sets)
live in the standard host directory ``~/.claude/skills/`` (one ``<name>/``
dir per skill, each carrying a ``SKILL.md``). They are NOT visible inside an
agent container, whose ``~/.claude/skills/`` only carries whatever the agent
materialized for itself — so the operator's general dev-rule skills never load.

This module closes that gap, mirroring :mod:`._host_commands`: at deploy time
it resolves each curated host skill dir (``Path("~/.claude/skills/<name>").
expanduser()`` — never a hard-coded username) and symlinks it into the agent's
materialized ``.claude/skills/<name>``.

Why a curated allowlist: the operator chose exactly the dev-rule skill SETS
(``ywatanabe`` and ``scitex``) — NOT the tool skills, and explicitly NOT
``secret`` / ``scitex-lead``. Keep the allowlist tight.

Why symlink (not copy): the host entry ``~/.claude/skills/<name>`` is itself a
symlink (e.g. ``-> ~/.dotfiles/src/.claude/skills/ywatanabe``); its
``.resolve()``d real target is bind-visible inside the container (apptainer
binds the host home at the same path), it matches how the operator's own
``~/.claude/skills/`` references these (symlinks), and it stays live without
re-copying. The link is created with the ABSOLUTE resolved target (a relative
link would break — the agent home sits at a different depth).

Precedence: per-agent / sac-bundled skills WIN. If an agent-side
``skills/<name>`` already exists, it is left untouched (no clobber).

Skip-if-missing: a curated name absent on the host → no-op for that name (no
error). No empty ``skills/`` dir is fabricated when nothing is deployed.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The curated host dev-rule skill SETS the operator chose to propagate into
# every agent. Deliberately excludes tool skills and ``secret`` /
# ``scitex-lead``. Resolved fresh at deploy time so each always tracks the real
# host user (no hard-coded username).
_DEFAULT_SKILLS: tuple[str, ...] = ("ywatanabe", "scitex")


def deploy_host_skills(
    workspace_home: Path,
    names: tuple[str, ...] = _DEFAULT_SKILLS,
) -> None:
    """Symlink curated host ``~/.claude/skills/<name>`` into ``<workspace_home>/.claude/skills/``.

    For each ``name`` in ``names`` resolve the host skill dir
    ``~/.claude/skills/<name>`` (itself a symlink) to its real absolute target
    via ``.resolve()``. When that target exists and is a directory, create a
    symlink ``<workspace_home>/.claude/skills/<name>`` pointing at the ABSOLUTE
    resolved target.

    Skip-if-missing: a host skill absent → no-op for that name. No clobber: an
    existing agent-side ``skills/<name>`` (per-agent / bundled) is left in
    place. The agent ``skills/`` dir is created only when at least one skill is
    deployed.
    """
    dst_dir = Path(workspace_home) / ".claude" / "skills"
    for name in names:
        host_entry = Path(f"~/.claude/skills/{name}").expanduser()
        target = host_entry.resolve()
        if not target.is_dir():
            continue
        dst = dst_dir / name
        if dst.exists() or dst.is_symlink():
            # Per-agent / bundled skill of the same name wins — do not clobber.
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(target)
        logger.info("to_home: host skill %s -> %s", name, target)


__all__ = ["deploy_host_skills"]
