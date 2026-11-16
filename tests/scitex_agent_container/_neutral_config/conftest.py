"""Shared fixtures for the neutral agent-config tests (ADR-0033)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._neutral_config import (
    apply,
    import_claude,
    load_layer,
    plan_claude,
)

DEFAULT_HOOKS = [
    {
        "name": "guard",
        "event": "pre_tool",
        "matcher": "Bash",
        "command": "hooks/guard.sh",
    },
    {"name": "wt", "event": "claude:WorktreeCreate", "command": "hooks/guard.sh"},
    {"name": "cx", "event": "codex:Weird", "command": "hooks/guard.sh"},
]
DEFAULT_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls)"]},
    "native": {"claude": {"theme": "dark"}},
}


def make_layer(
    root: Path, *, hooks=None, settings=None, skills=("alpha",), disable=None
) -> Path:
    """Write a small but complete neutral layer at ``root``."""
    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text("be kind\n")
    (root / "RTK.md").write_text("rtk notes\n")
    for name in skills:
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n"
        )
    (root / "hooks").mkdir()
    (root / "hooks" / "guard.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "subagents").mkdir()
    (root / "subagents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: r\n---\nReview.\n"
    )
    doc = {"hooks": DEFAULT_HOOKS if hooks is None else hooks}
    if disable:
        doc["disable"] = disable
    (root / "hooks.yaml").write_text(yaml.safe_dump(doc))
    (root / "settings.yaml").write_text(
        yaml.safe_dump(DEFAULT_SETTINGS if settings is None else settings)
    )
    return root


@pytest.fixture
def user_root(tmp_path):
    return make_layer(tmp_path / "user")


@pytest.fixture
def user_layer(user_root):
    return load_layer(user_root)


@pytest.fixture
def claude_actions(user_layer, tmp_path):
    return plan_claude(user_layer, tmp_path / "claude", "user")


@pytest.fixture
def lived_in_target(tmp_path):
    """A ~/.claude with a real hooks dir, live state and a foreign skill link."""
    target = tmp_path / "claude"
    (target / "hooks").mkdir(parents=True)
    (target / "hooks" / "old.sh").write_text("old")
    (target / "skills").mkdir()
    os.symlink(tmp_path, target / "skills" / "scitex")
    (target / ".credentials.json").write_text("secret")
    return target


@pytest.fixture
def deployed(user_layer, lived_in_target):
    actions, _ = plan_claude(user_layer, lived_in_target, "user")
    results = apply(lived_in_target, actions)
    return lived_in_target, actions, results


ORIGINAL_CLAUDE_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": "$HOME/.claude/hooks/pre/a.sh"}
                ],
            },
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": "$HOME/.claude/hooks/pre/b.sh"}
                ],
            },
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": "rtk hook claude"}],
            },
        ],
        "PostToolUse": [
            {
                "matcher": ".*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "$HOME/.claude/hooks/post/a.sh",
                        "timeout": 5,
                    }
                ],
            }
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": "$HOME/.claude/hooks/stop/a.sh"}]}
        ],
        "WorktreeCreate": [
            {"hooks": [{"type": "command", "command": "$HOME/.claude/hooks/wt.sh"}]}
        ],
    },
    "statusLine": {"type": "command", "command": "x"},
    "model": "opus",
    "permissions": {"allow": ["Bash(ls)"]},
}


@pytest.fixture
def claude_dir(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps(ORIGINAL_CLAUDE_SETTINGS))
    (claude / "CLAUDE.md").write_text("rules\n")
    return claude


@pytest.fixture
def imported_root(claude_dir, tmp_path):
    dest = tmp_path / "imported"
    import_claude(claude_dir, dest)
    return dest
