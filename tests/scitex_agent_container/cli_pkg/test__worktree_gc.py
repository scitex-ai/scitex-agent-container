"""CLI tests for ``sac worktree gc``.

PA-306: no ``unittest.mock``. A real ``CliRunner`` driving the real verb
against REAL temp git repos with REAL worktrees.

Two deliberate choices keep this hermetic without faking anything:

* Every worktree here sits on a branch that IS an ancestor of ``develop``,
  so the merged leg is satisfied locally and ``gh`` is never invoked — no
  network, no flake, and the CLI is exercised with its REAL default seams
  rather than injected ones.
* ``SAC_EVENT_LOG`` is redirected to a temp file for the WHOLE module
  (resolved at call time, so the redirect is honoured). The alarm defaults
  ON under ``--apply``, so without this a test would write to the
  operator's real event log. Belt and braces: tests whose subject is not
  the alarm pass ``--no-alarm`` anyway.

The assertions that matter are the EXIT CODES and the DISK: ``--dry-run``
is the default and must leave every directory exactly where it was, and a
repo over its cap must exit non-zero — a cap alarm that exits 0 is a
report nobody reads, which is how a repo reached 105 worktrees.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._events import read_events
from scitex_agent_container.cli_pkg._worktree_gc import worktree_gc


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def events_log(tmp_path: Path, env_save_restore) -> Path:
    """Redirect sac's event log to a temp file for this whole module.

    The alarm rides --apply by default; a test must never write to the
    operator's real log. Env redirect (not a mock) — the same mechanism
    the fleet uses, and it resolves at call time.
    """
    log = tmp_path / "sac-events.jsonl"
    env_save_restore.set("SAC_EVENT_LOG", str(log))
    return log


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo on develop with one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.email", "gc-cli-test@example.invalid")
    _git(root, "config", "user.name", "GC CLI Test")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture
def worktree(repo: Path, tmp_path: Path):
    """Factory: a real worktree on an ancestor-merged branch (no gh needed)."""

    def _add(name: str) -> Path:
        path = tmp_path / "worktrees" / name
        _git(repo, "worktree", "add", "-q", "-b", f"feat/{name}", str(path), "develop")
        return path

    return _add


# ---------------------------------------------------------------------------
# Dry-run is the default and removes NOTHING
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_reapable_worktree(repo, worktree, events_log):
    # Arrange — a clean, merged worktree; --min-age-hours 0 clears the age gate.
    path = worktree("reapable")
    # Act
    result = CliRunner().invoke(
        worktree_gc, ["--repo", str(repo), "--min-age-hours", "0", "--no-alarm"]
    )
    # Assert
    assert "would remove" in result.output


def test_dry_run_leaves_the_worktree_on_disk(repo, worktree, events_log):
    # Arrange — the report is a claim; the directory is the fact.
    path = worktree("reapable")
    # Act
    CliRunner().invoke(
        worktree_gc, ["--repo", str(repo), "--min-age-hours", "0", "--no-alarm"]
    )
    # Assert
    assert path.is_dir()


def test_dry_run_says_it_removed_nothing(repo, worktree, events_log):
    # Arrange — never silent: a dry run must SAY it was a dry run.
    worktree("reapable")
    # Act
    result = CliRunner().invoke(
        worktree_gc, ["--repo", str(repo), "--min-age-hours", "0", "--no-alarm"]
    )
    # Assert
    assert "nothing was removed" in result.output


def test_dry_run_exits_zero_when_under_cap(repo, worktree, events_log):
    # Arrange
    worktree("reapable")
    # Act
    result = CliRunner().invoke(
        worktree_gc, ["--repo", str(repo), "--min-age-hours", "0", "--no-alarm"]
    )
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --apply acts
# ---------------------------------------------------------------------------


def test_apply_removes_the_reapable_worktree(repo, worktree, events_log):
    # Arrange — the one case where deleting is right.
    path = worktree("reapable")
    # Act
    CliRunner().invoke(
        worktree_gc,
        ["--repo", str(repo), "--apply", "--min-age-hours", "0", "--no-alarm"],
    )
    # Assert
    assert not path.exists()


def test_apply_keeps_the_dirty_worktree(repo, worktree, events_log):
    # Arrange — merged + old, but dirty. --apply must not touch it.
    path = worktree("dirty")
    (path / "README.md").write_text("uncommitted\n")
    # Act
    CliRunner().invoke(
        worktree_gc,
        ["--repo", str(repo), "--apply", "--min-age-hours", "0", "--no-alarm"],
    )
    # Assert
    assert (path / "README.md").read_text() == "uncommitted\n"


def test_apply_names_the_keep_reason(repo, worktree, events_log):
    # Arrange — "kept" alone is useless; WHY is the product.
    path = worktree("dirty")
    (path / "README.md").write_text("uncommitted\n")
    # Act
    result = CliRunner().invoke(
        worktree_gc,
        ["--repo", str(repo), "--apply", "--min-age-hours", "0", "--no-alarm"],
    )
    # Assert
    assert "dirty" in result.output


# ---------------------------------------------------------------------------
# Exit codes — the cron-facing contract
# ---------------------------------------------------------------------------


def test_over_cap_exits_non_zero(repo, worktree, events_log):
    # Arrange — one dirty survivor against a cap of 0. An alarm that exits
    # 0 on sprawl is a report nobody reads.
    path = worktree("dirty")
    (path / "README.md").write_text("uncommitted\n")
    # Act
    result = CliRunner().invoke(
        worktree_gc,
        ["--repo", str(repo), "--cap", "0", "--min-age-hours", "0", "--no-alarm"],
    )
    # Assert
    assert result.exit_code == 1


def test_unreadable_repo_exits_two(tmp_path, events_log):
    # Arrange — unknown OUTRANKS over-cap: it is a known-bad you cannot see.
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    # Act
    result = CliRunner().invoke(worktree_gc, ["--repo", str(plain), "--no-alarm"])
    # Assert
    assert result.exit_code == 2


def test_unreadable_repo_is_labelled_unknown(tmp_path, events_log):
    # Arrange — "could not read" must never render as "clean".
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    # Act
    result = CliRunner().invoke(worktree_gc, ["--repo", str(plain), "--no-alarm"])
    # Assert
    assert "UNKNOWN" in result.output


# ---------------------------------------------------------------------------
# Usage guards
# ---------------------------------------------------------------------------


def test_repo_and_all_together_is_a_usage_error(repo, events_log):
    # Arrange
    # Act
    result = CliRunner().invoke(worktree_gc, ["--repo", str(repo), "--all"])
    # Assert
    assert result.exit_code == 2


def test_neither_repo_nor_all_is_a_usage_error(events_log):
    # Arrange
    # Act
    result = CliRunner().invoke(worktree_gc, [])
    # Assert
    assert result.exit_code == 2


def test_apply_with_dry_run_is_a_usage_error(repo, events_log):
    # Arrange — opposites. Silently preferring one would be a coin flip on
    # whether the run destroys anything.
    # Act
    result = CliRunner().invoke(
        worktree_gc, ["--repo", str(repo), "--apply", "--dry-run"]
    )
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


def test_json_output_carries_the_exit_code(repo, worktree, events_log):
    # Arrange — cron reads the JSON; it must agree with the exit code.
    path = worktree("dirty")
    (path / "README.md").write_text("uncommitted\n")
    # Act
    result = CliRunner().invoke(
        worktree_gc,
        [
            "--repo",
            str(repo),
            "--cap",
            "0",
            "--min-age-hours",
            "0",
            "--no-alarm",
            "--json",
        ],
    )
    # Assert
    assert json.loads(result.output)["exit_code"] == 1


def test_json_output_reports_the_keep_reasons(repo, worktree, events_log):
    # Arrange
    path = worktree("dirty")
    (path / "README.md").write_text("uncommitted\n")
    # Act
    result = CliRunner().invoke(
        worktree_gc,
        ["--repo", str(repo), "--min-age-hours", "0", "--no-alarm", "--json"],
    )
    # Assert
    assert json.loads(result.output)["repos"][0]["keep_reasons"] == {"dirty": 1}


def test_json_dry_run_reports_zero_removed(repo, worktree, events_log):
    # Arrange
    worktree("reapable")
    # Act
    result = CliRunner().invoke(
        worktree_gc,
        ["--repo", str(repo), "--min-age-hours", "0", "--no-alarm", "--json"],
    )
    # Assert
    assert json.loads(result.output)["removed"] == 0


# ---------------------------------------------------------------------------
# The alarm rail's CLI wiring
# ---------------------------------------------------------------------------


def test_dry_run_writes_no_card_by_default(repo, worktree, events_log):
    # Arrange — a dry run is a REPORT; it must not mutate the board. Note
    # no --no-alarm here: this pins the DEFAULT.
    path = worktree("dirty")
    (path / "README.md").write_text("uncommitted\n")
    # Act
    CliRunner().invoke(
        worktree_gc, ["--repo", str(repo), "--cap", "0", "--min-age-hours", "0"]
    )
    # Assert
    assert not events_log.exists()


def test_apply_over_cap_records_the_repo(repo, worktree, events_log):
    # Arrange — the scheduled path: --apply alarms by default, so the
    # sprawl the GC could NOT fix leaves a durable record.
    path = worktree("dirty")
    (path / "README.md").write_text("uncommitted\n")
    # Act
    CliRunner().invoke(
        worktree_gc,
        ["--repo", str(repo), "--apply", "--cap", "0", "--min-age-hours", "0"],
    )
    # Assert
    recorded = [e.event for e in read_events(events_log)]
    assert recorded == ["subject-degraded"]
