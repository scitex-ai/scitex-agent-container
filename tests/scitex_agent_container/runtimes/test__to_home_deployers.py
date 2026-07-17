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


class TestDeployPlainFileSymlinkToOtherTarget:
    """A leftover ``dst`` symlink pointing at a DIFFERENT file than ``src``
    (e.g. a prior host-merge link ``$HOME/.claude/hooks/x -> ~/.claude/hooks/x``
    left in place after ``x`` moves into the agent baseline) must be REPLACED
    with a real hermetic file — NEVER written through.

    Writing through the link would (a) CORRUPT the operator's real host file
    the link points at, and (b) leave a non-hermetic symlink in the container
    ``$HOME``. The existing same-source guard (:func:`_dst_resolves_to_source`)
    only covers a symlink back to ``src`` itself (INCIDENT 2026-07-02); this is
    its unguarded sibling — a symlink to a DIFFERENT target.
    """

    def _arrange(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        src = tmp_path / "baseline" / "deny_edit.sh"
        src.parent.mkdir(parents=True)
        src.write_text("NEW-baseline")
        host = tmp_path / "host" / "deny_edit.sh"
        host.parent.mkdir(parents=True)
        host.write_text("HOST-ORIGINAL")
        dst = tmp_path / "home" / "deny_edit.sh"
        dst.parent.mkdir(parents=True)
        dst.symlink_to(host)
        return src, host, dst

    def test_dst_becomes_a_real_file_with_src_content(self, tmp_path) -> None:
        # Arrange
        src, _host, dst = self._arrange(tmp_path)
        # Act
        _deploy_plain_file(
            src, dst, config=None, rel=Path("hooks/pre-tool-use/deny_edit.sh")
        )
        # Assert — a real file (not a symlink) carrying the current src content.
        assert not dst.is_symlink() and dst.read_text() == "NEW-baseline"

    def test_pointed_at_host_file_is_not_corrupted(self, tmp_path) -> None:
        # Arrange
        src, host, dst = self._arrange(tmp_path)
        # Act
        _deploy_plain_file(
            src, dst, config=None, rel=Path("hooks/pre-tool-use/deny_edit.sh")
        )
        # Assert — the host file the stale link pointed at is byte-for-byte intact.
        assert host.read_text() == "HOST-ORIGINAL"

    def test_broken_symlink_dst_is_replaced_with_real_file(self, tmp_path) -> None:
        # Arrange — dst is a DANGLING symlink (host target already removed).
        src = tmp_path / "baseline" / "x.sh"
        src.parent.mkdir(parents=True)
        src.write_text("NEW")
        dst = tmp_path / "home" / "x.sh"
        dst.parent.mkdir(parents=True)
        dst.symlink_to(tmp_path / "gone" / "x.sh")
        # Act
        _deploy_plain_file(src, dst, config=None, rel=Path("hooks/stop/x.sh"))
        # Assert
        assert not dst.is_symlink() and dst.read_text() == "NEW"
