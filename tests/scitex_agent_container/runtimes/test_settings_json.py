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
    """Each hook kind points at the right ``scitex-agent-container ingest-hook-event`` sub-command."""
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    hooks = json.loads(_settings_path(tmp_path).read_text())["hooks"]
    expected = {
        "PreToolUse": "scitex-agent-container ingest-hook-event pretool",
        "PostToolUse": "scitex-agent-container ingest-hook-event posttool",
        "UserPromptSubmit": "scitex-agent-container ingest-hook-event prompt",
        "Stop": "scitex-agent-container ingest-hook-event stop",
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


# ---------------------------------------------------------------------------
# Merged from test_settings_json_extras.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------


import pytest

import scitex_agent_container.runtimes.settings_json as sj_mod
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.settings_json import (
    _mcp_server_names,
    _needs_dev_channels,
    _needs_skip_permissions,
    ensure_global_settings_json,
)

# ---------------------------------------------------------------------------
# _needs_* predicates
# ---------------------------------------------------------------------------


def test_needs_skip_permissions_true_when_flag_present():
    cfg = AgentConfig(
        name="x", claude=ClaudeSpec(flags=["--dangerously-skip-permissions"])
    )
    assert _needs_skip_permissions(cfg) is True


def test_needs_skip_permissions_false_without_flag():
    cfg = AgentConfig(name="x", claude=ClaudeSpec(flags=["--other"]))
    assert _needs_skip_permissions(cfg) is False


def test_needs_dev_channels_true_when_flag_present():
    cfg = AgentConfig(
        name="x", claude=ClaudeSpec(flags=["--dangerously-load-development-channels"])
    )
    assert _needs_dev_channels(cfg) is True


def test_needs_dev_channels_false_without_flag():
    cfg = AgentConfig(name="x", claude=ClaudeSpec(flags=[]))
    assert _needs_dev_channels(cfg) is False


# ---------------------------------------------------------------------------
# _mcp_server_names
# ---------------------------------------------------------------------------


def test_mcp_server_names_from_config_only(tmp_path):
    cfg = AgentConfig(
        name="x", mcp_servers={"alpha": {"command": "x"}, "beta": {"command": "y"}}
    )
    names = _mcp_server_names(cfg, str(tmp_path))
    assert names == ["alpha", "beta"]


def test_mcp_server_names_from_on_disk_only(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"gamma": {}, "delta": {}}})
    )
    cfg = AgentConfig(name="x")
    names = _mcp_server_names(cfg, str(tmp_path))
    assert names == ["delta", "gamma"]


def test_mcp_server_names_merges_and_dedupes(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"alpha": {}, "gamma": {}}})
    )
    cfg = AgentConfig(name="x", mcp_servers={"alpha": {}, "beta": {}})
    names = _mcp_server_names(cfg, str(tmp_path))
    assert names == ["alpha", "beta", "gamma"]


def test_mcp_server_names_tolerates_malformed_mcp_json(tmp_path):
    (tmp_path / ".mcp.json").write_text("not-json {{{")
    cfg = AgentConfig(name="x", mcp_servers={"alpha": {}})
    names = _mcp_server_names(cfg, str(tmp_path))
    assert names == ["alpha"]


def test_mcp_server_names_no_files_no_servers(tmp_path):
    cfg = AgentConfig(name="x")
    assert _mcp_server_names(cfg, str(tmp_path)) == []


# ---------------------------------------------------------------------------
# ensure_global_settings_json
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path):
    """Redirect Path.home() to a tmp_path via $HOME.

    PA-306: no `monkeypatch.setattr` on Path.home. Path.home() reads
    $HOME on Unix; mutating the env var with explicit save/restore
    is the real equivalent.
    """
    import os

    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_ensure_global_settings_creates_when_missing(fake_home):
    sj_mod._SEED_TEMPLATE_orig = sj_mod._SEED_TEMPLATE
    # Force fallback to _SEED_DEFAULTS (template path under our fake home will not exist).
    target = fake_home / ".claude" / "settings.json"
    assert not target.exists()
    # Redirect _SEED_TEMPLATE to a non-existent location under fake_home.
    import scitex_agent_container.runtimes.settings_json as m

    m._SEED_TEMPLATE = fake_home / "no-such-template.json"
    ensure_global_settings_json()
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True
    assert "permissions" in data


