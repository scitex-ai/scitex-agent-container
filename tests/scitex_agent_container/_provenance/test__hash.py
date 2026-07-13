"""Tests for the content hash — the identity that cannot lie.

Real files on disk in ``tmp_path``. The property that matters: the digest
MOVES when the code moves, which is the one thing a declared version
string never did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._provenance._hash import code_hash, iter_py_files


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A tiny package tree: two modules in a subpackage."""
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").write_text("VALUE = 1\n")
    (root / "sub" / "mod.py").write_text("def f():\n    return 1\n")
    return root


class TestCodeHash:
    def test_same_tree_hashes_the_same(self, package: Path):
        # Arrange
        first = code_hash(package)

        # Act
        second = code_hash(package)

        # Assert
        assert second == first

    def test_editing_a_file_changes_the_hash(self, package: Path):
        # Arrange
        before = code_hash(package)

        # Act — the exact scenario a version string cannot see.
        (package / "sub" / "mod.py").write_text("def f():\n    return 2\n")
        after = code_hash(package)

        # Assert
        assert after != before

    def test_adding_a_file_changes_the_hash(self, package: Path):
        # Arrange
        before = code_hash(package)

        # Act
        (package / "sub" / "extra.py").write_text("X = 1\n")
        after = code_hash(package)

        # Assert
        assert after != before

    def test_renaming_a_file_changes_the_hash(self, package: Path):
        # Arrange — content is identical; only the path moved. Hashing the
        # relative path alongside the bytes is what catches this.
        before = code_hash(package)

        # Act
        (package / "sub" / "mod.py").rename(package / "sub" / "renamed.py")
        after = code_hash(package)

        # Assert
        assert after != before

    def test_the_build_stamp_itself_is_excluded(self, package: Path):
        # Arrange — _build_info.py is GENERATED FROM this hash, so counting
        # it would make the build-time digest unmatchable by construction.
        before = code_hash(package)

        # Act
        (package / "_build_info.py").write_text("STAMP = {'commit': 'abc'}\n")
        after = code_hash(package)

        # Assert
        assert after == before

    def test_non_python_files_do_not_move_the_hash(self, package: Path):
        # Arrange
        before = code_hash(package)

        # Act
        (package / "notes.txt").write_text("irrelevant\n")
        after = code_hash(package)

        # Assert
        assert after == before

    def test_missing_directory_hashes_to_none(self, tmp_path: Path):
        # Arrange
        absent = tmp_path / "nope"

        # Act
        found = code_hash(absent)

        # Assert
        assert found is None


class TestIterPyFiles:
    def test_pycache_directories_are_never_hashed(self, package: Path):
        # Arrange
        cache = package / "__pycache__"
        cache.mkdir()
        (cache / "stale.py").write_text("X = 1\n")

        # Act
        found = iter_py_files(package)

        # Assert
        assert not any("__pycache__" in str(p) for p in found)

# EOF
