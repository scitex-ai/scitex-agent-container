"""F-CS1: claude-session must materialise CLAUDE.md with hard/soft skills.

These tests pin the contract the F-CS1 feature request specifies:

* ``spec.skills.required[]`` — HARD mode → ``@<absolute path>`` lines
  in CLAUDE.md so the SDK inlines the skill content at session start.
* ``spec.skills.available[]`` — SOFT mode → a reference listing
  (``- <name>: <path>``) with NO ``@-import``; the agent reads on demand.

The runtime materialises CLAUDE.md via ``_setup_workspace`` before
spawning the SDK runner subprocess, and tears it down via
``_cleanup_workspace`` on stop. Both helpers wrap the existing
``runtimes.claude_md`` primitives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec, SkillsSpec
from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime


def _make_skill(root: Path, name: str, body: str = "skill body") -> Path:
    """Create ``<root>/<name>/SKILL.md`` with a frontmatter ``name:`` line.

    Mirrors the canonical Anthropic skill layout that ``_resolve_skill``
    consumes (``skill-id`` strategy walks ``<root>/.../<dir>/SKILL.md``
    and matches frontmatter ``name:`` ELSE the dir name).
    """
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(f"---\nname: {name}\n---\n\n{body}\n")
    return md


@pytest.fixture
def skill_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a temp skill-root layout with one HARD and one SOFT skill."""
    root = tmp_path / "skills"
    hard_md = _make_skill(root, "f-cs1-hard-skill", "load me eagerly")
    soft_md = _make_skill(root, "f-cs1-soft-skill", "read me on demand")
    return root, hard_md, soft_md


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Per-agent workdir where ``.claude/CLAUDE.md`` will land."""
    wd = tmp_path / "agent-workdir"
    wd.mkdir()
    return wd


def _make_config(workdir: Path, skill_root: Path) -> AgentConfig:
    """Real AgentConfig declaring both required + available skills.

    The ``--add-dir`` flag tells ``_resolve_skill`` where to look for
    skill markdown files (otherwise it falls back to ``~/.claude/skills``
    which the test suite must not depend on).
    """
    return AgentConfig(
        name="f-cs1-agent",
        runtime="claude-session",
        workdir=str(workdir),
        skills=SkillsSpec(
            required=["f-cs1-hard-skill"],
            available=["f-cs1-soft-skill"],
            injection_mode="at-import",
        ),
        claude=ClaudeSpec(flags=[f"--add-dir={skill_root}"]),
    )


class TestSetupWorkspace:
    """``_setup_workspace`` writes CLAUDE.md with hard + soft sections."""

    def test_required_skill_emits_at_import_line(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """HARD mode: required skill resolves to ``@<absolute path>`` line."""
        skill_root, hard_md, _soft_md = skill_roots
        config = _make_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        claude_md = workdir / ".claude" / "CLAUDE.md"
        assert claude_md.exists(), "CLAUDE.md must be created on setup"
        text = claude_md.read_text()

        # HARD: required skill emits an @-import line.
        assert f"@{hard_md}" in text, (
            "Required skill must materialise as '@<absolute path>' so the "
            "SDK inlines its content at session start (F-CS1 hard mode)."
        )

    def test_available_skill_emits_soft_listing_without_at_import(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """SOFT mode: available skill listed by name+path; no ``@<path>``."""
        skill_root, _hard_md, soft_md = skill_roots
        config = _make_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        claude_md = workdir / ".claude" / "CLAUDE.md"
        text = claude_md.read_text()

        # SOFT: available skill must NOT be eagerly @-imported.
        assert f"@{soft_md}" not in text, (
            "Available skill must be SOFT — no '@<path>' line (would defeat "
            "the lazy/reference-only contract in F-CS1)."
        )
        # SOFT: but it MUST appear as a reference listing (name + path)
        # so the agent knows the skill exists and where to read it.
        assert "### Available Skills" in text
        assert "f-cs1-soft-skill" in text
        assert str(soft_md) in text, (
            "Available skill's resolved path must still be visible in "
            "CLAUDE.md (just not @-imported)."
        )

    def test_managed_section_markers_present(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """The agent-container section is delimited by stable HTML markers."""
        skill_root, _hard, _soft = skill_roots
        config = _make_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        text = (workdir / ".claude" / "CLAUDE.md").read_text()
        assert '<!-- agent-container:start id="f-cs1-agent" -->' in text
        assert '<!-- agent-container:end id="f-cs1-agent" -->' in text

    def test_remote_config_skips_workspace_setup(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """Remote agents materialise CLAUDE.md on the remote host, not here.

        ``_setup_workspace`` short-circuits when ``config.remote.is_remote``
        is True so we don't write CLAUDE.md into a workdir that maps to a
        path on a different machine.
        """
        skill_root, _hard, _soft = skill_roots
        config = _make_config(workdir, skill_root)
        # Mark the config as remote.
        config.remote.host = "some-remote-box"
        # ``RemoteSpec.is_remote`` is a property derived from ``.host``, so
        # this is enough; no need to monkey-patch the dataclass.
        assert config.remote.is_remote is True

        ClaudeSessionRuntime()._setup_workspace(config)

        # No local CLAUDE.md should have been written for a remote agent.
        assert not (workdir / ".claude" / "CLAUDE.md").exists()


class TestCleanupWorkspace:
    """``_cleanup_workspace`` removes the managed section."""

    def test_cleanup_strips_managed_section(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        skill_root, _hard, _soft = skill_roots
        config = _make_config(workdir, skill_root)

        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        claude_md = workdir / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        # Sanity: section was added.
        before = claude_md.read_text()
        assert "agent-container:start" in before

        runtime._cleanup_workspace(config)

        # File may still exist (cleanup_claude_md only strips the managed
        # block, preserving any user content) but our markers must be gone.
        if claude_md.exists():
            after = claude_md.read_text()
            assert "agent-container:start" not in after
            assert "f-cs1-hard-skill" not in after
            assert "f-cs1-soft-skill" not in after

    def test_cleanup_preserves_user_content(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """User-authored content above/below our markers must survive cleanup."""
        skill_root, _hard, _soft = skill_roots
        # Pre-seed CLAUDE.md with user content.
        claude_dir = workdir / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nuser stuff above\n")

        config = _make_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        # Verify user content + agent block coexist.
        text = claude_md.read_text()
        assert "# My Project" in text and "user stuff above" in text
        assert "agent-container:start" in text

        runtime._cleanup_workspace(config)

        after = claude_md.read_text()
        assert "# My Project" in after
        assert "user stuff above" in after
        assert "agent-container:start" not in after
