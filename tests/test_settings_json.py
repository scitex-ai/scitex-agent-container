"""Tests for settings_json.py — .claude/settings.local.json injection."""

import json
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes.settings_json import (
    _HOOKS_CONFIG,
    _MANAGED_KEYS,
    cleanup_settings_json,
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


def test_statusline_command_written(tmp_path):
    """statusLine is written even without skip-permissions / dev-channels."""
    cfg = _make_cfg()
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(_settings_path(tmp_path).read_text())
    assert "statusLine" in data
    assert data["statusLine"]["type"] == "command"
    assert data["statusLine"]["command"] == "sac-statusline"


def test_statusline_in_managed_keys():
    """statusLine MUST be in _MANAGED_KEYS so cleanup removes it."""
    assert "statusLine" in _MANAGED_KEYS


def test_cleanup_removes_statusline(tmp_path):
    """cleanup_settings_json drops the statusLine entry."""
    cfg = _make_cfg()
    setup_settings_json(cfg, str(tmp_path))
    assert "statusLine" in json.loads(_settings_path(tmp_path).read_text())
    cleanup_settings_json(cfg, str(tmp_path))
    if _settings_path(tmp_path).exists():
        remaining = json.loads(_settings_path(tmp_path).read_text())
        assert "statusLine" not in remaining
