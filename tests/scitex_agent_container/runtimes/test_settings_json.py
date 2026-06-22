"""Tests for settings_json.py — .claude/settings.local.json injection."""

import json
import os
from pathlib import Path

import pytest

import scitex_agent_container.runtimes.settings_json as sj_mod
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes.settings_json import (
    _HOOKS_CONFIG,
    _MANAGED_KEYS,
    _mcp_server_names,
    _needs_dev_channels,
    _needs_skip_permissions,
    cleanup_settings_json,
    ensure_global_settings_json,
    setup_settings_json,
)


def _make_cfg(*flags: str) -> AgentConfig:
    return AgentConfig(name="test-agent", claude=ClaudeSpec(flags=list(flags)))


def _settings_path(workdir: str) -> Path:
    return Path(workdir) / ".claude" / "settings.local.json"


# ---------------------------------------------------------------------------
# Hooks injection
# ---------------------------------------------------------------------------


def test_hooks_always_injected_when_settings_written(tmp_path):
    """The hook block goes in on every non-empty write, even without dev-channels."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data["hooks"] == _HOOKS_CONFIG


@pytest.mark.parametrize(
    "kind,subcommand",
    [
        ("PreToolUse", "pretool"),
        ("PostToolUse", "posttool"),
        ("UserPromptSubmit", "prompt"),
        ("Stop", "stop"),
    ],
)
def test_hook_kind_maps_to_ingest_command(tmp_path, kind, subcommand):
    """Each hook kind points at the right ingest-hook-event sub-command."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    # Act
    hooks = json.loads(_settings_path(tmp_path).read_text())["hooks"]
    # Assert
    assert hooks[kind][0]["hooks"][0]["command"].endswith(" " + subcommand)


@pytest.mark.parametrize(
    "kind", ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]
)
def test_hook_command_has_no_control_chars(tmp_path, kind):
    """Regression: a stray backspace (\\x08) once sat INSIDE every hook command
    (``ingest-hook-event\\x08 pretool``), so each tool/turn ran an unknown
    command ``ingest-hook-event\\x08`` → ``No such command``. The suffix-only
    assert above missed it (the command still ended with `` pretool``); assert
    the command carries no ASCII control char."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    command = json.loads(_settings_path(tmp_path).read_text())["hooks"][kind][0][
        "hooks"
    ][0]["command"]
    # Act
    control_chars = [ch for ch in command if ord(ch) < 32 and ch != "\t"]
    # Assert
    assert control_chars == []


@pytest.mark.parametrize(
    "kind", ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]
)
def test_hook_kind_has_empty_matcher(tmp_path, kind):
    """Every hook kind uses the wildcard (empty) matcher."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    # Act
    hooks = json.loads(_settings_path(tmp_path).read_text())["hooks"]
    # Assert
    assert hooks[kind][0]["matcher"] == ""


@pytest.mark.parametrize(
    "kind", ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]
)
def test_hook_kind_uses_command_type(tmp_path, kind):
    """Every hook entry is of type ``command``."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    # Act
    hooks = json.loads(_settings_path(tmp_path).read_text())["hooks"]
    # Assert
    assert hooks[kind][0]["hooks"][0]["type"] == "command"


def test_skip_permissions_key_present_with_both_flags(tmp_path):
    """skipDangerousModePermissionPrompt is written when --dangerously-skip-permissions is set."""
    # Arrange
    cfg = _make_cfg(
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels",
    )
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


def test_enable_all_mcp_servers_present_with_dev_channels(tmp_path):
    """enableAllProjectMcpServers is written when dev channels are enabled."""
    # Arrange
    cfg = _make_cfg(
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels",
    )
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data["enableAllProjectMcpServers"] is True


def test_hooks_coexist_with_skip_permissions_and_dev_channels(tmp_path):
    """The hooks block is still present alongside the other managed keys."""
    # Arrange
    cfg = _make_cfg(
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels",
    )
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(_settings_path(tmp_path).read_text())
    assert "hooks" in data


# ---------------------------------------------------------------------------
# cleanup_settings_json — hooks/statusLine
# ---------------------------------------------------------------------------


def test_cleanup_removes_hooks_key(tmp_path):
    """cleanup_settings_json drops the hooks key from the settings file."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    # Act
    cleanup_settings_json(cfg, str(tmp_path))
    # Assert
    remaining = (
        json.loads(_settings_path(tmp_path).read_text())
        if _settings_path(tmp_path).exists()
        else {}
    )
    assert "hooks" not in remaining


