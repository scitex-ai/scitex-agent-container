from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "src" / "scitex_agent_container" / "containers" / "apptainer-base.def"


def test_base_image_installs_and_verifies_pinned_opencode_cli():
    text = RECIPE.read_text()

    assert "opencode-ai@1.18.31" in text
    assert "/opt/npm-global/bin/opencode --version" in text
    assert " opencode apptainer " in text


def test_base_image_never_bakes_opencode_go_credentials():
    text = RECIPE.read_text()

    assert "OPENCODE_GO_API_KEY" not in text
    assert "sk-Ze" not in text
