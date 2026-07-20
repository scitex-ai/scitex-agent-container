"""Shared fixtures + helpers for the claude_worktree_hooks tests.

Lives next to ``test_worktree_create.py`` / ``test_worktree_remove.py``;
pytest auto-loads ``conftest.py`` from the test dir so both files share
the ephemeral-git-repo fixture, the path constants, and the JSON-payload
builders without duplicating the bootstrap.

Audit (PS-204 §2): each test file mirrors a single src module, so the
shared scaffolding HAS to live here — putting it in either test file
would create a duplicate-helper-source-of-truth split.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

# The hook scripts live alongside the source-of-truth asset dir; the
# tests resolve them by package-relative import so a refactor of the
# asset path is caught at import time, not at script-spawn time.
HOOK_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "claude_worktree_hooks"
)
CREATE_SCRIPT = HOOK_DIR / "worktree_create.py"
REMOVE_SCRIPT = HOOK_DIR / "worktree_remove.py"


def _git(cwd: Path, *args: str) -> str:
    """Run ``git -C cwd <args>``; assert success and return stdout."""
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _run_hook(
    script: Path,
    payload: dict,
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook script with the given JSON payload on stdin.

    Returns the completed process so individual tests can assert on
    returncode / stdout / stderr without retrying or coupling.

    ``env`` (when given) fully REPLACES the subprocess environment —
    the owner-stamp tests use it to pin ``SCITEX_TODO_AGENT_ID`` (or to
    prove its absence) deterministically, independent of the runner's
    ambient value. When ``None`` the child inherits the parent env.
    """
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _create_payload(name: str, cwd: Path) -> dict:
    """Mirror the shape Claude Code's ``executeWorktreeCreateHook``
    constructs at runtime (verified 2026-06-06 against the bundled binary)."""
    return {
        "hook_event_name": "WorktreeCreate",
        "name": name,
        "cwd": str(cwd),
        "session_id": "test-session-id",
        "transcript_path": str(cwd / ".transcript"),
        "permission_mode": "default",
        "agent_id": "test-agent-id",
        "agent_type": "test",
    }


def _remove_payload(worktree_path: str, cwd: Path | str) -> dict:
    """Mirror the shape Claude Code's ``executeWorktreeRemoveHook`` constructs."""
    return {
        "hook_event_name": "WorktreeRemove",
        "worktree_path": worktree_path,
        "session_id": "test-session-id",
        "transcript_path": str(Path(str(cwd)) / ".transcript"),
        "permission_mode": "default",
        "agent_id": "test-agent-id",
        "agent_type": "test",
        "cwd": str(cwd),
    }


@pytest.fixture
def ephemeral_repo(tmp_path: Path) -> Iterator[Path]:
    """Create a real on-disk git repo with a ``develop`` branch + initial commit.

    The hooks' "fresh-from-origin/develop-else-HEAD" base-resolution path
    needs SOMETHING to branch from; an empty fresh repo (no commits) has
    no resolvable HEAD and would fail in a way that's noise here. We
    seed a single commit on ``develop`` so the create-hook exercises its
    real base-resolution branch instead of an edge case.

    No ``origin`` is configured — that exercises the "HEAD fallback"
    branch of ``_resolve_base`` and keeps the test self-contained (no
    network, no shared remote). A separate test pins the
    ``origin/develop`` branch when an origin IS configured.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=develop")
    # Identity is required for ``git commit``; tests must not depend on
    # the host's ``git config user.*``.
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "SAC Test")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    yield repo
