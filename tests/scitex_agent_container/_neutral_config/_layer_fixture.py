"""Builders shared by the neutral agent-config tests (ADR-0033)."""

from __future__ import annotations

from pathlib import Path

import yaml

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
