"""Tests for ``_to_home_deployers`` — the same-file (linked-host-file) guard.

No mocks: real files + real symlinks under ``tmp_path``. AAA-marked, one
assert per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.runtimes._to_home_deployers import (
    _deploy_plain_file,
    _dst_resolves_to_source,
)


class TestDstResolvesToSource:
    def test_true_when_dst_symlinks_to_source(self, tmp_path) -> None:
        # Arrange
        src = tmp_path / "s.md"
        src.write_text("x")
        dst = tmp_path / "d.md"
        dst.symlink_to(src)
        # Act
        result = _dst_resolves_to_source(src, dst)
        # Assert
        assert result is True

    def test_false_for_two_distinct_files(self, tmp_path) -> None:
        # Arrange
        src = tmp_path / "s.md"
        src.write_text("x")
        dst = tmp_path / "d.md"
        dst.write_text("y")
        # Act
        result = _dst_resolves_to_source(src, dst)
        # Assert
        assert result is False

    def test_false_when_dst_missing(self, tmp_path) -> None:
        # Arrange
        src = tmp_path / "s.md"
        src.write_text("x")
        dst = tmp_path / "does-not-exist.md"
        # Act
        result = _dst_resolves_to_source(src, dst)
        # Assert
        assert result is False


class TestDeployPlainFileSameFileGuard:
    def test_symlink_back_to_source_does_not_corrupt_source(self, tmp_path) -> None:
        # Arrange — dst is a "linked host file" symlink back to src.
        src = tmp_path / "autonomous.md"
        src.write_text("HOST-CONTENT")
        dst = tmp_path / "home" / "autonomous.md"
        dst.parent.mkdir(parents=True)
        dst.symlink_to(src)
        # Act — must NOT raise SameFileError, must NOT write through the link.
        _deploy_plain_file(src, dst, config=None, rel=Path("commands/autonomous.md"))
        # Assert
        assert src.read_text() == "HOST-CONTENT"

    def test_distinct_dst_is_still_overwritten(self, tmp_path) -> None:
        # Arrange — a normal (non-linked) dst still gets the full overwrite.
        src = tmp_path / "src.md"
        src.write_text("NEW")
        dst = tmp_path / "dst.md"
        dst.write_text("OLD")
        # Act
        _deploy_plain_file(src, dst, config=None, rel=Path("commands/x.md"))
        # Assert
        assert dst.read_text() == "NEW"
