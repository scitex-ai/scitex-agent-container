"""Smoke test for the scitex_smoke_test.py skill artifact."""

from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_skills"
    / "scitex-agent-container"
    / "scitex_smoke_test.py"
)


def test_skill_script_exists_and_is_python():
    # Arrange
    script = SCRIPT
    # Act
    text = script.read_text(encoding="utf-8") if script.is_file() else None
    compiled = compile(text, str(script), "exec") if text is not None else None
    # Assert
    assert compiled is not None
