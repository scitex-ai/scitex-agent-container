from __future__ import annotations

from pathlib import Path

import tomllib
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parents[2]


def test_psutil_is_a_core_runtime_dependency() -> None:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    requirements = {
        Requirement(value).name for value in pyproject["project"]["dependencies"]
    }

    assert "psutil" in requirements
