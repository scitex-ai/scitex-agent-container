"""CLAUDE.md skills-block parser.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports ``_parse_skills`` so existing
``agent_meta._parse_skills`` access keeps working.
"""

from __future__ import annotations

import re
from pathlib import Path


def _parse_skills(workdir: str) -> list[str]:
    """Parse ```skills fenced code block from workspace CLAUDE.md."""
    skills: list[str] = []
    # stx-allow: fallback (reason: CLAUDE.md may be absent or unreadable;
    # empty skills list is an acceptable result for unconfigured agents)
    try:
        cmd = Path(workdir) / "CLAUDE.md"
        if cmd.is_file():
            text = cmd.read_text()
            for block in re.findall(r"```skills\n(.*?)\n```", text, re.DOTALL):
                for ln in block.splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        skills.append(ln)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass
    return skills
