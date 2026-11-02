from __future__ import annotations

import ast
from importlib.metadata import version
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
        _ROOT / "src/scitex_agent_container/runtimes/_inbox_sidecar_reconcile.py",
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


def test_gui_and_dev_share_one_exact_sdk_gui_requirement() -> None:
    # Arrange
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]

    # Act
    declared = {
        extra: [
            Requirement(value)
            for value in extras[extra]
            if Requirement(value).name == "scitex-sdk"
        ]
        for extra in ("gui", "dev")
    }
    contracts = {
        extra: [(str(item.specifier), sorted(item.extras)) for item in items]
        for extra, items in declared.items()
    }
    gui = declared["gui"]
    exact = (
        len(gui) == 1
        and len(list(gui[0].specifier)) == 1
        and next(iter(gui[0].specifier)).operator == "=="
    )

    # Assert
    assert (
        contracts["gui"] == contracts["dev"],
        exact,
        len(gui) == 1 and gui[0].extras == {"gui"},
    ) == (True, True, True)


def test_installed_sdk_satisfies_the_render_cohort_requirement() -> None:
    # Arrange
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
    requirements = [
        Requirement(value) for value in project["optional-dependencies"]["gui"]
    ]
    sdk = next(item for item in requirements if item.name == "scitex-sdk")

    # Act
    installed = version("scitex-sdk")

    # Assert
    assert installed in sdk.specifier
