"""Every shipped ``examples/agents/*/spec.yaml`` must load clean under v3.

``examples/agents/`` uses dir-as-SSoT layout: each subdirectory is an
agent example that users copy and customise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples" / "agents"


@pytest.mark.parametrize(
    "spec",
    sorted(EXAMPLES_DIR.glob("*/spec.yaml")),
    ids=lambda p: p.parent.name,
)
def test_example_spec_name_matches_directory_name(spec):
    # Arrange
    from scitex_agent_container.config import load_config

    # Act
    cfg = load_config(str(spec))
    # Assert
    assert cfg.name == spec.parent.name


@pytest.mark.parametrize(
    "spec",
    sorted(EXAMPLES_DIR.glob("*/spec.yaml")),
    ids=lambda p: p.parent.name,
)
def test_example_spec_runtime_defaults_to_apptainer(spec):
    # Arrange
    from scitex_agent_container.config import load_config

    # Act
    cfg = load_config(str(spec))
    # Assert
    assert cfg.runtime == "apptainer"


def test_full_example_dot_claude_contains_claude_md():
    # Arrange
    dot_claude = EXAMPLES_DIR / "full-agent" / "dot_claude"
    # Act
    path = dot_claude / "CLAUDE.md"
    # Assert
    assert path.exists()


def test_full_example_dot_claude_contains_mcp_json():
    # Arrange
    dot_claude = EXAMPLES_DIR / "full-agent" / "dot_claude"
    # Act
    path = dot_claude / ".mcp.json"
    # Assert
    assert path.exists()


def test_full_example_dot_claude_contains_env_example():
    # Arrange
    dot_claude = EXAMPLES_DIR / "full-agent" / "dot_claude"
    # Act
    path = dot_claude / ".env.example"
    # Assert
    assert path.exists()


def test_full_example_dot_claude_contains_commands_dir():
    # Arrange
    dot_claude = EXAMPLES_DIR / "full-agent" / "dot_claude"
    # Act
    path = dot_claude / "commands"
    # Assert
    assert path.is_dir()


def test_full_example_dot_claude_contains_skills_dir():
    # Arrange
    dot_claude = EXAMPLES_DIR / "full-agent" / "dot_claude"
    # Act
    path = dot_claude / "skills"
    # Assert
    assert path.is_dir()


def test_full_example_dot_claude_contains_hooks_dir():
    # Arrange
    dot_claude = EXAMPLES_DIR / "full-agent" / "dot_claude"
    # Act
    path = dot_claude / "hooks"
    # Assert
    assert path.is_dir()


def test_minimal_example_has_no_dot_claude_directory():
    # Arrange
    minimal_dir = EXAMPLES_DIR / "minimal-agent"
    # Act
    dot_claude = minimal_dir / "dot_claude"
    # Assert
    assert not dot_claude.exists()
