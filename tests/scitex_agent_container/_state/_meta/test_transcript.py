"""Tests for ``_state._meta.transcript`` — Claude project encoding + jsonl.

PS-202 src-tests mirror. ``_encode_claude_project`` is pure; the
``_latest_jsonls`` reader walks ``~/.claude/projects/<encoded>/`` so
we redirect ``Path.home`` via ``monkeypatch`` only on ``pathlib`` ...
wait — task says no monkeypatch. Use ``HOME`` env override via
``env_save_restore`` so ``Path.home()`` resolves into ``tmp_path``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scitex_agent_container._state._meta.transcript import (
    _encode_claude_project,
    _latest_jsonls,
)

# --- _encode_claude_project (pure) ---------------------------------------


@pytest.mark.parametrize(
    "workdir,expected",
    [
        ("/home/u/proj", "-home-u-proj"),
        ("/a/b", "-a-b"),
    ],
)
def test_encode_simple_paths(workdir, expected):
    # Arrange
    path = workdir
    # Act
    encoded = _encode_claude_project(path)
    # Assert
    assert encoded == expected


def test_encode_collapses_triple_dashes_from_hidden_dir():
    # Arrange
    workdir = "/home/u/.config/foo"
    # Act
    encoded = _encode_claude_project(workdir)
    # Assert
    assert "---" not in encoded


def test_encode_preserves_double_dashes_for_hidden_dir():
    # Arrange
    workdir = "/.hidden"
    # Act
    encoded = _encode_claude_project(workdir)
    # Assert
    assert encoded.endswith("--hidden")


# --- _latest_jsonls (real filesystem, HOME redirected) -------------------


def test_latest_jsonls_returns_empty_when_proj_dir_absent(
    tmp_path: Path, env_save_restore
):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    workdir = tmp_path / "myproj"
    workdir.mkdir()
    # Act
    results = _latest_jsonls(str(workdir))
    # Assert
    assert results == []


def test_latest_jsonls_returns_sorted_by_mtime_desc(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    workdir = tmp_path / "myproj"
    workdir.mkdir()
    encoded = _encode_claude_project(str(workdir.resolve()))
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    older = proj_dir / "old.jsonl"
    newer = proj_dir / "new.jsonl"
    older.write_text("{}\n")
    time.sleep(0.01)
    newer.write_text("{}\n")
    # Act
    results = _latest_jsonls(str(workdir))
    # Assert
    assert results[0].name == "new.jsonl"


def test_latest_jsonls_skips_non_jsonl_files(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    workdir = tmp_path / "myproj"
    workdir.mkdir()
    encoded = _encode_claude_project(str(workdir.resolve()))
    proj_dir = tmp_path / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "a.jsonl").write_text("{}\n")
    (proj_dir / "b.txt").write_text("ignore\n")
    # Act
    results = _latest_jsonls(str(workdir))
    # Assert
    assert [p.name for p in results] == ["a.jsonl"]
