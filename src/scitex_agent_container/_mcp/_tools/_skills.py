"""``sac skills ...`` tools (F-CS15) — Python API + MCP wrappers.

Required pair per scitex MCP convention §5: every package exposes
``<pkg>_skills_list`` and ``<pkg>_skills_get`` so an agent can
introspect and load the package's own skill markdown without reading
the filesystem itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SKILLS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "_skills" / "scitex-agent-container"
)


def skills_list() -> dict[str, Any]:
    """Enumerate the markdown skill files this package ships under
    ``_skills/scitex-agent-container/``.

    Returns a flat list of ``{name, path, description}`` entries.
    ``description`` is the YAML-frontmatter ``description`` field
    when present, else the first non-blank line of the file.
    """
    skills = []
    if not _SKILLS_ROOT.is_dir():
        return {"count": 0, "skills": []}
    for md in sorted(_SKILLS_ROOT.glob("*.md")):
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
    extension. Returns ``{"name", "path", "content"}`` when found,
    or ``{"error", "name", "available"}`` when not.
    """
    target = _SKILLS_ROOT / f"{name}.md"
    if not target.is_file():
        return {
            "error": "skill not found",
            "name": name,
            "available": [p.stem for p in sorted(_SKILLS_ROOT.glob("*.md"))],
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
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if line.lstrip().startswith("description:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s
    return ""


__all__ = ["skills_list", "skills_get", "register_skills_tools"]
