"""Aggregate an agent's resolved skills content into its CLAUDE.md.

Operator guarantee (2026-06-20, routed via paper-scitex-clew handoff item 5):
skill-chaining (skills that reference other skills) and ``startup_prompts``
are both unreliable triggers, so the strongest enforcement is to inline the
agent's actual skill content into its ``CLAUDE.md`` at deploy time — CLAUDE.md
is always-on context that the harness loads every turn.

This module is called from :func:`_to_home.deploy_to_home` (and the overlay
twin) AFTER ``to_home`` materialization, so the skills the agent will actually
see — the real ``*.md`` files under ``<workspace_home>/.claude/skills/`` (v3:
skills are materialized there, not bind-mounted; see skill
``25_claude-setup-delivery``) — are already on disk.

The aggregated content is wrapped between idempotent markers::

    <!-- sac:skills:start -->
    ...inlined skill bodies...
    <!-- sac:skills:end -->

Re-deploys REPLACE the block cleanly: the writer strips any existing
``sac:skills`` block first, then appends a fresh one. This stays correct even
though ``CLAUDE.md`` is *also* marker-protected by
:func:`_to_home._deploy_marker_protected` (whose ``Start/End`` section is a
separate, independently-managed marker pair): the strip-then-append keeps
exactly one skills block no matter how the user-tail carries it forward.

Extracted into its own module (vs. folding into ``_to_home.py``, already at
the ~512-line file ceiling) per the repo's sibling-module convention.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Idempotent skills-aggregation markers. Distinct from the CLAUDE.md
# Start/End generated-section markers in _to_home_text.py.
SKILLS_START_MARKER = "<!-- sac:skills:start -->"
SKILLS_END_MARKER = "<!-- sac:skills:end -->"

# Subtrees we never treat as skill content.
_SKIP_DIRS = frozenset({"__pycache__", "GITIGNORED", ".git"})

# CLAUDE.md basenames Claude Code reads, relative to the workspace home.
# Both are honoured: the root file is the SAC convention (see
# 25_claude-setup-delivery), and ``.claude/CLAUDE.md`` is the SDK's own
# user-memory path. We aggregate into whichever already exist; if NEITHER
# exists but skills do, we create the root one (the canonical target).
_CLAUDE_MD_RELPATHS = ("CLAUDE.md", ".claude/CLAUDE.md")


def _iter_skill_files(skills_dir: Path) -> list[Path]:
    """Return every ``*.md`` skill leaf under ``skills_dir`` (sorted).

    Skips hidden / generated subtrees. The top-level ``SKILL.md`` index is
    included like any other leaf so its routing content is inlined too.
    """
    if not skills_dir.is_dir():
        return []
    out: list[Path] = []
    for md in sorted(skills_dir.rglob("*.md")):
        rel_parts = md.relative_to(skills_dir).parts[:-1]
        if any(p in _SKIP_DIRS or p.startswith(".") for p in rel_parts):
            continue
        out.append(md)
    return out


def _build_skills_block(skills_dir: Path) -> str | None:
    """Render the marker-wrapped aggregated skills block, or None.

    Returns ``None`` when there are no skill files to inline (caller then
    only strips any stale block, never appends an empty one).
    """
    files = _iter_skill_files(skills_dir)
    if not files:
        return None

    parts: list[str] = [
        SKILLS_START_MARKER,
        "",
        "# Aggregated skills (auto-injected by scitex-agent-container)",
        "",
        "The skill content below is inlined from "
        "`~/.claude/skills/` at deploy time so it is ALWAYS in context — "
        "do not rely on `@`-import or skill-chaining to load it.",
        "",
    ]
    for md in files:
        rel = md.relative_to(skills_dir)
        try:
            body = md.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("skills-aggregate: skip unreadable %s (%s)", md, exc)
            continue
        parts.append(f"## skill: {rel}")
        parts.append("")
        parts.append(body)
        parts.append("")
    parts.append(SKILLS_END_MARKER)
    return "\n".join(parts).rstrip("\n") + "\n"


def _strip_existing_block(text: str) -> str:
    """Remove any existing ``sac:skills`` block from ``text``.

    Tolerant of a missing end marker (truncates from start marker to EOF) so
    a half-written prior block never duplicates. No markers → returns
    ``text`` unchanged.
    """
    start = text.find(SKILLS_START_MARKER)
    if start == -1:
        return text
    prefix = text[:start]
    end = text.find(SKILLS_END_MARKER, start)
    if end == -1:
        # Malformed/truncated: drop everything from the start marker on.
        suffix = ""
    else:
        suffix = text[end + len(SKILLS_END_MARKER) :]
    prefix = prefix.rstrip("\n")
    suffix = suffix.lstrip("\n")
    if prefix and suffix:
        joined = f"{prefix}\n{suffix}"
    else:
        joined = prefix or suffix
    if not joined:
        return ""
    return joined.rstrip("\n") + "\n"


def _apply_to_file(claude_md: Path, block: str) -> None:
    """Write ``block`` into ``claude_md`` (strip-then-append; idempotent)."""
    existing = claude_md.read_text(encoding="utf-8") if claude_md.is_file() else ""
    stripped = _strip_existing_block(existing)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    sep = "\n" if stripped.strip() else ""
    new_text = f"{stripped}{sep}{block}"
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    # CLAUDE.md may have been written read-only by a prior step; clear it.
    if claude_md.exists():
        import os
        import stat

        mode = claude_md.stat().st_mode
        if not mode & stat.S_IWUSR:
            os.chmod(claude_md, mode | stat.S_IWUSR)
    claude_md.write_text(new_text, encoding="utf-8")


def aggregate_skills_into_claudemd(workspace_home: Path) -> list[Path]:
    """Inline ``<workspace_home>/.claude/skills/`` content into CLAUDE.md.

    Forceful + idempotent: on every call the ``sac:skills`` block in each
    target CLAUDE.md is replaced with a freshly-rendered one. Targets every
    CLAUDE.md in :data:`_CLAUDE_MD_RELPATHS` that already exists; if none
    exists but skills are present, the root ``CLAUDE.md`` is created.

    No-op (returns ``[]``) when there are no skill files — but a stale block
    in an existing CLAUDE.md is still stripped so removing the last skill
    cleans up after itself.

    Returns the list of CLAUDE.md paths that were written.
    """
    skills_dir = workspace_home / ".claude" / "skills"
    block = _build_skills_block(skills_dir)

    targets = [
        workspace_home / rel
        for rel in _CLAUDE_MD_RELPATHS
        if (workspace_home / rel).is_file()
    ]

    if block is None:
        # No skills: only clean up an existing block, never create a file.
        written: list[Path] = []
        for tgt in targets:
            before = tgt.read_text(encoding="utf-8")
            after = _strip_existing_block(before)
            if after != before:
                _write_plain(tgt, after)
                written.append(tgt)
        return written

    if not targets:
        # Skills exist but no CLAUDE.md yet — create the canonical root one.
        targets = [workspace_home / _CLAUDE_MD_RELPATHS[0]]

    written = []
    for tgt in targets:
        _apply_to_file(tgt, block)
        written.append(tgt)
        logger.info(
            "skills-aggregate: inlined %d skill file(s) into %s",
            len(_iter_skill_files(skills_dir)),
            tgt,
        )
    return written


def _write_plain(claude_md: Path, text: str) -> None:
    """Write ``text`` clearing a read-only bit first (no block logic)."""
    import os
    import stat

    if claude_md.exists():
        mode = claude_md.stat().st_mode
        if not mode & stat.S_IWUSR:
            os.chmod(claude_md, mode | stat.S_IWUSR)
    claude_md.write_text(text, encoding="utf-8")


__all__ = [
    "SKILLS_END_MARKER",
    "SKILLS_START_MARKER",
    "aggregate_skills_into_claudemd",
]
