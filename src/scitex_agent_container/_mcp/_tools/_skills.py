"""``sac dev skills ...`` tools (F-CS15) — Python API + MCP wrappers.

Required pair per scitex MCP convention §5: every package exposes
``<pkg>_skills_list`` and ``<pkg>_skills_get`` so an agent can
introspect and load the package's own skill markdown without reading
the filesystem itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_SKILLS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "_skills" / "scitex-agent-container"
)


def _skill_files() -> list[Path]:
    """Admit direct skill files whose resolved target stays in the skill root."""
    root = _SKILLS_ROOT.resolve()
    return [
        path
        for path in sorted(_SKILLS_ROOT.glob("*.md"))
        if path.is_file() and path.resolve().parent == root
    ]


def skills_list() -> dict[str, Any]:
    """Enumerate the markdown skill files this package ships under
    ``_skills/scitex-agent-container/``.

    Returns a flat list of ``{name, path, description}`` entries.
    ``description`` is the YAML-frontmatter ``description`` field
    when it is a string (including YAML block scalars), else the first
    non-blank, non-heading body line. Surrounding whitespace is stripped.
    """
    skills = []
    if not _SKILLS_ROOT.is_dir():
        return {"count": 0, "skills": []}
    for md in _skill_files():
        text = md.read_text(encoding="utf-8", errors="replace")
        skills.append(
            {
                "name": md.stem,
                "path": str(md),
                "description": _extract_description(text),
            }
        )
    return {"count": len(skills), "skills": skills}


def skills_get(name: str) -> dict[str, Any]:
    """Return the full text of a sac skill file by stem name (e.g.
    ``"02_quick-start"``). Reverse of :func:`skills_list`.

    ``name`` is matched against ``<stem>``; pass without the ``.md``
    extension. Only listed files resolving inside the skill directory are
    admitted; paths and external symlinks are not skill names.
    Returns ``{"name", "path", "content"}`` when found,
    or ``{"error", "name", "available"}`` when not.
    """
    files = _skill_files()
    target = next((path for path in files if path.stem == name), None)
    if target is None:
        return {
            "error": "skill not found",
            "name": name,
            "available": [path.stem for path in files],
        }
    return {
        "name": name,
        "path": str(target),
        "content": target.read_text(encoding="utf-8"),
    }


def register_skills_tools(mcp) -> None:
    for fn in (skills_list, skills_get):
        mcp.tool()(fn)


def _extract_description(text: str) -> str:
    block = re.match(
        r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL
    )
    if block is not None:
        import yaml

        try:
            metadata = yaml.safe_load(block.group(1))
        except yaml.YAMLError:
            metadata = None
        if isinstance(metadata, dict):
            description = metadata.get("description")
            if isinstance(description, str):
                return description.strip()
        text = text[block.end() :]
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s
    return ""


__all__ = ["skills_list", "skills_get", "register_skills_tools"]
