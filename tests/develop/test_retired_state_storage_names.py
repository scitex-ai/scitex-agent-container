"""Keep the deleted per-agent SQLite contract out of active source."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "scitex_agent_container"
RETIRED_MODULE_STEM = "state" + "_db"
RETIRED_ENV = "SCITEX_AGENT_CONTAINER_STATE" + "_DB"


def _python_sources() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def test_retired_module_name_is_absent_from_source_paths() -> None:
    # Arrange
    paths = _python_sources()
    # Act
    offenders = [
        path.relative_to(ROOT) for path in paths if RETIRED_MODULE_STEM in path.name
    ]
    # Assert
    assert offenders == []


def test_retired_module_name_is_absent_from_source_imports() -> None:
    # Arrange
    paths = _python_sources()
    # Act
    offenders = [
        path.relative_to(ROOT)
        for path in paths
        if RETIRED_MODULE_STEM in path.read_text(encoding="utf-8")
    ]
    # Assert
    assert offenders == []


def test_retired_database_env_is_absent_from_source() -> None:
    # Arrange
    paths = _python_sources()
    # Act
    offenders = [
        path.relative_to(ROOT)
        for path in paths
        if RETIRED_ENV in path.read_text(encoding="utf-8")
    ]
    # Assert
    assert offenders == []


def test_state_store_facade_is_the_only_current_facade() -> None:
    # Arrange
    current = SOURCE / "_state" / "state_store.py"
    retired = SOURCE / "_state" / f"{RETIRED_MODULE_STEM}.py"
    # Act
    facade_state = (current.is_file(), retired.exists())
    # Assert
    assert facade_state == (True, False)


def test_source_never_imports_sqlite_runtime() -> None:
    # Arrange
    paths = _python_sources()
    # Act
    offenders: list[Path] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        if any(
            (isinstance(node, ast.ImportFrom) and node.module == "sqlite3")
            or (
                isinstance(node, ast.Import)
                and any(alias.name == "sqlite3" for alias in node.names)
            )
            for node in imports
        ):
            offenders.append(path.relative_to(ROOT))
    # Assert
    assert offenders == []


def test_source_has_no_sqlite_connection_that_can_create_a_state_file() -> None:
    # Arrange
    paths = _python_sources()
    connect_spelling = "sqlite3" + ".connect"
    # Act
    offenders = [
        path.relative_to(ROOT)
        for path in paths
        if connect_spelling in path.read_text(encoding="utf-8")
    ]
    # Assert
    assert offenders == []


def test_store_cli_replaces_the_database_noun_without_an_alias() -> None:
    # Arrange
    source = (SOURCE / "cli_pkg" / "_main.py").read_text(encoding="utf-8")
    current_entry = '"store": f"{_PKG}.store_group:store_group"'
    retired_entry = '"' + "db" + '":'
    # Act
    command_state = (current_entry in source, retired_entry in source)
    # Assert
    assert command_state == (True, False)


def test_store_python_api_replaces_the_database_noun_without_an_alias() -> None:
    # Arrange
    api_root = SOURCE / "_api"
    # Act
    module_state = ((api_root / "store.py").is_file(), (api_root / "db.py").exists())
    # Assert
    assert module_state == (True, False)
