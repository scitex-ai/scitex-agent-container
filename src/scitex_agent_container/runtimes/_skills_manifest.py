"""Soft skill-manifest injection into the agent's ``system_prompt``.

Why this module exists
----------------------

``runtimes/_sdk_common.py::build_sdk_options`` pins ``setting_sources=[]``
for machine-independence — no host ``~/.claude`` auto-discovery. This is
the intentional doctrine (see ``_skills/scitex-agent-container/
25_claude-setup-delivery.md``). Side-effect: the SDK never enumerates
the skill frontmatter under ``$HOME/.claude/skills/``, so the agent
starts blind to which skills are mounted, and never volunteers to
invoke them via the ``Skill`` tool.

The fix is the SOFT manifest: append a bullet list of
``<name>: <description>`` + ``path`` to the system_prompt so the agent
KNOWS the skill exists and can lazy-load it on demand. **Bodies are
never inlined.** The whole point of the ``Skill`` tool is on-demand
load; surfacing the frontmatter is the minimum signal the agent needs
to discover the skill, without inflating the system_prompt with bodies
that may never be relevant for the conversation.

Per-agent allowlist surface
---------------------------

The list of skill names to advertise is the operator-curated
``metadata.labels.skills`` CSV on the agent's spec.yaml (parsed by
``config/_loaders.py`` into ``AgentConfig.labels``). The same CSV
also feeds the ``required_skills[]`` of the A2A AgentCard (see
``a2a/_card.py::project_card``), so the runtime and the protocol
surface stay consistent: the AgentCard advertises which skills the
agent claims, and the system_prompt manifest tells the agent which
skills it has.

Error model
-----------

This module raises NO exceptions. The system_prompt is best-effort
ADVISORY context — a broken skill mount must never crash agent start.
Failure modes degrade as follows:

* Empty ``skill_names`` → ``None`` (no manifest to inject; caller skips
  the append).
* Named skill has no SKILL.md → emit a bullet with the path only, no
  description. The operator SEES the gap in the agent's first turn
  rather than discovering it through silent missing-skill behaviour.
* SKILL.md frontmatter has no ``description`` field → emit a
  ``<no description>`` placeholder so the bullet still renders.
* SKILL.md frontmatter is malformed → degrade to the path-only bullet,
  same as a missing file. Same logic: the operator must see the gap.
"""

from __future__ import annotations

import pathlib
from typing import Iterable

import yaml

__all__ = ["build_skills_manifest_block"]


# The 240-char cap keeps the bullet on a single screen line for ~80-col
# terminals (the default Claude Code render width). Skill descriptions
# in the wild range from 80-300 chars; the cap trims runaways without
# truncating typical entries.
_DESCRIPTION_MAX_CHARS = 240


# Doctrine path inside the container. ``deploy_to_home`` materializes
# every per-agent skill at exactly this prefix inside the container
# ``$HOME``, so the agent can hand this path verbatim to the ``Skill``
# tool regardless of which host runs the apptainer.
_SKILLS_HOME_PREFIX = "~/.claude/skills"


# The LOCKED block heading. ``build_sdk_options``'s idempotency check
# greps for this exact literal to decide whether the manifest has
# already been appended — keep it stable.
_BLOCK_HEADING = "## Available skills"


_PREAMBLE = (
    "The following skills are mounted at ~/.claude/skills/. Invoke a "
    "skill via the Skill tool when needed — do NOT load full content "
    "into context."
)


def _extract_description_first_paragraph(description: object) -> str | None:
    """Return the leading paragraph of a SKILL.md ``description`` field.

    SKILL.md frontmatter ``description`` is typically a YAML multiline
    block with the scitex ``[WHAT]/[WHEN]/[HOW]`` structure — e.g.::

        description: |
          [WHAT] Main summary line.
          [WHEN] When to use this skill.
          [HOW] How to invoke.

    The [WHAT] line is the canonical summary surface; the rest are
    body concerns the agent can fetch via the ``Skill`` tool. We
    prefer the [WHAT] line when present, otherwise the first non-empty
    line of the field, then truncate to ``_DESCRIPTION_MAX_CHARS``.

    Returns ``None`` when the description is unset or empty so the
    caller can substitute the ``<no description>`` placeholder.
    """
    if not isinstance(description, str):
        return None
    stripped = description.strip()
    if not stripped:
        return None

    # Scan for a leading [WHAT] line — possibly indented (YAML
    # multiline preserves leading whitespace inside the block).
    lines = [line.strip() for line in stripped.splitlines()]
    for line in lines:
        if line.startswith("[WHAT]"):
            return _truncate(line)

    # No [WHAT] marker — first non-empty line wins.
    for line in lines:
        if line:
            return _truncate(line)
    return None


