"""End-to-end tests for the ``worktree_remove.py`` hook.

NO MOCKS. Every test drives the real hook script as a subprocess against
a real ephemeral git repository (see ``conftest.py``'s ``ephemeral_repo``
fixture). Covers the SDK's ``WorktreeRemove`` contract: tear down a
worktree on the matching payload, succeed silently on an already-gone
path (operator's cron may have already pruned), fail loud on bad input.

TQ: every test carries ``# Arrange`` / ``# Act`` / ``# Assert`` markers,
a ≥3-word descriptive name, and exactly one assertion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .conftest import (
    CREATE_SCRIPT,
    REMOVE_SCRIPT,
    _create_payload,
    _git,
    _remove_payload,
    _run_hook,
)


class TestWorktreeRemove:
    def test_remove_unregisters_a_created_worktree(self, ephemeral_repo: Path) -> None:
        # Arrange — create one via the create-hook, then run the
        # remove-hook against the path it returned.
        created = _run_hook(
            CREATE_SCRIPT, _create_payload("remove-probe", ephemeral_repo)
        ).stdout.strip()
        payload = _remove_payload(created, ephemeral_repo)
        # Act
        _run_hook(REMOVE_SCRIPT, payload)
        # Assert
        listed = _git(ephemeral_repo, "worktree", "list", "--porcelain")
        assert f"worktree {created}" not in listed

    def test_remove_exits_zero_on_success(self, ephemeral_repo: Path) -> None:
        # Arrange
        created = _run_hook(
            CREATE_SCRIPT, _create_payload("remove-exit-probe", ephemeral_repo)
        ).stdout.strip()
        payload = _remove_payload(created, ephemeral_repo)
        # Act
        result = _run_hook(REMOVE_SCRIPT, payload)
        # Assert
        assert result.returncode == 0

    def test_remove_is_idempotent_on_already_gone_path(self, tmp_path: Path) -> None:
        # Arrange — operator's daily prune cron already wiped the
        # worktree; SDK re-fires the remove hook on a vanished path.
        # The desired end-state ("this path is no longer a worktree")
        # is already true, so the hook must succeed silently.
        gone_path = tmp_path / "gone" / "worktree"
        payload = _remove_payload(str(gone_path), tmp_path)
        # Act
        result = _run_hook(REMOVE_SCRIPT, payload)
        # Assert
        assert result.returncode == 0

    def test_remove_with_empty_stdin_exits_nonzero(self) -> None:
        # Arrange
        script = REMOVE_SCRIPT
        # Act
        result = subprocess.run(
            [sys.executable, str(script)],
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