def test_ensure_global_settings_noop_when_present(fake_home):
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"existing": True}))
    ensure_global_settings_json()
    # File untouched.
    assert json.loads(target.read_text()) == {"existing": True}


def test_ensure_global_settings_replaces_broken_symlink(fake_home):
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(fake_home / "does-not-exist.json")
    # Sanity: broken symlink
    assert target.is_symlink() and not target.exists()

    import scitex_agent_container.runtimes.settings_json as m

    m._SEED_TEMPLATE = fake_home / "no-template.json"
    ensure_global_settings_json()
    # Replaced with a real file.
    assert target.exists() and not target.is_symlink()
    data = json.loads(target.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


def test_ensure_global_settings_uses_template_when_available(fake_home):
    import scitex_agent_container.runtimes.settings_json as m

    template_path = fake_home / "template.json"
    template_path.write_text(
        json.dumps({"_comment": "drop me", "custom": "value", "ok": True})
    )
    m._SEED_TEMPLATE = template_path

    target = fake_home / ".claude" / "settings.json"
    ensure_global_settings_json()
    data = json.loads(target.read_text())
    assert data["custom"] == "value"
    assert data["ok"] is True
    assert "_comment" not in data


def test_ensure_global_settings_falls_back_when_template_malformed(fake_home):
    import scitex_agent_container.runtimes.settings_json as m

    bad = fake_home / "bad-template.json"
    bad.write_text("not json {{{")
    m._SEED_TEMPLATE = bad

    target = fake_home / ".claude" / "settings.json"
    ensure_global_settings_json()
    data = json.loads(target.read_text())
    # Falls back to defaults.
    assert data["skipDangerousModePermissionPrompt"] is True


# ---------------------------------------------------------------------------
# setup_settings_json — merge edges
# ---------------------------------------------------------------------------


def test_setup_merges_enabled_mcpjson_servers(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"enabledMcpjsonServers": ["existing-one"]}))

    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"new-one": {}, "existing-one": {}}})
    )

    cfg = AgentConfig(
        name="m",
        claude=ClaudeSpec(flags=["--dangerously-load-development-channels"]),
    )
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(settings_path.read_text())
    assert data["enabledMcpjsonServers"] == ["existing-one", "new-one"]


def test_setup_merges_with_corrupt_existing_file(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("garbage {{")

    cfg = AgentConfig(
        name="m", claude=ClaudeSpec(flags=["--dangerously-skip-permissions"])
    )
    # Must not crash; corrupt content is replaced by the managed keys.
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(settings_path.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


def test_setup_merges_when_existing_is_not_dict(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(["a", "list"]))

    cfg = AgentConfig(
        name="m", claude=ClaudeSpec(flags=["--dangerously-skip-permissions"])
    )
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(settings_path.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


# ---------------------------------------------------------------------------
# cleanup_settings_json — edges
# ---------------------------------------------------------------------------


def test_cleanup_noop_when_file_missing(tmp_path):
    cfg = AgentConfig(name="m")
    # No file. Must not raise.
    cleanup_settings_json(cfg, str(tmp_path))


def test_cleanup_noop_when_file_corrupt(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("garbage {{")
    cleanup_settings_json(AgentConfig(name="m"), str(tmp_path))
    # File left as-is (best-effort).
    assert settings_path.read_text() == "garbage {{"


def test_cleanup_noop_when_no_managed_keys(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"userOnly": 1}))
    cleanup_settings_json(AgentConfig(name="m"), str(tmp_path))
    # User keys preserved.
    assert json.loads(settings_path.read_text()) == {"userOnly": 1}


def test_cleanup_noop_when_existing_is_not_dict(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps([1, 2, 3]))
    cleanup_settings_json(AgentConfig(name="m"), str(tmp_path))
    # Untouched.
    assert json.loads(settings_path.read_text()) == [1, 2, 3]
