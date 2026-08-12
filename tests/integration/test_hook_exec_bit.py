"""A hook sac ARMS must be a hook sac can RUN.

The second way a merged, tested, correct hook lands inert: ``settings.json``
arms it by bare path, Claude Code execs that path directly, and the file has no
execute bit. Perfect bytes, right location, dead. Every content comparison
calls it current, and ``git diff`` never shows a mode.

So the assertions here are deliberately written to be FALSE for a file that a
content check would call fine. Each fixture plants correct content with the
wrong mode — the exact state measured in the dotfiles baseline, where 18 of 43
bare-path-armed hooks are tracked ``100644``.

Method borrowed from the dotfiles agent: verify PER COPY, AFTER PLACEMENT.
Identical-before is not identical-after; the mode bit is what proved it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._baseline_hook_assets import (
    HOOKS_RELPATH,
    deploy_baseline_hook_assets,
    hook_asset_plan,
)
from scitex_agent_container.runtimes._hook_exec_bit import (
    HOOK_MODE,
    armed_bare_path_commands,
    ensure_armed_hooks_executable,
    is_executable,
)
from scitex_agent_container.runtimes._to_home import deploy_to_home

PROBE_HOOK = "enforce_telegram_no_bare_issue.sh"
RUNNABLE = "#!/bin/bash\nexit 0\n"


def write_settings(home: Path, commands: "list[str]") -> Path:
    """Write a settings.json arming ``commands`` under PreToolUse."""
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": c} for c in commands
                            ],
                        }
                    ]
                }
            }
        )
    )
    return settings


def plant(home: Path, name: str, *, mode: int) -> Path:
    """Plant a runnable script at the armed path with an explicit mode."""
    p = home / ".claude" / "hooks" / "pre-tool-use" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(RUNNABLE)
    os.chmod(p, mode)
    return p


@pytest.fixture
def home_with_unrunnable_hook(tmp_path: Path) -> Path:
    """An agent home arming a bare-path hook that has NO execute bit."""
    home = tmp_path / "home"
    hook = plant(home, "guard.sh", mode=0o644)
    write_settings(home, ["$HOME/.claude/hooks/pre-tool-use/guard.sh"])
    if is_executable(hook):
        pytest.fail("precondition not false: planted hook is already executable")
    return home


class TestArmedHookIsMadeRunnable:
    def test_bare_path_hook_gains_the_execute_bit(self, home_with_unrunnable_hook):
        # Arrange
        hook = home_with_unrunnable_hook / HOOKS_RELPATH / "guard.sh"
        # Act
        ensure_armed_hooks_executable(home_with_unrunnable_hook)
        # Assert
        assert is_executable(hook) is True

    def test_repaired_hook_actually_executes(self, home_with_unrunnable_hook):
        # Arrange
        hook = home_with_unrunnable_hook / HOOKS_RELPATH / "guard.sh"
        # Act
        ensure_armed_hooks_executable(home_with_unrunnable_hook)
        # Assert — exec'ing the bare path is exactly how Claude Code invokes it.
        assert subprocess.run([str(hook)], capture_output=True).returncode == 0

    def test_repair_is_reported(self, home_with_unrunnable_hook):
        # Arrange
        expected = "guard.sh"
        # Act
        repaired = ensure_armed_hooks_executable(home_with_unrunnable_hook)
        # Assert
        assert [Path(p).name for p in repaired] == [expected]

    def test_mode_is_exactly_the_hook_mode(self, home_with_unrunnable_hook):
        # Arrange
        hook = home_with_unrunnable_hook / HOOKS_RELPATH / "guard.sh"
        # Act
        ensure_armed_hooks_executable(home_with_unrunnable_hook)
        # Assert
        assert stat.S_IMODE(hook.stat().st_mode) == HOOK_MODE

    def test_already_runnable_hook_is_not_reported(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        plant(home, "fine.sh", mode=0o755)
        write_settings(home, ["$HOME/.claude/hooks/pre-tool-use/fine.sh"])
        # Act
        repaired = ensure_armed_hooks_executable(home)
        # Assert — steady state must be silent.
        assert repaired == []


class TestScopeIsNarrow:
    def test_interpreter_prefixed_hook_is_left_alone(self, tmp_path):
        # Arrange — `python3 <path>` runs regardless of mode; not our business.
        home = tmp_path / "home"
        hook = plant(home, "viapy.py", mode=0o644)
        write_settings(home, ["python3 $HOME/.claude/hooks/pre-tool-use/viapy.py"])
        # Act
        ensure_armed_hooks_executable(home)
        # Assert
        assert is_executable(hook) is False

    def test_missing_armed_path_does_not_raise(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        write_settings(home, ["$HOME/.claude/hooks/pre-tool-use/ghost.sh"])
        # Act
        repaired = ensure_armed_hooks_executable(home)
        # Assert — returning at all is the point; a raise would fail the test.
        assert repaired == []

    def test_absent_settings_is_a_no_op(self, tmp_path):
        # Arrange
        home = tmp_path / "bare-home"
        home.mkdir()
        # Act
        repaired = ensure_armed_hooks_executable(home)
        # Assert
        assert repaired == []

    def test_symlink_escaping_the_home_is_not_chmodded(self, tmp_path):
        # Arrange — a hook symlinked to a file OUTSIDE the agent's home must
        # never have its target's mode rewritten.
        outside = tmp_path / "host_file.sh"
        outside.write_text(RUNNABLE)
        os.chmod(outside, 0o644)
        home = tmp_path / "home"
        link = home / ".claude" / "hooks" / "pre-tool-use" / "linked.sh"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        write_settings(home, ["$HOME/.claude/hooks/pre-tool-use/linked.sh"])
        # Act
        ensure_armed_hooks_executable(home)
        # Assert
        assert is_executable(outside) is False


class TestBarePathDetection:
    def test_bare_path_command_is_selected(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        settings = write_settings(home, ["$HOME/.claude/hooks/pre-tool-use/a.sh"])
        # Act
        found = armed_bare_path_commands(settings)
        # Assert
        assert found == ["$HOME/.claude/hooks/pre-tool-use/a.sh"]

    def test_interpreter_prefixed_command_is_excluded(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        settings = write_settings(
            home, ["python3 $HOME/.claude/hooks/pre-tool-use/b.py"]
        )
        # Act
        found = armed_bare_path_commands(settings)
        # Assert
        assert found == []

    def test_non_hook_command_is_excluded(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        settings = write_settings(home, ["rtk hook claude"])
        # Act
        found = armed_bare_path_commands(settings)
        # Assert
        assert found == []


class TestPackagedAssetsCarryTheBit:
    """The other half: sac's own deploy must never place a mode-less hook."""

    def test_content_correct_but_mode_wrong_is_repaired(self, tmp_path):
        # Arrange — the exact state a content check calls "current": right
        # bytes, no execute bit (what the to_home walk lays down from a 0644
        # dotfiles source).
        home = tmp_path / "home"
        deploy_baseline_hook_assets(home)
        dst = home / HOOKS_RELPATH / PROBE_HOOK
        os.chmod(dst, 0o644)
        # Act
        deploy_baseline_hook_assets(home)
        # Assert
        assert is_executable(dst) is True

    def test_mode_repair_is_not_reported_as_unchanged(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        deploy_baseline_hook_assets(home)
        os.chmod(home / HOOKS_RELPATH / PROBE_HOOK, 0o644)
        # Act
        result = deploy_baseline_hook_assets(home)
        # Assert — calling it "unchanged" is precisely the bug.
        assert PROBE_HOOK not in result["unchanged"]

    def test_every_deployed_asset_is_executable_after_placement(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        # Act
        deploy_baseline_hook_assets(home)
        # Assert — verified per copy, after placement, not once at the source.
        not_runnable = [
            src.name
            for src, sub in hook_asset_plan()
            if not is_executable(home / ".claude" / "hooks" / sub / src.name)
        ]
        assert not_runnable == []


class TestStartPathWiring:
    """Regression guard: removing the call from deploy_to_home re-arms the bug."""

    def test_start_path_repairs_an_unrunnable_armed_hook(self, tmp_path):
        # Arrange
        saved = {k: os.environ.get(k) for k in ("SAC_USER_TO_HOME_BASELINE",)}
        os.environ["SAC_USER_TO_HOME_BASELINE"] = str(tmp_path / "nonexistent")
        try:
            agent_dir = tmp_path / "agent_def"
            (agent_dir / "to_home" / ".claude").mkdir(parents=True)
            cfg = AgentConfig(name="exec-bit-probe")
            cfg.config_path = str(agent_dir / "spec.yaml")
            cfg.to_home = ""
            home = tmp_path / "home"
            hook = plant(home, "guard.sh", mode=0o644)
            (agent_dir / "to_home" / ".claude" / "settings.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "$HOME/.claude/hooks/"
                                            "pre-tool-use/guard.sh",
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            )
            # Act
            deploy_to_home(cfg, str(home))
            # Assert
            assert is_executable(hook) is True
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