def test_cleanup_preserves_user_keys(tmp_path):
    """User-added keys must survive cleanup."""
    # Arrange
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"userCustomKey": "keep me"}))
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path))
    # Act
    cleanup_settings_json(cfg, str(tmp_path))
    # Assert
    assert json.loads(sp.read_text()) == {"userCustomKey": "keep me"}


def test_managed_keys_includes_hooks():
    """Regression guard: hooks MUST be in _MANAGED_KEYS so cleanup is in sync."""
    # Arrange
    managed = _MANAGED_KEYS
    # Act
    contains_hooks = "hooks" in managed
    # Assert
    assert contains_hooks


# ---------------------------------------------------------------------------
# statusLine
# ---------------------------------------------------------------------------


def test_statusline_section_present_without_flags(tmp_path):
    """statusLine is written even without skip-permissions / dev-channels."""
    # Arrange
    cfg = _make_cfg()
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(_settings_path(tmp_path).read_text())
    assert "statusLine" in data


def test_statusline_uses_command_type(tmp_path):
    """statusLine entry has type=command."""
    # Arrange
    cfg = _make_cfg()
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data["statusLine"]["type"] == "command"


def test_statusline_runs_sac_statusline(tmp_path):
    """statusLine points at the ``sac-statusline`` binary."""
    # Arrange
    cfg = _make_cfg()
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(_settings_path(tmp_path).read_text())
    assert data["statusLine"]["command"] == "sac-statusline"


def test_statusline_in_managed_keys_set():
    """statusLine MUST be in _MANAGED_KEYS so cleanup removes it."""
    # Arrange
    managed = _MANAGED_KEYS
    # Act
    contains_statusline = "statusLine" in managed
    # Assert
    assert contains_statusline


def test_cleanup_removes_statusline_key(tmp_path):
    """cleanup_settings_json drops the statusLine entry."""
    # Arrange
    cfg = _make_cfg()
    setup_settings_json(cfg, str(tmp_path))
    # Act
    cleanup_settings_json(cfg, str(tmp_path))
    # Assert
    remaining = (
        json.loads(_settings_path(tmp_path).read_text())
        if _settings_path(tmp_path).exists()
        else {}
    )
    assert "statusLine" not in remaining


# ---------------------------------------------------------------------------
# _needs_* predicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags,expected",
    [
        (["--dangerously-skip-permissions"], True),
        (["--other"], False),
        ([], False),
    ],
)
def test_needs_skip_permissions_matches_flag_presence(flags, expected):
    """_needs_skip_permissions reflects whether the skip flag is present."""
    # Arrange
    cfg = AgentConfig(name="x", claude=ClaudeSpec(flags=flags))
    # Act
    result = _needs_skip_permissions(cfg)
    # Assert
    assert result is expected


@pytest.mark.parametrize(
    "flags,expected",
    [
        (["--dangerously-load-development-channels"], True),
        (["--other"], False),
        ([], False),
    ],
)
def test_needs_dev_channels_matches_flag_presence(flags, expected):
    """_needs_dev_channels reflects whether the dev-channels flag is present."""
    # Arrange
    cfg = AgentConfig(name="x", claude=ClaudeSpec(flags=flags))
    # Act
    result = _needs_dev_channels(cfg)
    # Assert
    assert result is expected


# ---------------------------------------------------------------------------
# _mcp_server_names
# ---------------------------------------------------------------------------


