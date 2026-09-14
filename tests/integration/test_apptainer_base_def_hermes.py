from pathlib import Path

import tomllib

from scitex_agent_container.cli_pkg._hermes_source import (
    HERMES_COMMIT,
    HERMES_REPOSITORY,
)
from scitex_agent_container.cli_pkg.image_group import _LAYERS

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "src" / "scitex_agent_container" / "containers" / "apptainer-base.def"
DOCS = ROOT / "docs" / "images.md"


def test_base_image_installs_pinned_local_hermes_source():
    # Arrange
    expected = (
        "hermes-agent-src /opt/hermes-agent",
        'uv sync --project "$HERMES_ROOT" --frozen --no-dev',
        'ln -sf "$HERMES_ROOT/.venv/bin/hermes"',
        f"org.scitex.hermes.commit {HERMES_COMMIT}",
        f"org.scitex.hermes.repository {HERMES_REPOSITORY}",
        'cat "$HERMES_ROOT/SAC_UPSTREAM_COMMIT"',
        'cat "$HERMES_ROOT/SAC_UPSTREAM_REPOSITORY"',
        'assert hermes_cli.__version__ == "0.21.1"',
    )
    # Act
    text = RECIPE.read_text()

    # Assert
    assert all(item in text for item in expected)


def test_base_image_builds_and_verifies_official_hermes_tui():
    # Arrange
    expected = (
        "setup_22.x",
        'npm ci --prefix "$HERMES_ROOT" --workspace ui-tui',
        'npm run --prefix "$HERMES_ROOT" build --workspace ui-tui',
        'node --check "$HERMES_ROOT/ui-tui/dist/entry.js"',
        'test -s "$HERMES_ROOT/ui-tui/dist/entry.js"',
        'rm -rf "$HERMES_ROOT/node_modules"',
        "HERMES_TUI_DIR=/opt/hermes-agent/ui-tui",
    )
    # Act
    text = RECIPE.read_text()

    # Assert
    assert all(item in text for item in expected)


def test_hermes_is_a_base_capability_not_an_image_layer():
    # Arrange
    expected_layers = {"base", "scitex", "proxy"}
    # Act
    docs = DOCS.read_text()
    is_base_capability = (
        set(_LAYERS) == expected_layers
        and "Hermes is a capability of `:base`, not a separate `:hermes` layer." in docs
        and "sac image build hermes" not in docs
    )
    # Assert
    assert is_base_capability


def test_pyproject_declares_external_hermes_harness_package():
    # Arrange
    expected = {
        "package": "hermes-agent",
        "version": "0.21.1",
        "repository": HERMES_REPOSITORY,
        "commit": HERMES_COMMIT,
        "installation": "local-staged-source",
        "environment": "/opt/hermes-agent/.venv",
    }
    # Act
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = data["tool"]["scitex-agent-container"]["harnesses"]["hermes"]
    is_external = not any(
        requirement.startswith("hermes-agent")
        for requirement in data["project"]["dependencies"]
    )

    # Assert
    assert declared == expected and is_external
