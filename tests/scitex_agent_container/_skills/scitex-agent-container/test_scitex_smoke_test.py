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
    assert SCRIPT.is_file()
    text = SCRIPT.read_text(encoding="utf-8")
    # Must be syntactically valid Python.
    compile(text, str(SCRIPT), "exec")
