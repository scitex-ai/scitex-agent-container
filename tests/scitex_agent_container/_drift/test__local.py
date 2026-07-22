"""Tests for the launch-time LOCAL spec-source drift check.

PA-306: no mocks. Every test runs against a REAL git repo built with
``git init`` in ``tmp_path`` — a bare remote plus working clones — so
the drift comparison exercises real ``git fetch`` / ``git rev-list``.
The fetch cache is redirected to ``tmp_path`` via SCITEX_DIR so tests
never touch the user's real ~/.scitex.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003).
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._drift import (
    DriftState,
    SpecSourceDriftError,
    check_spec_source_drift,
    drift_warning_lines,
    spec_source_repo,
    warn_if_spec_source_drifted,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def isolated_scitex_dir(tmp_path: Path, env_save_restore):
    """Redirect the fetch-cache root to tmp_path via SCITEX_DIR.

    Keeps the per-repo last-fetch cache out of the user's real
    ~/.scitex during the test run.
    """
    cache_root = tmp_path / "scitex-home"
    cache_root.mkdir()
    env_save_restore.set("SCITEX_DIR", str(cache_root))
    return cache_root


@pytest.fixture
def spec_repo(tmp_path: Path):
    """A real git working clone tracking a bare remote on ``develop``.

    Returns ``(spec_path, work, remote)``. ``spec_path`` is a spec.yaml
    committed + pushed so the working clone starts CURRENT.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(remote), str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "checkout", "-b", "develop")
    agents = work / ".scitex" / "agent-container" / "agents" / "foo"
    agents.mkdir(parents=True)
    spec = agents / "spec.yaml"
    spec.write_text(explicitize_yaml("apiVersion: scitex-agent-container/v3\n"))
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "develop")
    return spec, work, remote


def _advance_remote(remote: Path, base_work: Path, tmp_path: Path) -> None:
    """Push a new commit to ``remote`` from a second clone (makes base BEHIND)."""
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(remote), str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "develop")
    (other / "new.txt").write_text("remote work")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote work")
    _git(other, "push")


# ---------------------------------------------------------------------------
# spec_source_repo
# ---------------------------------------------------------------------------


def test_spec_source_repo_finds_git_toplevel(spec_repo):
    # Arrange
    spec, work, _remote = spec_repo
    # Act
    repo = spec_source_repo(spec)
    # Assert
    assert repo == work.resolve()


def test_spec_source_repo_returns_none_outside_git(tmp_path: Path):
    # Arrange
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "spec.yaml").write_text("x")
    # Act
    repo = spec_source_repo(plain / "spec.yaml")
    # Assert
    assert repo is None


# ---------------------------------------------------------------------------
# check_spec_source_drift — state classification
# ---------------------------------------------------------------------------


def test_clean_clone_reports_current(spec_repo, isolated_scitex_dir):
    # Arrange
    spec, _work, _remote = spec_repo
    # Act
    status = check_spec_source_drift(spec, ttl=0)
    # Assert
    assert status.state is DriftState.CURRENT


def test_remote_ahead_reports_behind(spec_repo, isolated_scitex_dir, tmp_path):
    # Arrange
    spec, work, remote = spec_repo
    _advance_remote(remote, work, tmp_path)
    # Act
    status = check_spec_source_drift(spec, ttl=0)
    # Assert
    assert status.state is DriftState.BEHIND


def test_unpushed_local_commit_reports_ahead(spec_repo, isolated_scitex_dir):
    # Arrange
    spec, work, _remote = spec_repo
    (work / "local.txt").write_text("local work")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local work")
    # Act
    status = check_spec_source_drift(spec, ttl=0)
    # Assert
    assert status.state is DriftState.AHEAD


def test_both_ahead_and_behind_reports_diverged(
    spec_repo, isolated_scitex_dir, tmp_path
):
    # Arrange
    spec, work, remote = spec_repo
    _advance_remote(remote, work, tmp_path)
    (work / "local.txt").write_text("local work")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local work")
    # Act
    status = check_spec_source_drift(spec, ttl=0)
    # Assert
    assert status.state is DriftState.DIVERGED


def test_behind_count_matches_remote_commits(spec_repo, isolated_scitex_dir, tmp_path):
    # Arrange
    spec, work, remote = spec_repo
    _advance_remote(remote, work, tmp_path)
    # Act
    status = check_spec_source_drift(spec, ttl=0)
    # Assert
    assert status.behind == 1


def test_non_git_source_reports_not_a_repo(tmp_path, isolated_scitex_dir):
    # Arrange
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "spec.yaml").write_text("x")
    # Act
    status = check_spec_source_drift(plain / "spec.yaml")
    # Assert
    assert status.state is DriftState.NOT_A_REPO


