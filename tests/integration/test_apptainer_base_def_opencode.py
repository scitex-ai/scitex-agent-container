from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "src" / "scitex_agent_container" / "containers" / "apptainer-base.def"


def test_base_image_installs_and_verifies_pinned_opencode_cli():
    # Arrange
    text = RECIPE.read_text()

    # Act
    has_image_contract = all(
        item in text
        for item in (
            "opencode-ai@1.18.31",
            "XDG_CONFIG_HOME=/tmp/.config",
            "XDG_DATA_HOME=/tmp/.local/share",
            "XDG_CACHE_HOME=/tmp/.cache",
            "XDG_STATE_HOME=/tmp/.local/state",
            "BUN_INSTALL_CACHE_DIR=/tmp/.bun-cache",
            "/opt/npm-global/bin/opencode --version",
            " opencode apptainer ",
        )
    )

    # Assert
    assert has_image_contract


def test_base_image_never_bakes_opencode_go_credentials():
    # Arrange
    text = RECIPE.read_text()

    # Act
    has_embedded_credential = any(
        marker in text for marker in ("OPENCODE_GO_API_KEY", "sk-Ze")
    )

    # Assert
    assert not has_embedded_credential
