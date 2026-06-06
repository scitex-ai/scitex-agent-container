"""End-to-end tests for the Claude Code worktree-relocation hooks.

NO MOCKS. Every test drives the real hook script as a subprocess against
a real ephemeral git repository created via ``tmp_path`` and ``git
init``. The hook contract is what Claude Code's bundled binary enforces
at runtime (verified 2026-06-06 in the deobfuscated
``executeWorktreeCreateHook`` JS function); the regression coverage
below pins both directions of the I/O contract + the policy invariants.

TQ: every test carries ``# Arrange`` / ``# Act`` / ``# Assert`` markers,
a ≥3-word descriptive name, and exactly one assertion.
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


# ---------------------------------------------------------------------------
# Real ephemeral git repo — used by every test that exercises hook IO
# ---------------------------------------------------------------------------


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
    script: Path, payload: dict, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    """Invoke the hook script with the given JSON payload on stdin.

    Returns the completed process so individual tests can assert on
    returncode / stdout / stderr without retrying or coupling.
    """
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
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


# ---------------------------------------------------------------------------
# worktree_create.py — the headline relocation: target lands under .worktrees/
# ---------------------------------------------------------------------------


class TestWorktreeCreateRelocation:
    def test_create_lands_under_dotworktrees_not_dotclaude(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange — the whole point of the hook: stop creations under
        # ``.claude/worktrees/`` and put them under ``.worktrees/``.
        payload = _create_payload("relocate-probe", ephemeral_repo)

        # Act
        result = _run_hook(CREATE_SCRIPT, payload)

        # Assert — single-line stdout = absolute path under .worktrees/
        emitted = result.stdout.strip()
        assert emitted == str(ephemeral_repo / ".worktrees" / "relocate-probe")

    def test_create_returns_zero_on_success(self, ephemeral_repo: Path) -> None:
        # Arrange
        payload = _create_payload("zero-exit", ephemeral_repo)
        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        # Assert — SDK fails loud on any non-zero exit; pin success.
        assert result.returncode == 0

    def test_created_path_is_a_real_directory(self, ephemeral_repo: Path) -> None:
        # Arrange — SDK enforces "WorktreeCreate hook returned a path
        # that is not a directory" if we echo a non-existent path. The
        # hook MUST create the dir before returning.
        payload = _create_payload("real-dir-probe", ephemeral_repo)
        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        # Assert
        target = Path(result.stdout.strip())
        assert target.is_dir()

    def test_created_worktree_registered_with_git(self, ephemeral_repo: Path) -> None:
        # Arrange — relocation is meaningless if it doesn't go through
        # ``git worktree add``: the human-side cron prunes by mtime
        # AND by registered status. Pin the registration.
        payload = _create_payload("registered-probe", ephemeral_repo)
        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        target = Path(result.stdout.strip())
        # Assert
        listed = _git(ephemeral_repo, "worktree", "list", "--porcelain")
        assert f"worktree {target}" in listed

    def test_created_worktree_branch_is_claude_namespaced(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange — branch naming policy: ``claude/<name>`` so bulk
        # prune (``git branch -D 'claude/*'``) works.
        payload = _create_payload("branch-policy-probe", ephemeral_repo)
        # Act
        _run_hook(CREATE_SCRIPT, payload)
        # Assert — the branch list shows ``claude/<name>``.
        branches = _git(ephemeral_repo, "branch", "--list", "claude/*")
        assert "claude/branch-policy-probe" in branches


# ---------------------------------------------------------------------------
# Idempotence — re-trigger MUST NOT double-create / collide
# ---------------------------------------------------------------------------


class TestWorktreeCreateIdempotence:
    def test_second_trigger_with_same_name_re_emits_same_path(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange — Claude Code may re-trigger on session resume; the
        # hook MUST be safe to call twice without erroring out on
        # "worktree already exists".
        payload = _create_payload("idempotent-probe", ephemeral_repo)
        first = _run_hook(CREATE_SCRIPT, payload)
        # Act
        second = _run_hook(CREATE_SCRIPT, payload)
        # Assert
        assert second.stdout.strip() == first.stdout.strip()

    def test_second_trigger_with_same_name_exits_clean(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange
        payload = _create_payload("idempotent-exit", ephemeral_repo)
        _run_hook(CREATE_SCRIPT, payload)
        # Act
        second = _run_hook(CREATE_SCRIPT, payload)
        # Assert — even when the worktree exists, returncode is 0.
        assert second.returncode == 0


# ---------------------------------------------------------------------------
# Lingering-branch case — branch exists but worktree doesn't (post-prune)
# ---------------------------------------------------------------------------


class TestWorktreeCreateOnLingeringBranch:
    def test_lingering_branch_attaches_without_recreation_attempt(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange — operator pruned the worktree dir (via cron) but the
        # ``claude/<name>`` branch lingers in refs/heads/. A re-trigger
        # MUST re-attach a fresh worktree to the existing branch,
        # not fail with "branch already exists".
        name = "lingering-branch"
        _git(ephemeral_repo, "branch", f"claude/{name}")
        payload = _create_payload(name, ephemeral_repo)

        # Act
        result = _run_hook(CREATE_SCRIPT, payload)

        # Assert
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Loud-fail input validation — every failure path returns >0 + stderr
# ---------------------------------------------------------------------------


class TestWorktreeCreateLoudFailure:
    def test_missing_name_exits_nonzero(self, ephemeral_repo: Path) -> None:
        # Arrange
        payload = _create_payload("ignored", ephemeral_repo)
        payload.pop("name")
        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        # Assert
        assert result.returncode != 0

    def test_missing_name_stderr_names_the_field(self, ephemeral_repo: Path) -> None:
        # Arrange — SDK surfaces stderr in its hook-failure message;
        # operator must see WHICH field was missing.
        payload = _create_payload("ignored", ephemeral_repo)
        payload.pop("name")
        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        # Assert
        assert "name" in result.stderr

    def test_non_git_cwd_exits_nonzero(self, tmp_path: Path) -> None:
        # Arrange — ``cwd`` exists but isn't a git repo (no .git, no
        # toplevel resolvable). Hook must refuse, not write to random
        # filesystem paths.
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        payload = _create_payload("no-repo", non_repo)
        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        # Assert
        assert result.returncode != 0

    def test_invalid_name_traversal_is_rejected(self, ephemeral_repo: Path) -> None:
        # Arrange — ``name`` arrives from session bookkeeping; defense-
        # in-depth, reject any path-traversal-shaped input so the hook
        # can never write outside ``<git-root>/.worktrees/``.
        payload = _create_payload("../escape", ephemeral_repo)
        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        # Assert
        assert result.returncode != 0

    def test_empty_stdin_exits_nonzero(self, tmp_path: Path) -> None:
        # Arrange — hook fired with no input (misconfiguration) must
        # fail loud, not silently echo something the SDK will accept.
        result = subprocess.run(
            [sys.executable, str(CREATE_SCRIPT)],
            input="",
            capture_output=True,
            text=True,
        )
        # Assert
        assert result.returncode != 0

    def test_malformed_json_stdin_exits_nonzero(self) -> None:
        # Arrange
        result = subprocess.run(
            [sys.executable, str(CREATE_SCRIPT)],
            input="not json at all",
            capture_output=True,
            text=True,
        )
        # Assert
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Base-ref resolution — origin/develop wins when configured, else HEAD
# ---------------------------------------------------------------------------


class TestWorktreeCreateBaseResolution:
    def test_origin_develop_wins_when_configured(
        self, ephemeral_repo: Path, tmp_path: Path
    ) -> None:
        # Arrange — wire an ``origin`` remote pointing at a sibling
        # bare repo so ``origin/develop`` is fetch-resolvable, then
        # confirm the hook branches off that tip and NOT off HEAD.
        upstream = tmp_path / "upstream.git"
        _git(tmp_path, "init", "-q", "--bare", str(upstream))
        _git(ephemeral_repo, "remote", "add", "origin", str(upstream))
        _git(ephemeral_repo, "push", "-q", "origin", "develop")
        # Add an EXTRA commit on local develop AFTER pushing; the
        # remote tip stays at the seed. A correctly-resolving base
        # picks ``origin/develop`` (seed tip), not HEAD (local tip).
        (ephemeral_repo / "newer.txt").write_text("local-only\n")
        _git(ephemeral_repo, "add", "newer.txt")
        _git(ephemeral_repo, "commit", "-q", "-m", "local-only")

        origin_develop_sha = _git(ephemeral_repo, "rev-parse", "origin/develop")
        payload = _create_payload("base-resolve-probe", ephemeral_repo)

        # Act
        result = _run_hook(CREATE_SCRIPT, payload)
        target = Path(result.stdout.strip())

        # Assert — the new worktree's HEAD == origin/develop (NOT
        # the local develop HEAD which has the extra commit).
        new_sha = _git(target, "rev-parse", "HEAD")
        assert new_sha == origin_develop_sha


# ---------------------------------------------------------------------------
# worktree_remove.py — counterpart, idempotent + loud on failure
# ---------------------------------------------------------------------------


class TestWorktreeRemove:
    def test_remove_unregisters_a_created_worktree(self, ephemeral_repo: Path) -> None:
        # Arrange — create one via the create-hook, then run the
        # remove-hook against the path it returned.
        create_payload = _create_payload("remove-probe", ephemeral_repo)
        created = _run_hook(CREATE_SCRIPT, create_payload).stdout.strip()
        remove_payload = {
            "hook_event_name": "WorktreeRemove",
            "worktree_path": created,
            "session_id": "test-session-id",
            "transcript_path": str(ephemeral_repo / ".transcript"),
            "permission_mode": "default",
            "agent_id": "test-agent-id",
            "agent_type": "test",
            "cwd": str(ephemeral_repo),
        }

        # Act
        _run_hook(REMOVE_SCRIPT, remove_payload)

        # Assert
        listed = _git(ephemeral_repo, "worktree", "list", "--porcelain")
        assert f"worktree {created}" not in listed

    def test_remove_exits_zero_on_success(self, ephemeral_repo: Path) -> None:
        # Arrange
        create_payload = _create_payload("remove-exit-probe", ephemeral_repo)
        created = _run_hook(CREATE_SCRIPT, create_payload).stdout.strip()
        remove_payload = {
            "hook_event_name": "WorktreeRemove",
            "worktree_path": created,
            "session_id": "s",
            "transcript_path": str(ephemeral_repo / ".transcript"),
            "permission_mode": "default",
            "agent_id": "a",
            "agent_type": "t",
            "cwd": str(ephemeral_repo),
        }
        # Act
        result = _run_hook(REMOVE_SCRIPT, remove_payload)
        # Assert
        assert result.returncode == 0

    def test_remove_is_idempotent_on_already_gone_path(self, tmp_path: Path) -> None:
        # Arrange — operator's daily prune cron already wiped the
        # worktree; SDK re-fires the remove hook on a vanished path.
        # The desired end-state ("this path is no longer a worktree")
        # is already true, so the hook must succeed silently.
        gone_path = tmp_path / "gone" / "worktree"
        remove_payload = {
            "hook_event_name": "WorktreeRemove",
            "worktree_path": str(gone_path),
            "session_id": "s",
            "transcript_path": str(tmp_path / ".transcript"),
            "permission_mode": "default",
            "agent_id": "a",
            "agent_type": "t",
            "cwd": str(tmp_path),
        }
        # Act
        result = _run_hook(REMOVE_SCRIPT, remove_payload)
        # Assert
        assert result.returncode == 0

    def test_remove_with_empty_stdin_exits_nonzero(self) -> None:
        # Arrange
        result = subprocess.run(
            [sys.executable, str(REMOVE_SCRIPT)],
            input="",
            capture_output=True,
            text=True,
        )
        # Assert
        assert result.returncode != 0

    def test_remove_with_missing_worktree_path_exits_nonzero(self) -> None:
        # Arrange
        payload = {
            "hook_event_name": "WorktreeRemove",
            "session_id": "s",
            "transcript_path": "/tmp/t",
            "permission_mode": "default",
            "agent_id": "a",
            "agent_type": "t",
            "cwd": "/tmp",
        }
        # Act
        result = subprocess.run(
            [sys.executable, str(REMOVE_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        # Assert
        assert result.returncode != 0