def test_mcp_server_names_from_config_only(tmp_path):
    """Server names come straight from the in-memory config when there is no .mcp.json."""
    # Arrange
    cfg = AgentConfig(
        name="x", mcp_servers={"alpha": {"command": "x"}, "beta": {"command": "y"}}
    )
    # Act
    names = _mcp_server_names(cfg, str(tmp_path))
    # Assert
    assert names == ["alpha", "beta"]


def test_mcp_server_names_from_on_disk_only(tmp_path):
    """Server names come from .mcp.json when the config has none."""
    # Arrange
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"gamma": {}, "delta": {}}})
    )
    cfg = AgentConfig(name="x")
    # Act
    names = _mcp_server_names(cfg, str(tmp_path))
    # Assert
    assert names == ["delta", "gamma"]


def test_mcp_server_names_merges_and_dedupes(tmp_path):
    """Config + .mcp.json are merged and deduplicated."""
    # Arrange
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"alpha": {}, "gamma": {}}})
    )
    cfg = AgentConfig(name="x", mcp_servers={"alpha": {}, "beta": {}})
    # Act
    names = _mcp_server_names(cfg, str(tmp_path))
    # Assert
    assert names == ["alpha", "beta", "gamma"]


def test_mcp_server_names_tolerates_malformed_mcp_json(tmp_path):
    """A malformed .mcp.json falls back to whatever the config knows."""
    # Arrange
    (tmp_path / ".mcp.json").write_text("not-json {{{")
    cfg = AgentConfig(name="x", mcp_servers={"alpha": {}})
    # Act
    names = _mcp_server_names(cfg, str(tmp_path))
    # Assert
    assert names == ["alpha"]


def test_mcp_server_names_empty_when_no_files_no_servers(tmp_path):
    """No config and no .mcp.json yields an empty list."""
    # Arrange
    cfg = AgentConfig(name="x")
    # Act
    names = _mcp_server_names(cfg, str(tmp_path))
    # Assert
    assert names == []


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
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_ensure_global_settings_creates_file_when_missing(fake_home):
    """ensure_global_settings_json creates the settings file when absent."""
    # Arrange
    sj_mod._SEED_TEMPLATE = fake_home / "no-such-template.json"
    target = fake_home / ".claude" / "settings.json"
    # Act
    ensure_global_settings_json()
    # Assert
    assert target.exists()


def test_ensure_global_settings_seeds_skip_permissions_default(fake_home):
    """The fallback seed sets skipDangerousModePermissionPrompt=True."""
    # Arrange
    sj_mod._SEED_TEMPLATE = fake_home / "no-such-template.json"
    target = fake_home / ".claude" / "settings.json"
    # Act
    ensure_global_settings_json()
    # Assert
    data = json.loads(target.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


def test_ensure_global_settings_seeds_permissions_section(fake_home):
    """The fallback seed includes a ``permissions`` section."""
    # Arrange
    sj_mod._SEED_TEMPLATE = fake_home / "no-such-template.json"
    target = fake_home / ".claude" / "settings.json"
    # Act
    ensure_global_settings_json()
    # Assert
    data = json.loads(target.read_text())
    assert "permissions" in data


def test_ensure_global_settings_noop_when_present(fake_home):
    """An existing settings file is left untouched."""
    # Arrange
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"existing": True}))
    # Act
    ensure_global_settings_json()
    # Assert
    assert json.loads(target.read_text()) == {"existing": True}


def test_ensure_global_settings_replaces_broken_symlink(fake_home):
    """A broken symlink is replaced by a real file."""
    # Arrange
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(fake_home / "does-not-exist.json")
    sj_mod._SEED_TEMPLATE = fake_home / "no-template.json"
    # Act
    ensure_global_settings_json()
    # Assert
    assert target.exists() and not target.is_symlink()


def test_ensure_global_settings_uses_template_custom_value(fake_home):
    """Template values are copied into the new settings file."""
    # Arrange
    template_path = fake_home / "template.json"
    template_path.write_text(
        json.dumps({"_comment": "drop me", "custom": "value", "ok": True})
    )
    sj_mod._SEED_TEMPLATE = template_path
    target = fake_home / ".claude" / "settings.json"
    # Act
    ensure_global_settings_json()
    # Assert
    data = json.loads(target.read_text())
    assert data["custom"] == "value"


