from __future__ import annotations

import ast
from pathlib import Path

import tomllib
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parents[2]


def test_psutil_is_a_core_runtime_dependency() -> None:
    # Arrange
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())

    # Act
    requirements = {
        Requirement(value).name for value in pyproject["project"]["dependencies"]
    }

    # Assert
    assert "psutil" in requirements


def test_clean_start_modules_import_the_declared_psutil_runtime() -> None:
    # Arrange
    modules = (
        _ROOT
        / "src/scitex_agent_container/runtimes/_inbox_sidecar_reconcile.py",
        _ROOT / "src/scitex_agent_container/runtimes/_hermes_stale_recovery.py",
    )

    # Act
    importers = {
        path.name: any(
            alias.name.split(".", 1)[0] == "psutil"
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        for path in modules
    }

    # Assert
    assert importers == {
        "_inbox_sidecar_reconcile.py": True,
        "_hermes_stale_recovery.py": True,
    }
