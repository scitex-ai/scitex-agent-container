"""CLAUDE.md management for agent-container sections."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..config import AgentConfig

logger = logging.getLogger(__name__)


# ----------------------------- skill resolution -----------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TAGS_LINE_RE = re.compile(r"^tags:\s*\[([^\]]*)\]\s*$", re.MULTILINE)


def _add_dir_paths(config: AgentConfig) -> list[Path]:
    """Extract `--add-dir <path>` values from spec.claude.flags.

    Accepts all three CLI forms YAML producers use in the wild:
      * ``"--add-dir P"``  (single string, space-joined; common in
        hand-written yaml lists)
      * ``"--add-dir=P"``  (single token with =)
      * ``"--add-dir", "P"`` (two adjacent list items)

    Used as the search roots for skill resolution in at-import mode.
    """
    flags = list(getattr(config.claude, "flags", []) or [])
    out: list[Path] = []
    i = 0
    while i < len(flags):
        f = (flags[i] or "").strip()
        # Form 1: single space-joined token
        if f.startswith("--add-dir ") and len(f) > len("--add-dir "):
            out.append(Path(os.path.expanduser(f[len("--add-dir ") :].strip())))
            i += 1
            continue
        # Form 2: --add-dir=PATH
        if f.startswith("--add-dir="):
            out.append(Path(os.path.expanduser(f.split("=", 1)[1])))
            i += 1
            continue
        # Form 3: two adjacent list items
        if f == "--add-dir" and i + 1 < len(flags):
            out.append(Path(os.path.expanduser(flags[i + 1])))
            i += 2
            continue
        i += 1
    return out


_NAME_LINE_RE = re.compile(r"^name:\s*([^\s].*?)\s*$", re.MULTILINE)


def _read_frontmatter(md: Path) -> str | None:
    """Return the raw frontmatter block (between ``---`` markers), or None."""
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = re.sub(r"\A<!--.*?-->\s*", "", text, count=1, flags=re.DOTALL)
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def _file_tags(md: Path) -> list[str]:
    """Read frontmatter `tags:` from a markdown file (regex; no PyYAML dep)."""
    fm = _read_frontmatter(md)
    if fm is None:
        return []
    tm = _TAGS_LINE_RE.search(fm)
    if not tm:
        return []
    return [t.strip().strip("\"'") for t in tm.group(1).split(",") if t.strip()]


def _file_frontmatter_name(md: Path) -> str | None:
    """Read frontmatter ``name:`` value, if present."""
    fm = _read_frontmatter(md)
    if fm is None:
        return None
    nm = _NAME_LINE_RE.search(fm)
    if not nm:
        return None
    return nm.group(1).strip().strip("\"'")


def _walk_md(root: Path):
    """Yield .md files under root, following symlinks. Skip hidden + GITIGNORED."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d not in {"GITIGNORED", "__pycache__"}
        ]
        for fn in filenames:
            if fn.endswith(".md"):
                yield Path(dirpath) / fn


def _default_skill_roots() -> list[Path]:
    """Default Claude Code skill roots (used when yaml has no --add-dir)."""
    p = Path.home() / ".claude" / "skills"
    return [p] if p.is_dir() else []


def _matches(value: str, candidate: str, style: str) -> bool:
    """Compare ``value`` to ``candidate`` per the configured match style."""
    if not candidate:
        return False
    if style == "partial":
        return value in candidate
    return value == candidate  # exact


def _resolve_skill(
    name: str,
    roots: list[Path],
    strategies: list[str] | None = None,
    style: str = "exact",
) -> list[Path]:
    """Resolve a skill name to absolute markdown paths.

    ``strategies`` controls which match strategies run; results are
    unioned and de-duplicated by canonical path.

    Strategies:
      * ``"skill-id"`` — Anthropic-canonical. Walk ``<root>/.../<dir>/SKILL.md``
        at any depth; identity = ``frontmatter.name`` (if present)
        ELSE ``<dir>.name``. Match if identity matches ``name`` per
        ``style``. Per spec: ``name`` is optional and defaults to dir
        name.
      * ``"tag"`` — files whose frontmatter ``tags:`` contains a value
        matching ``name`` per ``style``.
      * ``"filename"`` — files whose basename (without ``.md``) matches
        ``name`` per ``style`` (broader; opt-in).

    If ``roots`` is empty, falls back to ``~/.claude/skills/`` (Claude
    Code's default skill location).
    """
    if strategies is None:
        strategies = ["skill-id", "tag"]
    if not roots:
        roots = _default_skill_roots()
    matches: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        if rp not in seen:
            seen.add(rp)
            matches.append(rp)

    skill_id_hits: list[Path] = []  # collected for dedup-warn

    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") and d not in {"GITIGNORED", "__pycache__"}
            ]

            # Strategy: skill-id (Anthropic canonical)
            if "skill-id" in strategies and "SKILL.md" in filenames:
                skill_md = Path(dirpath) / "SKILL.md"
                identity = _file_frontmatter_name(skill_md) or Path(dirpath).name
                if _matches(name, identity, style):
                    skill_id_hits.append(skill_md)

            # Strategy: filename — any .md with basename matching name
            if "filename" in strategies:
                for fn in filenames:
                    if fn.endswith(".md") and _matches(name, fn[:-3], style):
                        _add(Path(dirpath) / fn)

            # Strategy: tag — frontmatter tags scan
            if "tag" in strategies:
                for fn in filenames:
                    if not fn.endswith(".md"):
                        continue
                    md = Path(dirpath) / fn
                    if any(_matches(name, t, style) for t in _file_tags(md)):
                        _add(md)

    # ``skill-id`` is supposed to be a unique identity per Anthropic
    # spec. Multiple hits mean the user has duplicate skill dirs (or
    # frontmatter ``name`` collisions). Keep the first hit (deterministic
    # walk order) and warn so the duplicate can be resolved upstream.
    if skill_id_hits:
        first = skill_id_hits[0]
        _add(first)
        if len(skill_id_hits) > 1:
            logger.warning(
                "skill %r matched %d skill-id candidates; using %s (first). "
                "Other matches: %s",
                name,
                len(skill_id_hits),
                first,
                [str(p) for p in skill_id_hits[1:]],
            )

    return sorted(matches)


