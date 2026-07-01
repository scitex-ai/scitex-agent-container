"""Tests for the curated host ``~/.claude/skills/<name>`` baseline deploy.

The operator's general dev-rule skill SETS (``ywatanabe`` and ``scitex``) live
under the standard host ``~/.claude/skills/`` and must propagate into every
agent's materialized ``.claude/skills/`` so agents load them. They are
symlinked (not copied) to the host skill's resolved real target. The allowlist
is curated — tool skills and ``secret`` / ``scitex-lead`` are NOT deployed.

PA-306 no-mocks: real ``tmp_path`` directories. ``$HOME`` is steered to a fake
host home via the shared ``env_save_restore`` fixture (no monkeypatch) so
``Path("~/.claude/skills/<name>").expanduser()`` resolves under test control.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.runtimes._host_skills import deploy_host_skills
from scitex_agent_container.runtimes._to_home import materialize_to_home


def _seed_host_skill(fake_home: Path, name: str, env_save_restore) -> Path:
    """Point ``$HOME`` at ``fake_home`` and create its ``.claude/skills/<name>``."""
    env_save_restore.set("HOME", str(fake_home))
    skill = fake_home / ".claude" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("# skill\n")
    return skill


class TestDeployHostSkills:
    def test_curated_host_skill_symlinked_into_agent_skills(
        self, tmp_path, env_save_restore
    ):
        # Arrange — a fake host ~/.claude/skills/ywatanabe/ (in the allowlist).
        host_skill = _seed_host_skill(
            tmp_path / "host_home", "ywatanabe", env_save_restore
        )
        agent_home = tmp_path / "agent_home"
        # Act
        deploy_host_skills(agent_home)
        # Assert — the agent skill resolves to the host skill dir.
        landed = agent_home / ".claude" / "skills" / "ywatanabe"
        assert landed.resolve() == host_skill.resolve()

    def test_name_not_present_on_host_is_not_deployed(
        self, tmp_path, env_save_restore
    ):
        # Arrange — host has only ``ywatanabe``; ``scitex`` is absent.
        _seed_host_skill(tmp_path / "host_home", "ywatanabe", env_save_restore)
        agent_home = tmp_path / "agent_home"
        # Act — default allowlist requests both, but scitex must not be fabricated.
        deploy_host_skills(agent_home)
        # Assert — the absent name did not land.
        assert not (agent_home / ".claude" / "skills" / "scitex").exists()

    def test_skip_if_missing_fabricates_no_skills_dir(
        self, tmp_path, env_save_restore
    ):
        # Arrange — host home with NO .claude/skills/ dir at all.
        env_save_restore.set("HOME", str(tmp_path / "bare_host_home"))
        agent_home = tmp_path / "agent_home"
        # Act — must be a no-op (no error).
        deploy_host_skills(agent_home)
        # Assert — no skills dir fabricated in the agent home.
        assert not (agent_home / ".claude" / "skills").exists()


class TestHostSkillNoClobberViaMaterialize:
    def test_existing_agent_skill_is_not_clobbered(
        self, tmp_path, env_save_restore
    ):
        # Arrange — host ywatanabe AND a per-agent to_home/.claude/skills/ywatanabe.
        _seed_host_skill(tmp_path / "host_home", "ywatanabe", env_save_restore)
        spec_dir = tmp_path / "spec"
        per_agent = spec_dir / "to_home" / ".claude" / "skills" / "ywatanabe"
        per_agent.mkdir(parents=True)
        (per_agent / "SKILL.md").write_text("AGENT SKILL\n")
        agent_home = tmp_path / "agent_home"
        # Act
        materialize_to_home(spec_dir, agent_home)
        # Assert — the per-agent skill content wins (host symlink did not clobber).
        landed = agent_home / ".claude" / "skills" / "ywatanabe" / "SKILL.md"
        assert landed.read_text() == "AGENT SKILL\n"
