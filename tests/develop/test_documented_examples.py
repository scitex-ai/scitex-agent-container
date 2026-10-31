"""Execute opt-in examples from README, docs, and shipped skills in daily CI.

Use scitex-dev's Markdown block parser; only examples explicitly marked
``# sac-doc-check`` run. Mark self-contained, offline examples, never commands
that launch agents or modify operator state.
"""

from pathlib import Path
import os
import subprocess
import sys

import pytest

from scitex_dev.linter._md import _iter_python_blocks


ROOT = Path(__file__).resolve().parents[2]


def _examples():
    paths = [ROOT / "README.md"]
    paths.extend(sorted((ROOT / "docs").rglob("*.md")))
    paths.extend(sorted((ROOT / "src/scitex_agent_container/_skills").rglob("*.md")))
    examples = []
    for path in paths:
        for _, line, body in _iter_python_blocks(path.read_text(encoding="utf-8")):
            if "# sac-doc-check" in body.splitlines():
                examples.append(pytest.param(body, id=f"{path.relative_to(ROOT)}:{line}"))
    if not examples:
        raise RuntimeError("No executable documentation examples found in this checkout")
    return examples


@pytest.mark.parametrize("source", _examples())
def test_documented_example_executes_successfully(source, tmp_path):
    # Arrange: imports come from this checkout; execution and HOME are isolated.
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / ".hermes"))
    env["PYTHONPATH"] = str(ROOT / "src")
    # Act: run the documented assertions against actual implementations.
    result = subprocess.run(
        [sys.executable, "-c", source], cwd=tmp_path, env=env,
        text=True, capture_output=True, timeout=30,
    )
    # Assert
    assert result.returncode == 0, result.stdout + result.stderr
