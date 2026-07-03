"""Tests for the boot-time effective-skills diagnostic log.

PA-306 no-mocks: real ``AgentConfig`` + real ``tmp_path`` skill dirs, real
``caplog``. The log is a pure diagnostic — it must list the materialized skill
dir names at INFO and never raise.
"""

from __future__ import annotations

import logging

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._skills_boot_log import log_effective_skills

_LOGGER = "scitex_agent_container.runtimes._skills_boot_log"


def test_logs_present_skill_names(tmp_path, caplog):
    # Arrange
    sk = tmp_path / ".claude" / "skills" / "scitexification"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: scitexification\n---\n")
    cfg = AgentConfig(name="solver-1")
    # Act
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_effective_skills(cfg, tmp_path)
    # Assert
    assert any("scitexification" in r.getMessage() for r in caplog.records)


def test_absent_skills_dir_logs_zero(tmp_path, caplog):
    # Arrange — no .claude/skills/ at all.
    cfg = AgentConfig(name="empty-agent")
    # Act
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_effective_skills(cfg, tmp_path)
    # Assert
    assert any("0 skills" in r.getMessage() for r in caplog.records)


def test_dir_without_skill_md_is_annotated(tmp_path, caplog):
    # Arrange — a skill dir missing its SKILL.md.
    (tmp_path / ".claude" / "skills" / "half").mkdir(parents=True)
    cfg = AgentConfig(name="a")
    # Act
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_effective_skills(cfg, tmp_path)
    # Assert
    assert any("no SKILL.md" in r.getMessage() for r in caplog.records)


def test_never_raises_on_missing_home(tmp_path, caplog):
    # Arrange — a home dir that does not exist at all.
    cfg = AgentConfig(name="a")
    # Act — a diagnostic must not abort a start; reaching the assert proves
    # no exception was raised for a nonexistent home.
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_effective_skills(cfg, tmp_path / "does-not-exist")
    # Assert
    assert any("0 skills" in r.getMessage() for r in caplog.records)
