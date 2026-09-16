from __future__ import annotations

import tomllib
from pathlib import Path


def test_psutil_is_a_core_runtime_dependency() -> None:
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]

    names = {
        dependency.split("[", 1)[0].split("<", 1)[0].split(">", 1)[0].split("=", 1)[0]
        for dependency in project["dependencies"]
    }

    assert "psutil" in names