def _truncate(text: str) -> str:
    """Cap a description line at ``_DESCRIPTION_MAX_CHARS``.

    Truncation includes a trailing ellipsis so the operator KNOWS the
    line was cut. The cap counts the ellipsis toward the budget so the
    rendered string never exceeds the limit.
    """
    if len(text) <= _DESCRIPTION_MAX_CHARS:
        return text
    # Reserve 1 char for the ellipsis so the total stays at the cap.
    return text[: _DESCRIPTION_MAX_CHARS - 1] + "…"


def _read_skill_frontmatter(skill_path: pathlib.Path) -> dict | None:
    """Parse a SKILL.md's YAML frontmatter into a dict.

    Returns ``None`` for ANY failure (missing file, malformed YAML,
    no frontmatter, frontmatter not a dict). Callers degrade to the
    path-only bullet form on ``None``.

    Frontmatter shape: starts at line 1 with ``---``, ends at the
    next ``---`` line. Everything in between is YAML. We deliberately
    do not import a heavyweight frontmatter parser — the format is
    simple and ``yaml.safe_load`` is enough.
    """
    if not skill_path.is_file():
        return None
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        # stx-allow: fallback (reason: filesystem hiccup must degrade
        # to a path-only bullet, not crash agent start — see module
        # docstring "Error model" section)
        return None

    if not text.startswith("---"):
        return None

    # Find the closing ``---`` on its own line.
    lines = text.splitlines()
    end_index: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_index = i
            break
    if end_index is None:
        return None

    yaml_text = "\n".join(lines[1:end_index])
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # stx-allow: fallback (reason: malformed frontmatter is treated
        # the same as a missing file — operator sees the path-only
        # bullet; see module docstring "Error model" section)
        return None

    if not isinstance(parsed, dict):
        return None
    return parsed


def _format_bullet(skill_name: str, frontmatter: dict | None) -> str:
    """Render a single skill bullet.

    The shape is LOCKED — two lines per bullet:

        - <name>: <description>
          path: ~/.claude/skills/<name>/SKILL.md

    The bullet ALWAYS renders, even when ``frontmatter`` is ``None``
    (missing/malformed SKILL.md); in that case the description slot
    carries ``<no description>`` so the operator sees the named gap.
    """
    path = f"{_SKILLS_HOME_PREFIX}/{skill_name}/SKILL.md"

    if frontmatter is None:
        description_line = "<no description>"
    else:
        description_line = (
            _extract_description_first_paragraph(frontmatter.get("description"))
            or "<no description>"
        )

    return f"- {skill_name}: {description_line}\n  path: {path}"


def build_skills_manifest_block(
    skill_names: list[str] | Iterable[str],
    skills_root: pathlib.Path,
) -> str | None:
    """Build a system_prompt-ready block listing available skills.

    For each name in ``skill_names``, walk
    ``skills_root/<name>/SKILL.md``, parse the YAML frontmatter,
    extract ``name`` + ``description``, and emit one bullet per skill.
    Returns a complete block ready to APPEND to a ``system_prompt``.

    Returns ``None`` when ``skill_names`` is empty — there is no
    manifest to inject. When all names resolve to missing files, the
    block STILL renders (the operator must see which skills are
    expected but absent on disk).

    The block shape (LOCKED — ``build_sdk_options`` greps the heading
    for idempotency):

        ## Available skills
        <preamble>

        - <name>: <description-first-paragraph>
          path: ~/.claude/skills/<name>/SKILL.md
        - <name>: ...

    Raises NO exceptions. See the module docstring "Error model"
    section for the degradation rules.

    Parameters
    ----------
    skill_names
        Operator-curated list of skill names to advertise (typically
        the per-agent ``metadata.labels.skills`` CSV split on ``,``).
        Order is preserved verbatim — operators control the order so
        the most relevant skill leads the list.

    skills_root
        Directory holding ``<name>/SKILL.md`` trees. In the container,
        this resolves to ``$HOME/.claude/skills``. Tests pass
        ``tmp_path`` directly so they don't need a real container.
    """
    names = [n for n in skill_names if n]
    if not names:
        return None

    bullets: list[str] = []
    for name in names:
        skill_path = skills_root / name / "SKILL.md"
        frontmatter = _read_skill_frontmatter(skill_path)
        bullets.append(_format_bullet(name, frontmatter))

    body = "\n".join(bullets)
    return f"{_BLOCK_HEADING}\n{_PREAMBLE}\n\n{body}\n"