def build_skills_lines(config: AgentConfig) -> list[str]:
    """Build the Required/Available skills section as a list of markdown lines.

    Mode controlled by ``config.skills.injection_mode``:

    * ``"block"`` (default) — emit ```skills <name> ``` fenced blocks.
    * ``"at-import"`` — emit ``@<absolute-path>`` lines so Claude Code
      inlines the skill files at session start. Path resolution prefers
      ``<root>/<name>/SKILL.md`` and falls back to a frontmatter
      ``tags:`` scan; unresolved names emit a visible
      ``<!-- skill 'X': not resolved -->`` placeholder + a WARNING log.

    Returned list is appendable into either the v1 ``.claude/CLAUDE.md``
    auto-section or the v2 main ``CLAUDE.md`` (between Start/End markers).
    """
    mode = getattr(config.skills, "injection_mode", "block")
    roots = _add_dir_paths(config) if mode == "at-import" else []
    strategies = list(getattr(config.skills, "match_by", ["skill-id", "tag"]))
    style = getattr(config.skills, "match_style", "exact")
    lines: list[str] = []

    def _emit(heading: str, intro: str, names: list[str]) -> None:
        if not names:
            return
        lines.append(heading)
        lines.append(intro)
        if mode == "at-import":
            for name in names:
                paths = _resolve_skill(name, roots, strategies, style)
                if not paths:
                    logger.warning(
                        "skill %r not found under --add-dir roots %s "
                        "(checked name-as-dir <root>/<name>/SKILL.md and "
                        "frontmatter tag match); injection skipped",
                        name,
                        [str(r) for r in roots],
                    )
                    lines.append(f"<!-- skill {name!r}: not resolved -->")
                    continue
                for p in paths:
                    lines.append(f"@{p}")
        else:
            lines.append("```skills")
            for name in names:
                lines.append(name)
            lines.append("```")
        lines.append("")

    _emit(
        "### Required Skills", "Load these skills at startup:", config.skills.required
    )
    _emit(
        "### Available Skills",
        "These skills can be used when needed:",
        config.skills.available,
    )
    return lines


def setup_claude_md(config: AgentConfig, workdir: str) -> None:
    """Generate and inject an agent-container section into CLAUDE.md.

    Uses HTML comment tags to delimit the managed section so it can be
    updated or removed without touching user content.
    """
    claude_dir = Path(workdir) / ".claude"
    claude_md = claude_dir / "CLAUDE.md"

    existing = ""
    if claude_md.exists():
        existing = claude_md.read_text()

    agent_id = config.name
    role = config.env.get("SCITEX_AGENT_CONTAINER_ROLE", config.labels.get("role", ""))
    agent_env_id = config.env.get("SCITEX_AGENT_CONTAINER_ID", config.name)

    lines = [
        f'<!-- agent-container:start id="{agent_id}" -->',
        f"## Agent: {agent_id} (auto-generated by scitex-agent-container)",
        "",
    ]

    lines.extend(build_skills_lines(config))

    lines.append("### Agent Role")
    if role:
        lines.append(f"- Role: {role}")
    lines.append(f"- ID: {agent_env_id}")
    lines.append(f'<!-- agent-container:end id="{agent_id}" -->')

    section = "\n".join(lines)

    pattern = (
        rf'<!-- agent-container:start id="{re.escape(agent_id)}" -->.*?'
        rf'<!-- agent-container:end id="{re.escape(agent_id)}" -->'
    )
    if re.search(pattern, existing, re.DOTALL):
        updated = re.sub(pattern, section, existing, flags=re.DOTALL)
    else:
        separator = (
            "\n\n"
            if existing and not existing.endswith("\n\n")
            else ("\n" if existing and not existing.endswith("\n") else "")
        )
        updated = existing + separator + section + "\n"

    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_md.write_text(updated)
    logger.info("CLAUDE.md updated for agent %s at %s", agent_id, claude_md)


def cleanup_claude_md(config: AgentConfig, workdir: str) -> None:
    """Remove the agent-container section from CLAUDE.md."""
    claude_md = Path(workdir) / ".claude" / "CLAUDE.md"
    if not claude_md.exists():
        return

    existing = claude_md.read_text()
    agent_id = config.name

    pattern = (
        rf'\n*<!-- agent-container:start id="{re.escape(agent_id)}" -->.*?'
        rf'<!-- agent-container:end id="{re.escape(agent_id)}" -->\n?'
    )
    updated = re.sub(pattern, "", existing, flags=re.DOTALL)

    if updated != existing:
        claude_md.write_text(updated)
        logger.info("CLAUDE.md cleaned up for agent %s at %s", agent_id, claude_md)
