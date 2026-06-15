"""Tests for :mod:`scitex_agent_container.runtimes._symlink_resolve`.

Real I/O against tmp_path trees + real symlinks + real
``shutil.copytree``. No mocks. AAA layout, one assert per test.

The module's primary contract — dereference-copy a ``to_home/`` symlink
to real content at the destination — is exercised here along with the
2026-06-04 F-CS8 fix: ``worktrees/`` subtrees inside the resolved
target are EXCLUDED from the copy via :func:`_walk_exclusions.copytree_ignore`.
Without this, a baseline symlink like
``_shared/to_home/.claude/skills -> ~/.claude/skills`` would
transitively pull every git worktree nested under the host
``~/.claude/`` into the container overlay at start time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.runtimes._symlink_resolve import (
    DanglingToHomeSymlinkError,
    deref_copy_symlink,
)

# ---------------------------------------------------------------------------
# Baseline contract — dereference real content
# ---------------------------------------------------------------------------


def test_deref_copy_symlink_copies_resolved_directory_into_dst(tmp_path: Path):
    """Symlink → real dir; deref-copy lands real dir at dst."""
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    (real / "kept.txt").write_text("hello")
    src = tmp_path / "src"
    src.symlink_to(real)
    dst = tmp_path / "dst"
    # Act
    deref_copy_symlink(src, dst)
    # Assert
    assert (dst / "kept.txt").read_text() == "hello"


def test_deref_copy_symlink_destination_is_not_a_symlink(tmp_path: Path):
    """The dst must be real content — never a symlink itself."""
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    (real / "x.txt").write_text("x")
    src = tmp_path / "src"
    src.symlink_to(real)
    dst = tmp_path / "dst"
    # Act
    deref_copy_symlink(src, dst)
    # Assert
    assert not dst.is_symlink()


def test_deref_copy_symlink_dangling_target_raises(tmp_path: Path):
    """Dangling symlink → hard abort with the dedicated error class."""
    # Arrange
    src = tmp_path / "src"
    src.symlink_to(tmp_path / "does-not-exist")
    dst = tmp_path / "dst"
    # Act
    ctx = pytest.raises(DanglingToHomeSymlinkError)
    # Assert
    with ctx:
        deref_copy_symlink(src, dst)


def test_deref_copy_symlink_is_idempotent_for_existing_dst(tmp_path: Path):
    """Existing dst (file/dir/symlink) is replaced cleanly."""
    # Arrange — pre-create a dst with stale content
    real = tmp_path / "real"
    real.mkdir()
    (real / "fresh.txt").write_text("fresh")
    src = tmp_path / "src"
    src.symlink_to(real)
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "stale.txt").write_text("stale")
    # Act
    deref_copy_symlink(src, dst)
    # Assert
    assert (dst / "fresh.txt").is_file() and not (dst / "stale.txt").exists()


# ---------------------------------------------------------------------------
# F-CS8 fix — `worktrees/` subtrees excluded from the resolved copy
# ---------------------------------------------------------------------------


def test_deref_copy_symlink_excludes_worktrees_subtree(tmp_path: Path):
    """Resolved target has `worktrees/agent-x/big.bin`; the copy at dst
    does NOT contain `worktrees/` — the bloat trap is closed."""
    # Arrange
    real = tmp_path / "real"
    (real / "worktrees" / "agent-x").mkdir(parents=True)
    (real / "worktrees" / "agent-x" / "big.bin").write_text("BIG")
    (real / "skills").mkdir()
    (real / "skills" / "kept.md").write_text("ok")
    src = tmp_path / "src"
    src.symlink_to(real)
    dst = tmp_path / "dst"
    # Act
    deref_copy_symlink(src, dst)
    # Assert
    assert (dst / "skills" / "kept.md").is_file() and not (dst / "worktrees").exists()


def test_deref_copy_symlink_excludes_nested_worktrees_subtree(tmp_path: Path):
    """The exclusion is BASENAME-keyed and position-agnostic — a
    nested `worktrees/` deeper in the resolved target is also pruned."""
    # Arrange
    real = tmp_path / "real"
    (real / "skills" / "deep" / "worktrees" / "x").mkdir(parents=True)
    (real / "skills" / "deep" / "worktrees" / "x" / "big.bin").write_text("BIG")
    (real / "skills" / "deep" / "kept.txt").write_text("ok")
    src = tmp_path / "src"
    src.symlink_to(real)
    dst = tmp_path / "dst"
    # Act
    deref_copy_symlink(src, dst)
    # Assert
    assert (dst / "skills" / "deep" / "kept.txt").is_file() and not (
        dst / "skills" / "deep" / "worktrees"
    ).exists()
