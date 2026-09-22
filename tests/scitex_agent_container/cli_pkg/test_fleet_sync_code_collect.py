"""Tests for the sync-code collector (git plumbing, no ssh).

Collector tests run against real tmp git repos (no mocks, PA-306) —
``collect_checkout_state`` takes ``proj_dir`` / ``dotfiles_dir``
overrides precisely so tests never touch the real ~/proj.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scitex_agent_container.cli_pkg._fleet_sync_code_collect import (
    collect_checkout_state,
)


# ---------------------------------------------------------------------------
# Helpers (not tests).
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _make_repo(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t.t")
    _git(d, "config", "user.name", "t")
    (d / "f.txt").write_text("x\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "init")
    _git(d, "branch", "-M", "develop")
    return d


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_collect_finds_scitex_checkouts_only(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_repo(proj / "scitex-foo")
    (proj / "not-scitex").mkdir()
    (proj / "random.txt").write_text("x")
    got = collect_checkout_state(proj_dir=proj, dotfiles_dir=tmp_path / "nodot")
    assert "scitex-foo" in got["checkouts"]


def test_collect_ignores_non_scitex_dirs(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "other").mkdir()
    got = collect_checkout_state(proj_dir=proj, dotfiles_dir=tmp_path / "nodot")
    assert got["checkouts"] == {}


def test_collect_marks_dirty_tree(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    repo = _make_repo(proj / "scitex-foo")
    (repo / "f.txt").write_text("modified\n")
    got = collect_checkout_state(proj_dir=proj, dotfiles_dir=tmp_path / "nodot")
    assert got["checkouts"]["scitex-foo"]["dirty"] is True


def test_collect_clean_tree_not_dirty(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_repo(proj / "scitex-foo")
    got = collect_checkout_state(proj_dir=proj, dotfiles_dir=tmp_path / "nodot")
    assert got["checkouts"]["scitex-foo"]["dirty"] is False


def test_collect_untracked_files_do_not_count_as_dirty(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    repo = _make_repo(proj / "scitex-foo")
    (repo / "new-untracked.txt").write_text("hello\n")
    got = collect_checkout_state(proj_dir=proj, dotfiles_dir=tmp_path / "nodot")
    assert got["checkouts"]["scitex-foo"]["dirty"] is False


def test_collect_missing_proj_dir_reports_error(tmp_path: Path) -> None:
    got = collect_checkout_state(
        proj_dir=tmp_path / "nope", dotfiles_dir=tmp_path / "nodot"
    )
    assert any("proj dir missing" in e for e in got["errors"])


def test_collect_dotfiles_sha_when_present(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    dot = _make_repo(tmp_path / "dotfiles")
    got = collect_checkout_state(proj_dir=proj, dotfiles_dir=dot)
    assert got["dotfiles_sha"] is not None and len(got["dotfiles_sha"]) == 40


def test_collect_state_is_json_serialisable(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_repo(proj / "scitex-foo")
    got = collect_checkout_state(proj_dir=proj, dotfiles_dir=tmp_path / "nodot")
    assert json.loads(json.dumps(got))["checkouts"]["scitex-foo"]["branch"] == "develop"


# EOF
