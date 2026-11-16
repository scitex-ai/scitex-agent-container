"""ADR-0033: the neutral agent layer projected into sac's Hermes profiles."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._neutral_config import load_layer
from scitex_agent_container.runtimes._hermes_neutral import (
    MARKER,
    SKILLS_DIR,
    materialize_neutral_skills,
    merge_neutral_mcp,
    neutral_layer_for,
    neutral_mcp_servers,
)
from tests.scitex_agent_container._neutral_config._layer_fixture import make_layer


@pytest.fixture
def env():
    """Set real environment variables for one test; restore them afterwards."""
    saved: dict[str, str | None] = {}

    def set_var(name: str, value: str) -> None:
        saved.setdefault(name, os.environ.get(name))
        os.environ[name] = value

    yield set_var
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def layer(tmp_path):
    root = make_layer(
        tmp_path / "u",
        skills=("alpha", "scitex"),
        settings={"external_skills": ["scitex"]},
    )
    (root / "mcp.yaml").write_text(
        yaml.safe_dump(
            {
                "servers": {
                    "tools": {
                        "transport": "stdio",
                        "command": "x",
                        "args": ["serve"],
                        "env": {"K": "${K}"},
                    },
                    "web": {
                        "transport": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"A": "b"},
                    },
                }
            }
        )
    )
    return load_layer(root)


def test_skill_is_copied_into_the_profile(layer, tmp_path):
    # Arrange
    profile = tmp_path / "profile"
    # Act
    materialize_neutral_skills(layer, [profile])
    # Assert
    assert (profile / SKILLS_DIR / "alpha" / "SKILL.md").is_file()


def test_copied_skill_is_a_real_dir_not_a_link(layer, tmp_path):
    # Arrange
    profile = tmp_path / "profile"
    # Act
    materialize_neutral_skills(layer, [profile])
    # Assert
    assert not (profile / SKILLS_DIR / "alpha").is_symlink()


def test_external_skill_is_not_projected(layer, tmp_path):
    # Arrange
    profile = tmp_path / "profile"
    # Act
    materialize_neutral_skills(layer, [profile])
    # Assert
    assert not (profile / SKILLS_DIR / "scitex").exists()


def test_projection_returns_hermes_relative_dir(layer, tmp_path):
    # Arrange
    profile = tmp_path / "profile"
    # Act
    dirs = materialize_neutral_skills(layer, [profile])
    # Assert
    assert dirs == [SKILLS_DIR]


def test_unmanaged_existing_dir_is_refused(layer, tmp_path):
    # Arrange
    profile = tmp_path / "profile"
    (profile / SKILLS_DIR).mkdir(parents=True)
    # Act
    projecting = pytest.raises(ValueError, match="unmanaged")
    # Assert
    with projecting:
        materialize_neutral_skills(layer, [profile])


def test_reprojection_drops_a_removed_skill(tmp_path):
    # Arrange
    root = make_layer(tmp_path / "u", skills=("alpha", "beta"))
    profile = tmp_path / "profile"
    materialize_neutral_skills(load_layer(root), [profile])
    (root / "skills" / "beta" / "SKILL.md").unlink()
    (root / "skills" / "beta").rmdir()
    # Act
    materialize_neutral_skills(load_layer(root), [profile])
    # Assert
    assert not (profile / SKILLS_DIR / "beta").exists()


def test_projection_marks_its_directory(layer, tmp_path):
    # Arrange
    profile = tmp_path / "profile"
    # Act
    materialize_neutral_skills(layer, [profile])
    # Assert
    assert (profile / SKILLS_DIR / MARKER).is_file()


def test_stdio_server_keeps_command_args_env(layer):
    # Arrange
    source = layer
    # Act
    servers = neutral_mcp_servers(source)
    # Assert
    assert servers["tools"] == {"command": "x", "args": ["serve"], "env": {"K": "${K}"}}


def test_http_server_keeps_url_and_headers(layer):
    # Arrange
    source = layer
    # Act
    servers = neutral_mcp_servers(source)
    # Assert
    assert servers["web"] == {
        "url": "https://example.invalid/mcp",
        "headers": {"A": "b"},
    }


def test_agent_server_wins_on_name_clash(layer):
    # Arrange
    agent = {"tools": {"command": "agent-own"}}
    # Act
    merged, _ = merge_neutral_mcp(layer, agent, ["mcp-tools"])
    # Assert
    assert merged["tools"] == {"command": "agent-own"}


def test_added_server_is_loaded_eagerly(layer):
    # Arrange
    agent: dict = {}
    # Act
    _, toolsets = merge_neutral_mcp(layer, agent, [])
    # Assert
    assert toolsets == ["mcp-tools", "mcp-web"]


def test_project_layer_skill_overrides_user_layer(env, tmp_path):
    # Arrange
    make_layer(tmp_path / "user", skills=("alpha",))
    env("SAC_AGENTS_DIR", str(tmp_path / "user"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    make_layer(repo / ".scitex" / "agent-container" / ".agents", skills=("alpha",))
    config = type("Cfg", (), {"workdir": str(repo)})()
    # Act
    layer = neutral_layer_for(config)
    # Assert
    assert (
        layer.skills["alpha"]
        == repo / ".scitex" / "agent-container" / ".agents" / "skills" / "alpha"
    )


def test_agent_without_workdir_uses_user_layer(env, tmp_path):
    # Arrange
    make_layer(tmp_path / "user", skills=("alpha",))
    env("SAC_AGENTS_DIR", str(tmp_path / "user"))
    config = type("Cfg", (), {"workdir": None})()
    # Act
    layer = neutral_layer_for(config)
    # Assert
    assert layer.skills["alpha"] == Path(tmp_path / "user" / "skills" / "alpha")
