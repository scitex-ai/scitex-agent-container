"""The two measuring instruments of the ADR-0024 migration.

``tree_size`` produces the number an operator decides on and ``verify_copy``
is the instrument that licenses the ``rmtree``, so each is tested from BOTH
sides: it must be able to say "same" (the positive control) and it must
actually catch the specific difference it exists to catch.

Real directories, real files, real hard links and real symlinks on disk — a
faked stat would test our idea of the filesystem, which is the idea that
would be wrong. STX-TQ002 AAA markers; one fact per test (PA-307).
"""

from __future__ import annotations

import os
from pathlib import Path

from scitex_agent_container._maintenance._scratch_migrate_measure import (
    tree_size,
    verify_copy,
)


# ---------------------------------------------------------------------------
# tree_size — the number in the preview
# ---------------------------------------------------------------------------


def test_tree_size_counts_the_bytes_it_would_move(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "t"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "uv").write_text("x" * 40, encoding="utf-8")
    # Act
    size, _files = tree_size(root)
    # Assert
    assert size == 40


def test_tree_size_counts_the_files_it_would_move(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "t"
    root.mkdir()
    (root / "a").write_text("a", encoding="utf-8")
    (root / "b").write_text("b", encoding="utf-8")
    # Act
    _size, files = tree_size(root)
    # Assert
    assert files == 2


def test_tree_size_counts_a_hard_link_once(tmp_path: Path) -> None:
    # Arrange — a uv cache is full of hard links; counting them per-link
    # would promise the operator space the move cannot free.
    root = tmp_path / "t"
    root.mkdir()
    (root / "a").write_text("x" * 100, encoding="utf-8")
    os.link(root / "a", root / "b")
    # Act
    size, _files = tree_size(root)
    # Assert
    assert size == 100


def test_two_separate_files_of_the_same_size_are_both_counted(
    tmp_path: Path,
) -> None:
    # Arrange — the positive control for the row above: the de-duplication
    # keys on the inode, not on the size.
    root = tmp_path / "t"
    root.mkdir()
    (root / "a").write_text("x" * 100, encoding="utf-8")
    (root / "b").write_text("y" * 100, encoding="utf-8")
    # Act
    size, _files = tree_size(root)
    # Assert
    assert size == 200


def test_tree_size_of_an_absent_tree_is_zero(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "never-created"
    # Act
    measured = tree_size(root)
    # Assert
    assert measured == (0, 0)


def test_tree_size_of_a_file_is_zero(tmp_path: Path) -> None:
    # Arrange — the source must be a DIRECTORY; a file is not a tree.
    path = tmp_path / "f"
    path.write_text("x" * 10, encoding="utf-8")
    # Act
    measured = tree_size(path)
    # Assert
    assert measured == (0, 0)


# ---------------------------------------------------------------------------
# verify_copy — the instrument that licenses the delete
# ---------------------------------------------------------------------------


def test_verify_reports_nothing_for_an_identical_tree(tmp_path: Path) -> None:
    # Arrange — the positive control: the instrument can say "same".
    src, dst = tmp_path / "s", tmp_path / "d"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f").write_text("data", encoding="utf-8")
    (dst / "sub").mkdir(parents=True)
    (dst / "sub" / "f").write_text("data", encoding="utf-8")
    # Act
    problems = verify_copy(src, dst)
    # Assert
    assert problems == []


def test_verify_reports_a_file_missing_from_the_copy(tmp_path: Path) -> None:
    # Arrange
    src, dst = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "f").write_text("data", encoding="utf-8")
    # Act
    problems = verify_copy(src, dst)
    # Assert
    assert problems == ["missing in copy: f"]


def test_verify_reports_a_file_that_differs_in_size(tmp_path: Path) -> None:
    # Arrange — a truncated copy is the failure a byte count catches.
    src, dst = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "f").write_text("data", encoding="utf-8")
    (dst / "f").write_text("dat", encoding="utf-8")
    # Act
    problems = verify_copy(src, dst)
    # Assert
    assert problems[0].startswith("differs: f")


def test_verify_reports_a_missing_directory(tmp_path: Path) -> None:
    # Arrange — an empty directory carries no bytes, so only the manifest
    # can notice it went missing.
    src, dst = tmp_path / "s", tmp_path / "d"
    (src / "empty").mkdir(parents=True)
    dst.mkdir()
    # Act
    problems = verify_copy(src, dst)
    # Assert
    assert problems == ["missing in copy: empty"]


def test_verify_reports_a_symlink_retargeted_by_the_copy(tmp_path: Path) -> None:
    # Arrange — a venv is mostly symlinks; one pointing elsewhere is a
    # broken interpreter, not a smaller file.
    src, dst = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "python").symlink_to("uv")
    (dst / "python").symlink_to("somewhere-else")
    # Act
    problems = verify_copy(src, dst)
    # Assert
    assert problems[0].startswith("differs: python")


def test_verify_reports_a_symlink_the_copy_dereferenced(tmp_path: Path) -> None:
    # Arrange — copying with ``symlinks=False`` turns a link into a file,
    # which inflates the tree and must not read as verified.
    src, dst = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "uv").write_text("payload", encoding="utf-8")
    (src / "python").symlink_to("uv")
    (dst / "uv").write_text("payload", encoding="utf-8")
    (dst / "python").write_text("payload", encoding="utf-8")
    # Act
    problems = verify_copy(src, dst)
    # Assert
    assert problems[0].startswith("differs: python")


def test_verify_reports_an_extra_path_in_the_copy(tmp_path: Path) -> None:
    # Arrange — a destination with someone else's files in it is not this
    # tree, and must not be mistaken for a verified copy of it.
    src, dst = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (dst / "stowaway").write_text("not in the source", encoding="utf-8")
    # Act
    problems = verify_copy(src, dst)
    # Assert
    assert problems == ["extra in copy: stowaway"]
