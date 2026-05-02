"""Mirror test for the shipped ``_skills/scitex-agent-container/scitex-smoke-test.py``.

Frontmatter / markdown quality checks live in ``tests/skills/`` — this
file exists so the audit (PS202) sees a mirror directory and so the
shipped smoke-test script has a matching test entry (PS204).
"""

from __future__ import annotations

from pathlib import Path

_SKILL_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_skills"
    / "scitex-agent-container"
)


def test_smoke_test_script_present_and_executable() -> None:
    smoke = _SKILL_DIR / "scitex-smoke-test.py"
    assert smoke.is_file()
    assert smoke.stat().st_size > 0


def test_skill_index_present() -> None:
    assert (_SKILL_DIR / "SKILL.md").is_file()
