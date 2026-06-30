"""Tests for host ``~/.claude/commands/*.md`` baseline deploy.

The operator authors a Claude Code slash command ONCE in the standard host
location ``~/.claude/commands/`` and it must propagate into every agent's
materialized ``.claude/commands/``. Host commands are the LOWEST baseline, so
a same-name per-agent / bundled command wins.

PA-306 no-mocks: real ``tmp_path`` directories. ``$HOME`` is steered to a fake
host home via the shared ``env_save_restore`` fixture (no monkeypatch) so
``Path("~/.claude/commands").expanduser()`` resolves under test control.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.runtimes._host_commands import (
    deploy_host_claude_commands,
)
from scitex_agent_container.runtimes._to_home import materialize_to_home


def _seed_host_commands(fake_home: Path, env_save_restore) -> Path:
    """Point ``$HOME`` at ``fake_home`` and create its ``.claude/commands/``."""
    env_save_restore.set("HOME", str(fake_home))
    cmds = fake_home / ".claude" / "commands"
    cmds.mkdir(parents=True, exist_ok=True)
    return cmds


class TestDeployHostClaudeCommands:
    def test_host_command_lands_in_agent_commands(
        self, tmp_path, env_save_restore
    ):
        # Arrange — a fake host ~/.claude/commands/foo.md.
        host_cmds = _seed_host_commands(tmp_path / "host_home", env_save_restore)
        (host_cmds / "foo.md").write_text("# /foo\nrun foo\n")
        agent_home = tmp_path / "agent_home"
        # Act
        deploy_host_claude_commands(agent_home)
        # Assert
        landed = agent_home / ".claude" / "commands" / "foo.md"
        assert landed.read_text() == "# /foo\nrun foo\n"

    def test_overwrites_read_only_destination(self, tmp_path, env_save_restore):
        # Arrange — deploy once, then make the landed file read-only (0444) and
        # change the host source (mirrors a 0444 source mode preserved across
        # deploys, which aborted a restart and left the agent DOWN).
        host_cmds = _seed_host_commands(tmp_path / "host_home", env_save_restore)
        (host_cmds / "foo.md").write_text("v1\n")
        agent_home = tmp_path / "agent_home"
        deploy_host_claude_commands(agent_home)
        landed = agent_home / ".claude" / "commands" / "foo.md"
        landed.chmod(0o444)
        (host_cmds / "foo.md").write_text("v2\n")
        # Act — must overwrite the read-only destination, not raise.
        deploy_host_claude_commands(agent_home)
        # Assert
        assert landed.read_text() == "v2\n"

    def test_skip_if_missing_fabricates_no_commands_dir(
        self, tmp_path, env_save_restore
    ):
        # Arrange — host home with NO .claude/commands/ dir.
        env_save_restore.set("HOME", str(tmp_path / "bare_host_home"))
        agent_home = tmp_path / "agent_home"
        # Act — must be a no-op (no error).
        deploy_host_claude_commands(agent_home)
        # Assert — no commands dir fabricated in the agent home.
        assert not (agent_home / ".claude" / "commands").exists()

    def test_non_md_host_entry_is_skipped(self, tmp_path, env_save_restore):
        # Arrange — a non-.md archive sits beside no .md files.
        host_cmds = _seed_host_commands(tmp_path / "host_home", env_save_restore)
        (host_cmds / "archive-old.tar.gz").write_bytes(b"\x00")
        agent_home = tmp_path / "agent_home"
        # Act
        deploy_host_claude_commands(agent_home)
        # Assert — the non-.md entry did not land.
        assert not (
            agent_home / ".claude" / "commands" / "archive-old.tar.gz"
        ).exists()


class TestHostCommandPrecedenceViaMaterialize:
    def test_per_agent_command_wins_over_host(self, tmp_path, env_save_restore):
        # Arrange — host /foo.md AND a per-agent to_home/.claude/commands/foo.md.
        host_cmds = _seed_host_commands(tmp_path / "host_home", env_save_restore)
        (host_cmds / "foo.md").write_text("HOST VERSION\n")
        spec_dir = tmp_path / "spec"
        per_agent = spec_dir / "to_home" / ".claude" / "commands"
        per_agent.mkdir(parents=True)
        (per_agent / "foo.md").write_text("AGENT VERSION\n")
        agent_home = tmp_path / "agent_home"
        # Act
        materialize_to_home(spec_dir, agent_home)
        # Assert — the per-agent command overwrites the host one.
        landed = agent_home / ".claude" / "commands" / "foo.md"
        assert landed.read_text() == "AGENT VERSION\n"
