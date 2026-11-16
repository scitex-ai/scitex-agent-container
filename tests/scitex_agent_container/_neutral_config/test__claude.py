"""ADR-0033: the Claude Code adapter (neutral layer -> ~/.claude plan)."""

from __future__ import annotations

import pytest

from scitex_agent_container._neutral_config import (
    NeutralConfigError,
    load_layer,
    plan_claude,
)
from scitex_agent_container._neutral_config._claude import render_mcp, render_settings

from ._layer_fixture import make_layer


def _link_sources(actions):
    return {a.dest.name: a.source for a in actions if hasattr(a, "source")}


def test_claude_md_is_a_link_to_agents_md(claude_actions, user_root):
    # Arrange
    actions, _ = claude_actions
    # Act
    source = _link_sources(actions)["CLAUDE.md"]
    # Assert
    assert source == user_root / "AGENTS.md"


def test_agents_dir_links_to_neutral_subagents(claude_actions, user_root):
    # Arrange
    actions, _ = claude_actions
    # Act
    source = _link_sources(actions)["agents"]
    # Assert
    assert source == user_root / "subagents"


def test_each_skill_is_linked_individually(claude_actions, user_root):
    # Arrange
    actions, _ = claude_actions
    # Act
    source = _link_sources(actions)["alpha"]
    # Assert
    assert source == user_root / "skills" / "alpha"


def test_neutral_pre_tool_hook_renders_as_claude_pre_tool_use(user_layer):
    # Arrange
    layer = user_layer
    # Act
    groups = render_settings(layer, "user")["hooks"]["PreToolUse"]
    # Assert
    assert groups == [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/guard.sh"}],
        }
    ]


def test_claude_only_event_is_rendered_for_claude(user_layer):
    # Arrange
    layer = user_layer
    # Act
    events = render_settings(layer, "user")["hooks"]
    # Assert
    assert "WorktreeCreate" in events


def test_hook_for_another_harness_is_reported_not_rendered(claude_actions):
    # Arrange
    _, notes = claude_actions
    # Act
    mentions = [n for n in notes if "codex" in n]
    # Assert
    assert len(mentions) == 1


def test_native_claude_settings_pass_through(user_layer):
    # Arrange
    layer = user_layer
    # Act
    theme = render_settings(layer, "user")["theme"]
    # Assert
    assert theme == "dark"


def test_project_scope_hook_uses_project_dir_variable(tmp_path):
    # Arrange
    layer = load_layer(make_layer(tmp_path / "p"))
    # Act
    cmd = render_settings(layer, "project")["hooks"]["PreToolUse"][0]["hooks"][0][
        "command"
    ]
    # Assert
    assert cmd == "$CLAUDE_PROJECT_DIR/.claude/hooks/guard.sh"


def test_native_claude_may_not_shadow_neutral_keys(tmp_path):
    # Arrange
    layer = load_layer(
        make_layer(tmp_path / "u", settings={"native": {"claude": {"model": "x"}}})
    )
    # Act
    planning = pytest.raises(NeutralConfigError, match="native.claude sets")
    # Assert
    with planning:
        plan_claude(layer, tmp_path / "c", "user")


def test_mcp_stdio_server_keeps_env_placeholders():
    # Arrange
    servers = {"s": {"transport": "stdio", "command": "x", "env": {"K": "${K}"}}}
    # Act
    rendered = render_mcp(servers)
    # Assert
    assert rendered == {
        "mcpServers": {"s": {"type": "stdio", "command": "x", "env": {"K": "${K}"}}}
    }


def test_unknown_scope_is_refused(user_layer, tmp_path):
    # Arrange
    layer = user_layer
    # Act
    planning = pytest.raises(ValueError, match="scope must be")
    # Assert
    with planning:
        plan_claude(layer, tmp_path / "c", "global")


def test_external_skill_is_not_rendered(tmp_path):
    # Arrange
    root = make_layer(tmp_path / "u", skills=("alpha", "scitex"), settings={"external_skills": ["scitex"]})
    # Act
    actions, _ = plan_claude(load_layer(root), tmp_path / "c", "user")
    # Assert
    assert "scitex" not in _link_sources(actions)


def test_external_skill_is_reported(tmp_path):
    # Arrange
    root = make_layer(tmp_path / "u", skills=("scitex",), settings={"external_skills": ["scitex"]})
    # Act
    _, notes = plan_claude(load_layer(root), tmp_path / "c", "user")
    # Assert
    assert any("'scitex'" in n for n in notes)