def test_ensure_global_settings_template_strips_comment_keys(fake_home):
    """Underscore-prefixed comment keys are stripped from the template."""
    # Arrange
    template_path = fake_home / "template.json"
    template_path.write_text(
        json.dumps({"_comment": "drop me", "custom": "value", "ok": True})
    )
    sj_mod._SEED_TEMPLATE = template_path
    target = fake_home / ".claude" / "settings.json"
    # Act
    ensure_global_settings_json()
    # Assert
    data = json.loads(target.read_text())
    assert "_comment" not in data


def test_ensure_global_settings_falls_back_when_template_malformed(fake_home):
    """A malformed template silently falls back to the built-in defaults."""
    # Arrange
    bad = fake_home / "bad-template.json"
    bad.write_text("not json {{{")
    sj_mod._SEED_TEMPLATE = bad
    target = fake_home / ".claude" / "settings.json"
    # Act
    ensure_global_settings_json()
    # Assert
    data = json.loads(target.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


# ---------------------------------------------------------------------------
# setup_settings_json — merge edges
# ---------------------------------------------------------------------------


def test_setup_merges_enabled_mcpjson_servers(tmp_path):
    """Existing enabledMcpjsonServers entries are merged with new ones from .mcp.json."""
    # Arrange
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
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(settings_path.read_text())
    assert data["enabledMcpjsonServers"] == ["existing-one", "new-one"]


def test_setup_merges_with_corrupt_existing_file(tmp_path):
    """A corrupt settings file is replaced cleanly with the managed keys."""
    # Arrange
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("garbage {{")
    cfg = AgentConfig(
        name="m", claude=ClaudeSpec(flags=["--dangerously-skip-permissions"])
    )
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(settings_path.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


def test_setup_merges_when_existing_is_not_dict(tmp_path):
    """A non-dict existing payload is replaced cleanly with the managed keys."""
    # Arrange
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(["a", "list"]))
    cfg = AgentConfig(
        name="m", claude=ClaudeSpec(flags=["--dangerously-skip-permissions"])
    )
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    data = json.loads(settings_path.read_text())
    assert data["skipDangerousModePermissionPrompt"] is True


# ---------------------------------------------------------------------------
# cleanup_settings_json — edges
# ---------------------------------------------------------------------------


def test_cleanup_noop_when_file_missing(tmp_path):
    """cleanup_settings_json must not raise when the file is absent."""
    # Arrange
    cfg = AgentConfig(name="m")
    settings_path = _settings_path(tmp_path)
    # Act
    cleanup_settings_json(cfg, str(tmp_path))
    # Assert
    assert not settings_path.exists()


def test_cleanup_noop_when_file_corrupt(tmp_path):
    """A corrupt file is left as-is (best-effort cleanup)."""
    # Arrange
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("garbage {{")
    # Act
    cleanup_settings_json(AgentConfig(name="m"), str(tmp_path))
    # Assert
    assert settings_path.read_text() == "garbage {{"


def test_cleanup_noop_when_no_managed_keys(tmp_path):
    """A file with no managed keys keeps all user keys untouched."""
    # Arrange
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"userOnly": 1}))
    # Act
    cleanup_settings_json(AgentConfig(name="m"), str(tmp_path))
    # Assert
    assert json.loads(settings_path.read_text()) == {"userOnly": 1}


def test_cleanup_noop_when_existing_is_not_dict(tmp_path):
    """A non-dict payload is left untouched by cleanup."""
    # Arrange
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps([1, 2, 3]))
    # Act
    cleanup_settings_json(AgentConfig(name="m"), str(tmp_path))
    # Assert
    assert json.loads(settings_path.read_text()) == [1, 2, 3]


# ---------------------------------------------------------------------------
# filename= parameter — USER-scope settings.json vs PROJECT-scope .local.json
# ---------------------------------------------------------------------------


