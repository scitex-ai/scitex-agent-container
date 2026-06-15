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
def test_example_spec_runtime_is_apptainer(spec):
    # Arrange
    from scitex_agent_container.config import load_config

    # Act
    cfg = load_config(str(spec))
    # Assert — every example spec EXPLICITLY pins runtime: apptainer
    # (incl. the proxy/provider examples, which need the SDK path, not
    # the interactive TUI). The DEFAULT-when-omitted is tui — covered in
    # test_v3_spec_structure.test_runtime_defaults_to_tui_when_omitted.
    assert cfg.runtime == "apptainer"


def test_full_example_to_home_contains_claude_md():
    # Arrange
    to_home = EXAMPLES_DIR / "full-agent" / "to_home"
    # Act
    path = to_home / "CLAUDE.md"
    # Assert
    assert path.exists()


def test_full_example_to_home_contains_mcp_json():
    # Arrange
    to_home = EXAMPLES_DIR / "full-agent" / "to_home"
    # Act
    path = to_home / ".mcp.json"
    # Assert
    assert path.exists()


def test_full_example_to_home_contains_env_example():
    # Arrange
    to_home = EXAMPLES_DIR / "full-agent" / "to_home"
    # Act
    path = to_home / ".env.example"
    # Assert
    assert path.exists()


def test_full_example_to_home_contains_commands_dir():
    # Arrange
    to_home = EXAMPLES_DIR / "full-agent" / "to_home"
    # Act
    path = to_home / ".claude" / "commands"
    # Assert
    assert path.is_dir()


def test_full_example_to_home_contains_skills_dir():
    # Arrange
    to_home = EXAMPLES_DIR / "full-agent" / "to_home"
    # Act
    path = to_home / ".claude" / "skills"
    # Assert
    assert path.is_dir()


def test_full_example_to_home_contains_hooks_dir():
    # Arrange
    to_home = EXAMPLES_DIR / "full-agent" / "to_home"
    # Act
    path = to_home / ".claude" / "hooks"
    # Assert
    assert path.is_dir()


def test_minimal_example_has_no_to_home_directory():
    # Arrange
    minimal_dir = EXAMPLES_DIR / "minimal-agent"
    # Act
    to_home = minimal_dir / "to_home"
    # Assert
    assert not to_home.exists()
