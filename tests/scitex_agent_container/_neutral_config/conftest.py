"""Shared fixtures for the neutral agent-config tests (ADR-0033)."""

from __future__ import annotations

import json
import os

import pytest

from scitex_agent_container._neutral_config import (
    apply,
    import_claude,
    load_layer,
    plan_claude,
)

from ._layer_fixture import ORIGINAL_CLAUDE_SETTINGS, make_layer


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