def test_setup_filename_settings_json_writes_settings_json(tmp_path):
    """filename='settings.json' writes the USER-scope file the TUI reads."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    # Act
    setup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Assert
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_setup_filename_settings_json_skips_local_json(tmp_path):
    """filename='settings.json' does NOT create a settings.local.json."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    # Act
    setup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Assert
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_setup_default_filename_still_writes_local_json(tmp_path):
    """The default (no filename=) keeps writing settings.local.json (project scope)."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    # Act
    setup_settings_json(cfg, str(tmp_path))
    # Assert
    assert (tmp_path / ".claude" / "settings.local.json").is_file()


def test_setup_settings_json_folds_legacy_local_sibling(tmp_path):
    """A legacy settings.local.json baseline gate is folded into settings.json."""
    # Arrange — deploy_to_home landed the baseline gate under the old name.
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.local.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [{"type": "command", "command": "gate.sh verify"}],
                        }
                    ]
                }
            }
        )
    )
    cfg = _make_cfg("--dangerously-skip-permissions")
    # Act
    setup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Assert — the baseline gate now lives in settings.json.
    data = json.loads((claude / "settings.json").read_text())
    commands = [h["command"] for grp in data["hooks"]["Stop"] for h in grp["hooks"]]
    assert "gate.sh verify" in commands


def test_setup_settings_json_keeps_sac_hook_after_fold(tmp_path):
    """After folding the legacy sibling, SAC's own Stop hook still coexists."""
    # Arrange
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.local.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [{"type": "command", "command": "gate.sh verify"}],
                        }
                    ]
                }
            }
        )
    )
    cfg = _make_cfg("--dangerously-skip-permissions")
    # Act
    setup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Assert — SAC's ingest-hook-event stop hook is present under Stop too.
    data = json.loads((claude / "settings.json").read_text())
    commands = [h["command"] for grp in data["hooks"]["Stop"] for h in grp["hooks"]]
    assert any("ingest-hook-event" in c for c in commands)


def test_setup_settings_json_removes_legacy_sibling_after_fold(tmp_path):
    """The legacy settings.local.json is removed once folded into settings.json."""
    # Arrange
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.local.json").write_text(json.dumps({"hooks": {}}))
    cfg = _make_cfg("--dangerously-skip-permissions")
    # Act
    setup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Assert — no critical hook can hide in the path the TUI never reads.
    assert not (claude / "settings.local.json").exists()


def test_cleanup_filename_settings_json_targets_settings_json(tmp_path):
    """cleanup_settings_json(filename='settings.json') drops managed keys there."""
    # Arrange
    cfg = _make_cfg("--dangerously-skip-permissions")
    setup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Act
    cleanup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Assert
    remaining_path = tmp_path / ".claude" / "settings.json"
    remaining = (
        json.loads(remaining_path.read_text()) if remaining_path.exists() else {}
    )
    assert "hooks" not in remaining


def test_merge_hooks_blocks_dedupes_identical_groups():
    """_merge_hooks_blocks concatenates per event and de-dupes identical groups."""
    # Arrange
    from scitex_agent_container.runtimes.settings_json import _merge_hooks_blocks

    grp = {"matcher": "", "hooks": [{"type": "command", "command": "x"}]}
    # Act
    merged = _merge_hooks_blocks({"Stop": [grp]}, {"Stop": [grp]})
    # Assert
    assert merged["Stop"] == [grp]


def test_merge_hooks_blocks_preserves_base_when_overlay_adds_event():
    """A base event survives when the overlay only adds a different event."""
    # Arrange
    from scitex_agent_container.runtimes.settings_json import _merge_hooks_blocks

    base = {"Stop": [{"matcher": "", "hooks": [{"command": "gate"}]}]}
    overlay = {"PreToolUse": [{"matcher": "", "hooks": [{"command": "pre"}]}]}
    # Act
    merged = _merge_hooks_blocks(base, overlay)
    # Assert
    assert merged["Stop"] == base["Stop"]
