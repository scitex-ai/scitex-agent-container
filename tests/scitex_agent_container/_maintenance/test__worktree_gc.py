"""Tests for ``_maintenance._worktree_gc`` — the GC engine + its predicate.

PA-306: no ``unittest.mock``. REAL temp git repos, REAL ``git worktree
add``, and for the in-use leg a REAL child process with its cwd inside a
worktree. Only the two documented seams are injected (the merged-PR
lookup and the ``/proc`` scan); everything else is git doing what git
does.

The behaviours that matter — one per leg of the safety predicate, because
every one of them is a way to destroy work:

* clean + merged-by-ancestor + old + idle -> REMOVED under ``apply``,
* the same worktree under a dry run -> LISTED and STILL THERE,
* clean + SQUASH-merged (a merged PR, not an ancestor) -> REMOVED,
* DIRTY -> KEPT, untracked-only -> KEPT (untracked IS dirty),
* clean but UNMERGED -> KEPT, too YOUNG -> KEPT, IN USE -> KEPT,
* an UNKNOWN on any leg -> KEPT (never silently "fine"),
* ``--force`` is never passed — git's refusal is the backstop.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from scitex_agent_container._maintenance._worktree_gc import gc_repo, gc_repos
from scitex_agent_container._maintenance._worktree_gc_model import (
    KEEP_DIRTY,
    KEEP_IN_USE,
    KEEP_IN_USE_UNKNOWN,
    KEEP_MERGE_UNKNOWN,
    KEEP_TOO_YOUNG,
    KEEP_UNMERGED,
)


def _verdict(result, path: Path):
    (match,) = [v for v in result.verdicts if v.path == str(path)]
    return match


# ---------------------------------------------------------------------------
# Leg-by-leg: the four ways a worktree earns its life
# ---------------------------------------------------------------------------


def test_clean_merged_old_worktree_is_removed(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — a worktree whose branch is an ancestor of develop, clean,
    # and (via the clock seam) older than the age gate.
    path = add_worktree("reapable")
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert — the one case where deleting is right.
    assert [v.path for v in result.removed] == [str(path)]


def test_removed_worktree_directory_is_really_gone(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — the verdict is a claim; the filesystem is the fact.
    path = add_worktree("reapable")
    # Act
    gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert not path.exists()


def test_squash_merged_worktree_is_removed(
    repo, add_worktree, old_now, pr_yes, no_cwds
):
    # Arrange — a branch AHEAD of develop (so the ancestor check says
    # "unmerged") whose PR was squash-merged. Without the PR leg, a
    # squash-merging repo would never have a single worktree collected.
    path = add_worktree("squashed", ahead=True)
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_yes, cwd_scan=no_cwds)
    # Assert
    assert [v.path for v in result.removed] == [str(path)]


def test_dirty_worktree_is_kept(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — merged + old + idle, but a tracked file is modified.
    path = add_worktree("dirty")
    (path / "README.md").write_text("uncommitted edit\n")
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert KEEP_DIRTY in _verdict(result, path).keep_reasons


def test_untracked_only_worktree_is_kept(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — the file is untracked, which is the WORST case, not the
    # mildest: it exists nowhere but this directory. `status --porcelain`
    # lists it, and we never pass -uno.
    path = add_worktree("untracked")
    (path / "scratch.txt").write_text("work saved nowhere else\n")
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert KEEP_DIRTY in _verdict(result, path).keep_reasons


def test_untracked_only_worktree_survives_on_disk(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — the verdict is a claim; the surviving directory is the fact.
    path = add_worktree("untracked")
    (path / "scratch.txt").write_text("work saved nowhere else\n")
    # Act
    gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert (path / "scratch.txt").exists()


def test_clean_unmerged_worktree_is_kept(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — ahead of develop AND GitHub says no merged PR. Two
    # independent sources agree, which is what a definite UNMERGED needs.
    path = add_worktree("unmerged", ahead=True)
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert KEEP_UNMERGED in _verdict(result, path).keep_reasons


def test_too_young_worktree_is_kept(repo, add_worktree, pr_no, no_cwds):
    # Arrange — merged + clean + idle, but committed seconds ago. NOTE the
    # real clock here (no old_now): a young worktree is presumed in flight.
    path = add_worktree("young")
    # Act
    result = gc_repo(
        repo, apply=True, now=time.time(), pr_merged=pr_no, cwd_scan=no_cwds
    )
    # Assert
    assert KEEP_TOO_YOUNG in _verdict(result, path).keep_reasons


def test_in_use_worktree_is_kept(repo, add_worktree, old_now, pr_no):
    # Arrange — clean + merged + old, but a REAL process is sitting in it.
    # No cwd seam: this drives the real /proc scanner against a real child.
    path = add_worktree("in-use")
    proc = subprocess.Popen(["sleep", "30"], cwd=str(path))
    try:
        # Act
        result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no)
        # Assert
        assert KEEP_IN_USE in _verdict(result, path).keep_reasons
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_in_use_worktree_survives_on_disk(repo, add_worktree, old_now, pr_no):
    # Arrange — the leg that protects an agent working RIGHT NOW.
    path = add_worktree("in-use")
    proc = subprocess.Popen(["sleep", "30"], cwd=str(path))
    try:
        # Act
        gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no)
        # Assert
        assert path.is_dir()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Unknown is not clean — a leg that could not run KEEPS
# ---------------------------------------------------------------------------


def test_unavailable_cwd_signal_keeps_the_worktree(
    repo, add_worktree, old_now, pr_no, unknown_cwds
):
    # Arrange — clean + merged + old, but the /proc signal is UNAVAILABLE.
    # "I could not look" must never read as "nothing is running".
    path = add_worktree("blind")
    # Act
    result = gc_repo(
        repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=unknown_cwds
    )
    # Assert
    assert KEEP_IN_USE_UNKNOWN in _verdict(result, path).keep_reasons


def test_unknown_merge_state_keeps_the_worktree(
    repo, add_worktree, old_now, pr_unknown, no_cwds
):
    # Arrange — ahead of develop, and gh could not answer (missing /
    # offline / rate-limited). A squash-merge and an unmerged branch are
    # indistinguishable here, so the only safe answer is KEEP.
    path = add_worktree("unknowable", ahead=True)
    # Act
    result = gc_repo(
        repo, apply=True, now=old_now, pr_merged=pr_unknown, cwd_scan=no_cwds
    )
    # Assert
    assert KEEP_MERGE_UNKNOWN in _verdict(result, path).keep_reasons


def test_raising_pr_seam_keeps_the_worktree(repo, add_worktree, old_now, no_cwds):
    # Arrange — a PR lookup that BLOWS UP must degrade to UNKNOWN -> KEEP,
    # never crash a scheduled pass and never read as merged.
    def _boom(repo_path, branch):
        raise RuntimeError("gh exploded")

    path = add_worktree("exploding", ahead=True)
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=_boom, cwd_scan=no_cwds)
    # Assert
    assert KEEP_MERGE_UNKNOWN in _verdict(result, path).keep_reasons


def test_unreadable_repo_reports_unknown_not_empty(tmp_path, pr_no, no_cwds):
    # Arrange — a directory that is not a git repo at all.
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    # Act
    result = gc_repo(not_a_repo, apply=True, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert — "I could not read it" is not "it has no worktrees".
    assert result.unreadable


# ---------------------------------------------------------------------------
# Dry-run is the default, and it removes NOTHING
# ---------------------------------------------------------------------------


def test_dry_run_lists_the_reapable_worktree(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — a worktree that passes every leg.
    path = add_worktree("reapable")
    # Act
    result = gc_repo(repo, apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert — it is reported as removable...
    assert [v.path for v in result.verdicts if v.removable] == [str(path)]


def test_dry_run_removes_nothing_from_disk(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — ...but the directory is still there afterwards.
    path = add_worktree("reapable")
    # Act
    gc_repo(repo, apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert path.is_dir()


def test_dry_run_records_no_removals(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — and it does not CLAIM to have removed anything either.
    add_worktree("reapable")
    # Act
    result = gc_repo(repo, apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert result.removed == ()


def test_dry_run_is_the_default_mode(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — `apply` not passed at all. A GC whose default is
    # destructive gets run destructively by accident exactly once.
    path = add_worktree("reapable")
    # Act
    gc_repo(repo, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert path.is_dir()


def test_dry_run_keeps_the_worktree_registered(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — git's own view must be untouched too, not just the disk.
    path = add_worktree("reapable")
    # Act
    gc_repo(repo, apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(path) in listed


# ---------------------------------------------------------------------------
# What the GC deliberately never touches
# ---------------------------------------------------------------------------


def test_main_worktree_is_never_a_candidate(repo, old_now, pr_no, no_cwds):
    # Arrange — the repo checkout itself is not sprawl, it is the repo.
    # (Its branch IS develop, so every leg would otherwise pass.)
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert result.verdicts == ()


def test_locked_worktree_is_kept(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — a lock is a human saying "leave this alone"; the GC obeys.
    path = add_worktree("locked")
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "lock", str(path)],
        capture_output=True,
        check=True,
    )
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert path.is_dir()


def test_remove_never_passes_force(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — THE pin. `--force` would disable git's own dirty-refusal,
    # the one check in this system we did not write ourselves. A worktree
    # that is dirty must survive `apply` even though we asked to remove
    # everything reapable — proving the remove path cannot be forced.
    path = add_worktree("dirty")
    (path / "README.md").write_text("uncommitted edit\n")
    # Act
    gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert (path / "README.md").read_text() == "uncommitted edit\n"


# ---------------------------------------------------------------------------
# Prune: the always-safe half
# ---------------------------------------------------------------------------


def test_prune_clears_the_ref_of_a_deleted_directory(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — a worktree whose directory someone `rm -rf`'d by hand,
    # leaving a dangling admin ref. Nothing is destroyed by pruning it.
    path = add_worktree("vanished")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    # Act
    gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(path) not in listed


def test_missing_worktree_is_not_removed_by_predicate(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — a vanished directory is prune's job, not remove's:
    # `worktree remove` would just error on it.
    path = add_worktree("vanished")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert result.removed == ()


def test_prune_names_what_it_pruned(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — WITHOUT --verbose, `git worktree prune` prints nothing at
    # all, so the pass would claim a prune with no evidence either way —
    # the exact "green line nobody can check" shape this GC exists to kill.
    path = add_worktree("vanished")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    # Act
    result = gc_repo(repo, apply=True, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert "vanished" in result.prune_detail


def test_dry_run_prune_reports_what_it_would_prune(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — a dry run must SAY what it would prune, not go quiet.
    path = add_worktree("vanished")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    # Act
    result = gc_repo(repo, apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    assert "vanished" in result.prune_detail


def test_dry_run_prune_leaves_the_dangling_ref(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — even prune, which destroys no files, only REPORTS on a dry
    # run. --dry-run means the whole pass is a read, not most of it.
    path = add_worktree("vanished")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    # Act
    gc_repo(repo, apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds)
    # Assert
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(path) in listed


# ---------------------------------------------------------------------------
# Cap + multi-repo
# ---------------------------------------------------------------------------


def test_repo_over_cap_is_flagged(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — two DIRTY worktrees (so the GC cannot reap them) and a cap
    # of 1. The cap is about what SURVIVES, which is the whole point: the
    # predicate is never relaxed to hit a number.
    for name in ("a", "b"):
        path = add_worktree(name)
        (path / "README.md").write_text("edit\n")
    # Act
    result = gc_repo(
        repo, apply=True, cap=1, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds
    )
    # Assert
    assert result.exceeds_cap


def test_repo_under_cap_is_not_flagged(repo, add_worktree, old_now, pr_no, no_cwds):
    # Arrange — one dirty worktree, cap 20.
    path = add_worktree("a")
    (path / "README.md").write_text("edit\n")
    # Act
    result = gc_repo(
        repo, apply=True, cap=20, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds
    )
    # Assert
    assert not result.exceeds_cap


def test_cap_counts_survivors_not_candidates(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — two worktrees, one reapable and one dirty, cap 1. After
    # the pass ONE survives, so the repo is at cap, not over it.
    add_worktree("reapable")
    dirty = add_worktree("dirty")
    (dirty / "README.md").write_text("edit\n")
    # Act
    result = gc_repo(
        repo, apply=True, cap=1, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds
    )
    # Assert
    assert not result.exceeds_cap


def test_keep_reason_breakdown_counts_each_reason(
    repo, add_worktree, old_now, pr_no, no_cwds
):
    # Arrange — the breakdown IS the cap card's value: "2 dirty" is an
    # instruction where "2 kept" is a number.
    for name in ("a", "b"):
        path = add_worktree(name)
        (path / "README.md").write_text("edit\n")
    # Act
    result = gc_repo(
        repo, apply=True, cap=1, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds
    )
    # Assert
    assert result.keep_reason_breakdown[KEEP_DIRTY] == 2


def test_gc_repos_sweeps_every_repo(
    repo, add_worktree, old_now, pr_no, no_cwds, tmp_path
):
    # Arrange — a second real repo alongside the first.
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-q", "-b", "develop"], check=True)
    add_worktree("reapable")
    # Act
    outcome = gc_repos(
        [repo, other], apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds
    )
    # Assert
    assert len(outcome.results) == 2


def test_one_unreadable_repo_does_not_stop_the_rest(
    repo, add_worktree, old_now, pr_no, no_cwds, tmp_path
):
    # Arrange — a bad path first; the good repo behind it must still be
    # swept. One broken repo never suppresses the whole fleet's pass.
    missing = tmp_path / "not-a-repo"
    missing.mkdir()
    path = add_worktree("reapable")
    # Act
    outcome = gc_repos(
        [missing, repo], apply=False, now=old_now, pr_merged=pr_no, cwd_scan=no_cwds
    )
    # Assert
    assert [v.path for v in outcome.results[1].verdicts if v.removable] == [str(path)]
