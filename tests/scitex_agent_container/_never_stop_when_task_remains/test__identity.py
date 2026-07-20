"""Tests for ``_never_stop_when_task_remains._identity`` — env-only agent resolution.

PA-306 no-mocks: every test drives real ``os.environ`` through the real
``env_save_restore`` fixture, and the cwd tests use a real chdir.
"""

from __future__ import annotations

import os
from pathlib import Path

from scitex_agent_container._never_stop_when_task_remains._identity import (
    IDENTITY_ENV_VARS,
    resolve_agent,
)

from ._fake_detector import clear_identity


def test_flag_wins_over_every_env_var(env_save_restore):
    # Arrange
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "from-env")
    # Act
    resolved = resolve_agent("from-flag")
    # Assert
    assert resolved == "from-flag"


def test_resolves_board_identity_env(env_save_restore):
    # Arrange
    clear_identity(env_save_restore)
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "scitex-hub")
    # Act
    resolved = resolve_agent("")
    # Assert
    assert resolved == "scitex-hub"


def test_prefers_new_cards_env_over_deprecated_todo_env(env_save_restore):
    # Arrange — the live store warns SCITEX_TODO_* is transitional.
    clear_identity(env_save_restore)
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "old-name")
    env_save_restore.set("SCITEX_CARDS_AGENT_ID", "new-name")
    # Act
    resolved = resolve_agent("")
    # Assert
    assert resolved == "new-name"


def test_falls_back_to_sac_name(env_save_restore):
    # Arrange
    clear_identity(env_save_restore)
    env_save_restore.set("SAC_NAME", "sac-injected-agent")
    # Act
    resolved = resolve_agent("")
    # Assert
    assert resolved == "sac-injected-agent"


def test_ignores_whitespace_only_env_value(env_save_restore):
    # Arrange
    clear_identity(env_save_restore)
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "   ")
    env_save_restore.set("SAC_NAME", "real-agent")
    # Act
    resolved = resolve_agent("")
    # Assert
    assert resolved == "real-agent"


def test_returns_empty_when_environment_is_silent(env_save_restore):
    # Arrange
    clear_identity(env_save_restore)
    # Act
    resolved = resolve_agent("")
    # Assert
    assert resolved == ""


def test_never_derives_identity_from_cwd(env_save_restore, tmp_path: Path):
    """Regression for the bug PR #742 removed: cwd-derived identity.

    A hook that falls back to ``Path.cwd().name`` reports a confident WRONG
    agent every time the session sits in a worktree. Standing in a directory
    named like an agent must still resolve to nothing.
    """
    # Arrange
    clear_identity(env_save_restore)
    decoy = tmp_path / "scitex-hub"
    decoy.mkdir()
    saved = os.getcwd()
    os.chdir(decoy)
    try:
        # Act
        resolved = resolve_agent("")
    finally:
        os.chdir(saved)
    # Assert
    assert resolved == ""


def test_identity_var_list_contains_no_cwd_sentinel():
    # Arrange
    names = IDENTITY_ENV_VARS
    # Act
    joined = " ".join(names).lower()
    # Assert
    assert "cwd" not in joined and "pwd" not in joined
