"""End-to-end tests for the ``session_start_reap_stale.py`` hook.

NO MOCKS. Every test drives the real hook script as a subprocess against
a real ephemeral git repository (see ``conftest.py``'s ``ephemeral_repo``
fixture). Stale worktrees are simulated with real ``os.utime`` calls on
real worktree dirs created via ``git worktree add`` — the predicate's
``develop..HEAD``-empty check sees real refs, the mtime gate sees real
mtimes.

The hook MUST be fail-open (exit 0 across the board) so a misfire never
wedges Claude Code's ``SessionStart`` event handler. The tests pin both
the success-side behaviour (stale + safe → reaped) and the safety-side
behaviour (recent → preserved, predicate-failing → preserved, no roots
→ no-op + exit 0, broken git → exit 0).

TQ: every test carries ``# Arrange`` / ``# Act`` / ``# Assert`` markers
on their own lines with at least one code statement between consecutive
markers, a >=3-word descriptive name, and exactly one assertion
(STX-TQ002 / STX-TQ007).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import _git

# The hook script lives next to its siblings under
# ``_baseline_assets/claude_worktree_hooks/``. Resolved relative to this
# test file so a refactor of the asset path is caught at import time.
SESSION_START_HOOK_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "claude_worktree_hooks"
    / "session_start_reap_stale.py"
)


def _session_start_payload(cwd: Path) -> dict:
    """Mirror the shape Claude Code's SessionStart hook emits on stdin."""
    return {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(cwd),
        "session_id": "test-session-id",
        "transcript_path": str(cwd / ".transcript"),
        "permission_mode": "default",
        "agent_id": "test-agent-id",
        "agent_type": "test",
    }


def _run_session_start(
    payload: dict,
    *,
    extra_args: list[str] | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the SessionStart hook script with the JSON payload on stdin.

    ``extra_args`` lets tests pass ``--age-hours`` / ``--now-epoch``
    overrides; ``env`` lets tests inject ``HOME`` / clear ``PATH`` to
    exercise the fail-open paths.
    """
    cmd = [sys.executable, str(SESSION_START_HOOK_SCRIPT)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _make_worktree(repo: Path, name: str) -> Path:
    """Add a real ``git worktree`` at ``<repo>/.worktrees/<name>``.

    The hook scans canonical agent-worktree roots; this helper builds a
    real one so the predicate's ``status --porcelain`` + ``rev-list
    develop..HEAD`` checks operate on real refs (no mocks).
    """
    target = repo / ".worktrees" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", f"claude/{name}", str(target), "develop")
    return target


def _age_worktree(worktree: Path, hours: float) -> None:
    """Backdate ``worktree`` mtime by ``hours`` so the age gate trips.

    Backdates the directory's own mtime — that's what the hook's
    ``_stale_children`` reads when classifying a candidate. Real
    ``os.utime`` on a real path; no mocks.
    """
    new_time = time.time() - (hours * 3600)
    os.utime(worktree, (new_time, new_time))


# ---------------------------------------------------------------------------
# Reap path: stale + safe -> removed
# ---------------------------------------------------------------------------


class TestSessionStartAutoReap:
    def test_hook_reaps_a_25h_old_worktree(self, ephemeral_repo: Path) -> None:
        # Arrange — a real worktree backdated past the 24h threshold;
        # clean tree + not-ahead-of-develop so the safety predicate
        # admits it.
        target = _make_worktree(ephemeral_repo, "stale-reapable")
        _age_worktree(target, hours=25)
        payload = _session_start_payload(ephemeral_repo)
        # Act
        _run_session_start(payload)
        # Assert — git no longer registers the worktree.
        listed = _git(ephemeral_repo, "worktree", "list", "--porcelain")
        assert f"worktree {target}" not in listed

    def test_hook_preserves_a_2h_old_worktree(self, ephemeral_repo: Path) -> None:
        # Arrange — a real worktree well within the 24h grace window.
        target = _make_worktree(ephemeral_repo, "fresh-preserve")
        _age_worktree(target, hours=2)
        payload = _session_start_payload(ephemeral_repo)
        # Act
        _run_session_start(payload)
        # Assert — git still registers the worktree (age gate skipped it).
        listed = _git(ephemeral_repo, "worktree", "list", "--porcelain")
        assert f"worktree {target}" in listed

    def test_hook_preserves_a_dirty_stale_worktree(self, ephemeral_repo: Path) -> None:
        # Arrange — stale by mtime, but DIRTY (untracked file) so the
        # safety predicate refuses. Auto-reap must NEVER destroy dirty
        # work (lead-learnings/19 doctrine, paired with PR #369 rescue).
        target = _make_worktree(ephemeral_repo, "dirty-stale")
        (target / "scratch.txt").write_text("uncommitted work\n")
        _age_worktree(target, hours=48)
        payload = _session_start_payload(ephemeral_repo)
        # Act
        _run_session_start(payload)
        # Assert — predicate-gated; the dirty worktree is preserved.
        listed = _git(ephemeral_repo, "worktree", "list", "--porcelain")
        assert f"worktree {target}" in listed

    def test_hook_exits_zero_on_reap_success(self, ephemeral_repo: Path) -> None:
        # Arrange
        target = _make_worktree(ephemeral_repo, "exit-code-probe")
        _age_worktree(target, hours=72)
        payload = _session_start_payload(ephemeral_repo)
        # Act
        result = _run_session_start(payload)
        # Assert — SessionStart MUST be fail-open: exit 0 on success.
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# No-op path: missing roots, broken env -> exit 0, no crash
# ---------------------------------------------------------------------------


class TestSessionStartNoop:
    def test_hook_no_op_when_no_agent_worktrees_dir_exists(
        self, tmp_path: Path
    ) -> None:
        # Arrange — a non-git tmpdir with NO .worktrees/ root; an empty
        # $HOME (so .claude/worktrees also resolves to nothing). The
        # hook must walk away cleanly.
        empty_cwd = tmp_path / "no-repo-here"
        empty_cwd.mkdir()
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        env = {**os.environ, "HOME": str(empty_home)}
        payload = _session_start_payload(empty_cwd)
        # Act
        result = _run_session_start(payload, env=env)
        # Assert — exit 0, hook is a true no-op when there's nothing
        # to scan.
        assert result.returncode == 0

    def test_hook_fails_open_on_prune_error(self, tmp_path: Path) -> None:
        # Arrange — drive the hook with an empty PATH so the subprocess
        # spawn of ``git`` fails (FileNotFoundError). Every reap attempt
        # MUST degrade silently; the hook MUST NOT exit non-zero.
        target_cwd = tmp_path / "any-cwd"
        target_cwd.mkdir()
        env = {"HOME": str(tmp_path), "PATH": ""}
        payload = _session_start_payload(target_cwd)
        # Act
        result = _run_session_start(payload, env=env)
        # Assert — fail-open: even with no git binary reachable, exit 0.
        assert result.returncode == 0
