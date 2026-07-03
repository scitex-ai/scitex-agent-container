"""Boot-time diagnostic: log the effective skills materialized into the agent home.

Pure diagnostic (operator requirement, 2026-07-03). sac's ONLY job re skills is
to materialize ``to_home/.claude/skills/`` VERBATIM into the in-container
``$HOME/.claude/skills/`` (= ``/home/agent/.claude/skills/``) — the ``@``-imports
live in each agent's OWN ``to_home/.claude/CLAUDE.md`` (author-controlled), never
in sac. After :func:`._to_home.deploy_to_home` runs, the agent home holds exactly
what was staged: the per-agent ``to_home/.claude/skills/`` subtree (copied by
``_to_home._walk_and_apply``'s recursive ``iterdir``) plus any curated host skill
sets symlinked in by ``_host_skills.deploy_host_skills``.

Listing that set at INFO on start makes a degraded / empty skill set visible
rather than silently absent — e.g. a ``skills: [scitexification]`` cohort can
confirm ``scitexification`` is present in ``$HOME/.claude/skills/``. Never
raises; a diagnostic must not abort a start.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AgentConfig

logger = logging.getLogger(__name__)


def log_effective_skills(config: AgentConfig, home_dir: str | Path) -> None:
    """Log (INFO) the skill dirs present under ``<home_dir>/.claude/skills``.

    Each immediate child directory is listed by name; a child that lacks a
    top-level ``SKILL.md`` is annotated ``(no SKILL.md)`` so a malformed or
    set-style entry is still visible. A broken symlink is annotated
    ``(unreadable)``. Absent skills dir → ``0 skills``. Never raises.
    """
    skills_dir = Path(home_dir) / ".claude" / "skills"
    name = getattr(config, "name", "?")
    if not skills_dir.is_dir():
        logger.info(
            "skills: agent %s — no %s (0 skills materialized)", name, skills_dir
        )
        return
    entries: list[str] = []
    for child in sorted(skills_dir.iterdir()):
        try:
            if not child.is_dir():
                continue
        except OSError:  # stx-allow: fallback (broken symlink → report as such)
            entries.append(f"{child.name}(unreadable)")
            continue
        has_skill_md = (child / "SKILL.md").is_file()
        entries.append(child.name if has_skill_md else f"{child.name}(no SKILL.md)")
    logger.info(
        "skills: agent %s — %d skill dir(s) under %s: %s",
        name,
        len(entries),
        skills_dir,
        ", ".join(entries) or "(none)",
    )


__all__ = ["log_effective_skills"]
