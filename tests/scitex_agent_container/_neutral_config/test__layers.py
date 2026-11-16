"""ADR-0033: where the user and project layers live."""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._neutral_config import (
    find_project_root,
    project_layer_root,
    user_layer_root,
)


@pytest.fixture
def env():
    """Set real environment variables for one test; restore them afterwards."""
    saved: dict[str, str | None] = {}

    def set_var(name: str, value: str | None) -> None:
        saved.setdefault(name, os.environ.get(name))
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    yield set_var
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_user_layer_honours_override(env, tmp_path):
    # Arrange
    env("SAC_AGENTS_DIR", str(tmp_path / "layer"))
    # Act
    root = user_layer_root()
    # Assert
    assert root == tmp_path / "layer"


def test_user_layer_default_is_under_scitex_agent_container(env, tmp_path):
    # Arrange
    env("SAC_AGENTS_DIR", None)
    env("HOME", str(tmp_path))
    # Act
    root = user_layer_root()
    # Assert
    assert root == tmp_path / ".scitex" / "agent-container" / ".agents"


def test_project_root_is_nearest_git_ancestor(tmp_path):
    # Arrange
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "a" / "b").mkdir(parents=True)
    # Act
    root = find_project_root(tmp_path / "repo" / "a" / "b")
    # Assert
    assert root == tmp_path / "repo"


def test_project_layer_sits_under_the_repo_scitex_dir(tmp_path):
    # Arrange
    repo = tmp_path / "repo"
    # Act
    root = project_layer_root(repo)
    # Assert
    assert root == repo / ".scitex" / "agent-container" / ".agents"
