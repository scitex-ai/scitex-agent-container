"""End-to-end tests for the ``worktree_create.py`` hook.

NO MOCKS. Every test drives the real hook script as a subprocess against
a real ephemeral git repository (see ``conftest.py``'s ``ephemeral_repo``
fixture). The hook contract is what Claude Code's bundled binary enforces
at runtime (verified 2026-06-06 in the deobfuscated
``executeWorktreeCreateHook`` JS function); the regression coverage
below pins both directions of the I/O contract + the policy invariants.

TQ: every test carries ``# Arrange`` / ``# Act`` / ``# Assert`` markers,
a ≥3-word descriptive name, and exactly one assertion.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .conftest import (
    CREATE_SCRIPT,
    _create_payload,
    _git,
    _run_hook,
)

# Both board-identity variables the hook reads. The runner's own container
# exports the canonical one, so an owner-stamp test that only strips the
# retired name would inherit a live identity and stamp when it must not.
_AGENT_ID_ENVS = ("SCITEX_CARDS_AGENT_ID", "SCITEX_TODO_AGENT_ID")


def _env_without_agent_ids() -> dict:
    """A copy of the ambient env with EVERY board-identity var removed."""
    return {k: v for k, v in os.environ.items() if k not in _AGENT_ID_ENVS}

# ---------------------------------------------------------------------------
# Relocation invariant — target lands under .worktrees/, NOT .claude/worktrees
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

    def test_empty_stdin_exits_nonzero(self) -> None:
        # Arrange — hook fired with no input (misconfiguration) must
        # fail loud, not silently echo something the SDK will accept.
        script = CREATE_SCRIPT
        # Act
        result = subprocess.run(
            [sys.executable, str(script)],
            input="",
            capture_output=True,
            text=True,
        )
        # Assert
        assert result.returncode != 0

    def test_malformed_json_stdin_exits_nonzero(self) -> None:
        # Arrange
        script = CREATE_SCRIPT
        bad_input = "not json at all"
        # Act
        result = subprocess.run(
            [sys.executable, str(script)],
            input=bad_input,
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
# Owner stamp — WRITE half of the shared-checkout mis-attribution fix.
#
# The hook stamps the creating agent's id at ``<git-dir>/sac-owner`` (OUT of
# the working tree) so the pre-stop rescue can rescue ONLY worktrees it owns
# (default-deny otherwise) on a checkout shared by several agents.
# ---------------------------------------------------------------------------


class TestWorktreeCreateOwnerStamp:
    def test_owner_stamp_written_to_private_gitdir(self, ephemeral_repo: Path) -> None:
        # Arrange — SCITEX_CARDS_AGENT_ID identifies the owning agent; the
        # hook must stamp it into the worktree's PRIVATE gitdir.
        payload = _create_payload("stamp-probe", ephemeral_repo)
        env = {**_env_without_agent_ids(), "SCITEX_CARDS_AGENT_ID": "stampy-agent"}
        # Act
        result = _run_hook(CREATE_SCRIPT, payload, env=env)
        target = Path(result.stdout.strip())
        git_dir = _git(target, "rev-parse", "--absolute-git-dir")
        # Assert — the marker holds the agent id, in the private gitdir.
        assert (Path(git_dir) / "sac-owner").read_text().strip() == "stampy-agent"

    def test_owner_stamp_is_never_inside_the_working_tree(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange — the marker MUST live outside the working tree so the
        # rescue's ``git add -A`` can never stage it.
        payload = _create_payload("stamp-outside-probe", ephemeral_repo)
        env = {**_env_without_agent_ids(), "SCITEX_CARDS_AGENT_ID": "stampy-agent"}
        # Act
        result = _run_hook(CREATE_SCRIPT, payload, env=env)
        target = Path(result.stdout.strip())
        # Assert — no sac-owner file in the worktree's working directory.
        assert not (target / "sac-owner").exists()

    def test_owner_stamp_falls_back_to_the_retired_agent_id(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange — a container still launched from an old-name spec carries
        # ONLY the retired variable; it must keep stamping.
        payload = _create_payload("stamp-legacy-probe", ephemeral_repo)
        env = {**_env_without_agent_ids(), "SCITEX_TODO_AGENT_ID": "legacy-agent"}
        # Act
        result = _run_hook(CREATE_SCRIPT, payload, env=env)
        target = Path(result.stdout.strip())
        git_dir = _git(target, "rev-parse", "--absolute-git-dir")
        # Assert
        assert (Path(git_dir) / "sac-owner").read_text().strip() == "legacy-agent"

    def test_owner_stamp_prefers_the_canonical_agent_id(
        self, ephemeral_repo: Path
    ) -> None:
        # Arrange — both set (a spec mid-migration): the CANONICAL name wins.
        payload = _create_payload("stamp-both-probe", ephemeral_repo)
        env = {
            **_env_without_agent_ids(),
            "SCITEX_CARDS_AGENT_ID": "canonical-agent",
            "SCITEX_TODO_AGENT_ID": "legacy-agent",
        }
        # Act
        result = _run_hook(CREATE_SCRIPT, payload, env=env)
        target = Path(result.stdout.strip())
        git_dir = _git(target, "rev-parse", "--absolute-git-dir")
        # Assert
        assert (Path(git_dir) / "sac-owner").read_text().strip() == "canonical-agent"

    def test_no_owner_stamp_when_agent_id_unset(self, ephemeral_repo: Path) -> None:
        # Arrange — an unset agent id leaves the worktree UNSTAMPED (the
        # rescue then default-denies it) rather than stamping an empty id.
        payload = _create_payload("no-stamp-probe", ephemeral_repo)
        env = _env_without_agent_ids()
        # Act
        result = _run_hook(CREATE_SCRIPT, payload, env=env)
        target = Path(result.stdout.strip())
        git_dir = _git(target, "rev-parse", "--absolute-git-dir")
        # Assert — no marker written.
        assert not (Path(git_dir) / "sac-owner").exists()