def test_branch_without_upstream_reports_unreachable(tmp_path, isolated_scitex_dir):
    # Arrange — a repo with a commit but no remote/upstream configured.
    repo = tmp_path / "lonely"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    spec = repo / "spec.yaml"
    spec.write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "only commit")
    # Act
    status = check_spec_source_drift(spec)
    # Assert
    assert status.state is DriftState.UNREACHABLE


# ---------------------------------------------------------------------------
# fetch caching (TTL)
# ---------------------------------------------------------------------------


def test_recent_fetch_is_cached_within_ttl(spec_repo, isolated_scitex_dir, tmp_path):
    # Arrange — first check fetches at t=1000 (writes cache); remote then
    # advances; second check at t=1030 with ttl=60 must reuse the cache
    # and therefore still see CURRENT (no re-fetch).
    spec, work, remote = spec_repo
    check_spec_source_drift(spec, ttl=60, now_fn=lambda: 1000.0)
    _advance_remote(remote, work, tmp_path)
    # Act
    status = check_spec_source_drift(spec, ttl=60, now_fn=lambda: 1030.0)
    # Assert
    assert status.state is DriftState.CURRENT


def test_expired_fetch_cache_refetches_and_sees_drift(
    spec_repo, isolated_scitex_dir, tmp_path
):
    # Arrange — first fetch at t=1000; remote advances; second check at
    # t=2000 (ttl=60 expired) must re-fetch and see BEHIND.
    spec, work, remote = spec_repo
    check_spec_source_drift(spec, ttl=60, now_fn=lambda: 1000.0)
    _advance_remote(remote, work, tmp_path)
    # Act
    status = check_spec_source_drift(spec, ttl=60, now_fn=lambda: 2000.0)
    # Assert
    assert status.state is DriftState.BEHIND


# ---------------------------------------------------------------------------
# drift_warning_lines
# ---------------------------------------------------------------------------


def test_current_status_produces_no_warning(spec_repo, isolated_scitex_dir):
    # Arrange
    spec, _work, _remote = spec_repo
    status = check_spec_source_drift(spec, ttl=0)
    # Act
    lines = drift_warning_lines(status)
    # Assert
    assert lines == []


def test_behind_warning_names_the_pull_fix(spec_repo, isolated_scitex_dir, tmp_path):
    # Arrange
    spec, work, remote = spec_repo
    _advance_remote(remote, work, tmp_path)
    status = check_spec_source_drift(spec, ttl=0)
    # Act
    text = "\n".join(drift_warning_lines(status))
    # Assert
    assert "pull --ff-only" in text


def test_ahead_warning_names_the_push_fix(spec_repo, isolated_scitex_dir):
    # Arrange
    spec, work, _remote = spec_repo
    (work / "local.txt").write_text("local")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local")
    status = check_spec_source_drift(spec, ttl=0)
    # Act
    text = "\n".join(drift_warning_lines(status))
    # Assert
    assert "push" in text


# ---------------------------------------------------------------------------
# warn_if_spec_source_drifted — default warn vs strict block
# ---------------------------------------------------------------------------


def test_default_warn_does_not_raise_on_drift(
    spec_repo, isolated_scitex_dir, tmp_path, capsys
):
    # Arrange
    spec, work, remote = spec_repo
    _advance_remote(remote, work, tmp_path)
    # Act
    status = warn_if_spec_source_drifted(spec, agent="foo", strict=False)
    # Assert — returns the drifted status, never raises (default = warn)
    assert status.state is DriftState.BEHIND


def test_default_warn_emits_loud_stderr_banner(
    spec_repo, isolated_scitex_dir, tmp_path, capsys
):
    # Arrange
    spec, work, remote = spec_repo
    _advance_remote(remote, work, tmp_path)
    # Act
    warn_if_spec_source_drifted(spec, agent="foo", strict=False)
    # Assert
    assert "sac-drift WARNING" in capsys.readouterr().err


def test_strict_mode_raises_on_drift(spec_repo, isolated_scitex_dir, tmp_path):
    # Arrange
    spec, work, remote = spec_repo
    _advance_remote(remote, work, tmp_path)
    # Act
    ctx = pytest.raises(SpecSourceDriftError)
    # Assert
    with ctx:
        warn_if_spec_source_drifted(spec, agent="foo", strict=True)


def test_strict_mode_does_not_raise_on_not_a_repo(tmp_path, isolated_scitex_dir):
    # Arrange — strict only blocks genuine drift, not unknown drift.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "spec.yaml").write_text("x")
    # Act
    status = warn_if_spec_source_drifted(
        plain / "spec.yaml", strict=True, do_fetch=False
    )
    # Assert
    assert status.state is DriftState.NOT_A_REPO


def test_strict_mode_does_not_raise_when_current(spec_repo, isolated_scitex_dir):
    # Arrange
    spec, _work, _remote = spec_repo
    # Act
    status = warn_if_spec_source_drifted(spec, strict=True)
    # Assert
    assert status.state is DriftState.CURRENT
