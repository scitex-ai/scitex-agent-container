"""Tests for settings_json.py — .claude/settings.local.json injection."""

import json
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes.settings_json import (
    _HOOKS_CONFIG,
    _MANAGED_KEYS,
    cleanup_settings_json,
    seed_claude_json_project_entry,
    setup_settings_json,
)


def _make_cfg(*flags: str) -> AgentConfig:
    return AgentConfig(name="test-agent", claude=ClaudeSpec(flags=list(flags)))


def _settings_path(workdir: str) -> Path:
    return Path(workdir) / ".claude" / "settings.local.json"


def test_hooks_always_injected_when_settings_written(tmp_path):
    """The hook block goes in on every non-empty write, even without dev-channels."""
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data["hooks"] == _HOOKS_CONFIG


def test_hook_commands_match_hook_event_kinds(tmp_path):
    """Each hook kind points at the right ``scitex-agent-container hook-event`` sub-command."""
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    hooks = json.loads(_settings_path(tmp_path).read_text())["hooks"]
    expected = {
        "PreToolUse": "scitex-agent-container hook-event pretool",
        "PostToolUse": "scitex-agent-container hook-event posttool",
        "UserPromptSubmit": "scitex-agent-container hook-event prompt",
        "Stop": "scitex-agent-container hook-event stop",
    }
    for kind, cmd in expected.items():
        assert kind in hooks, f"missing hook kind: {kind}"
        assert hooks[kind][0]["hooks"][0]["command"] == cmd
        assert hooks[kind][0]["matcher"] == ""
        assert hooks[kind][0]["hooks"][0]["type"] == "command"


def test_hooks_coexist_with_skip_permissions_and_dev_channels(tmp_path):
    """Prior managed keys still end up in the file alongside the hooks block."""
    cfg = _make_cfg(
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels",
    )
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data["skipDangerousModePermissionPrompt"] is True
    assert data["enableAllProjectMcpServers"] is True
    assert "hooks" in data


def test_cleanup_removes_hooks(tmp_path):
    """cleanup_settings_json drops hooks along with the other managed keys."""
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    assert "hooks" in json.loads(_settings_path(tmp_path).read_text())
    cleanup_settings_json(cfg, str(tmp_path))
    # File may be gone (empty after cleanup) or present without hooks key.
    if _settings_path(tmp_path).exists():
        remaining = json.loads(_settings_path(tmp_path).read_text())
        assert "hooks" not in remaining
    # Either way, the managed keys are all gone.


def test_cleanup_preserves_user_keys(tmp_path):
    """User-added keys must survive cleanup."""
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"userCustomKey": "keep me"}))
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    cleanup_settings_json(cfg, str(tmp_path))
    assert sp.exists()
    remaining = json.loads(sp.read_text())
    assert remaining == {"userCustomKey": "keep me"}


def test_managed_keys_includes_hooks():
    """Regression guard: hooks MUST be in _MANAGED_KEYS so cleanup is in sync."""
    assert "hooks" in _MANAGED_KEYS


# ---------------------------------------------------------------------------
# seed_claude_json_project_entry (todo#396)
# ---------------------------------------------------------------------------


def _write_claude_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_seed_adds_entry_when_missing(tmp_path, monkeypatch):
    """New workdir path gets a project entry in ~/.claude.json."""
    home = tmp_path / "home"
    home.mkdir()
    claude_json = home / ".claude.json"
    _write_claude_json(claude_json, {"projects": {}})
    monkeypatch.setenv("HOME", str(home))
    # monkeypatch os.path.expanduser so Path("~") resolves to our fake home
    import os
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)

    workdir = str(tmp_path / "my-agent")
    seed_claude_json_project_entry(workdir)

    data = json.loads(claude_json.read_text())
    key = str((tmp_path / "my-agent").resolve())
    assert key in data["projects"]
    assert data["projects"][key]["hasTrustDialogAccepted"] is True
    assert data["projects"][key]["hasCompletedProjectOnboarding"] is True


def test_seed_is_idempotent(tmp_path, monkeypatch):
    """Calling seed twice does not overwrite an existing entry."""
    home = tmp_path / "home"
    home.mkdir()
    claude_json = home / ".claude.json"
    workdir = str(tmp_path / "agent")
    abs_workdir = str((tmp_path / "agent").resolve())
    _write_claude_json(
        claude_json,
        {"projects": {abs_workdir: {"customKey": "preserved"}}},
    )
    import os
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)

    seed_claude_json_project_entry(workdir)

    data = json.loads(claude_json.read_text())
    assert data["projects"][abs_workdir]["customKey"] == "preserved"


def test_seed_no_op_when_no_claude_json(tmp_path, monkeypatch):
    """Missing ~/.claude.json does not raise."""
    home = tmp_path / "home"
    home.mkdir()
    import os
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)

    seed_claude_json_project_entry(str(tmp_path / "agent"))  # must not raise


def test_seed_no_op_when_claude_json_not_a_file(tmp_path, monkeypatch):
    """A directory at ~/.claude.json path is silently skipped."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").mkdir()  # directory, not file
    import os
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)

    seed_claude_json_project_entry(str(tmp_path / "agent"))  # must not raise


def test_seed_creates_projects_key_if_absent(tmp_path, monkeypatch):
    """~/.claude.json without a projects key gets one added."""
    home = tmp_path / "home"
    home.mkdir()
    claude_json = home / ".claude.json"
    _write_claude_json(claude_json, {"numStartups": 1})
    import os
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)

    workdir = str(tmp_path / "agent")
    seed_claude_json_project_entry(workdir)

    data = json.loads(claude_json.read_text())
    assert "projects" in data
    assert data["numStartups"] == 1  # original data preserved
