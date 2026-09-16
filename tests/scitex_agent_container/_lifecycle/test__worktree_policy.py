"""Contract tests for the external, harness-neutral worktree policy gate."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._worktree_policy import (
    WorktreePolicyError,
    enforce_task_worktree_policy,
)
from scitex_agent_container.config import AgentConfig

FIXTURE = (
    Path(__file__).parent
    / "_fixtures"
    / "worktree-policy"
    / "src"
    / ".bin"
    / "scitex-worktree-policy"
)


@pytest.fixture
def real_policy_cli() -> Path:
    """A real executable subprocess fixture, never a mocked ``subprocess``."""
    FIXTURE.chmod(FIXTURE.stat().st_mode | 0o111)
    return FIXTURE


def _config(workdir: Path, *, kind: str = "Agent") -> AgentConfig:
    return AgentConfig(name="policy-test", kind=kind, workdir=str(workdir))


def test_real_cli_allow_attaches_the_resolved_proof(
    real_policy_cli: Path, tmp_path: Path
) -> None:
    cfg = _config(tmp_path)

    proof = enforce_task_worktree_policy(cfg, cli_path=real_policy_cli)

    assert proof is not None
    assert proof.policy_sha256 == "1" * 64
    assert proof.projection_sha256 == "2" * 64
    assert proof.repo_root == str(tmp_path.resolve())
    assert cfg._worktree_policy_proof is proof  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["deny", "stale", "invalid-json", "hash-drift"])
def test_real_cli_non_approval_fails_closed(
    real_policy_cli: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("SCITEX_POLICY_FIXTURE_MODE", mode)

    with pytest.raises(WorktreePolicyError):
        enforce_task_worktree_policy(_config(tmp_path), cli_path=real_policy_cli)


def test_missing_cli_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(WorktreePolicyError, match="unavailable"):
        enforce_task_worktree_policy(
            _config(tmp_path), cli_path=tmp_path / "does-not-exist"
        )


def test_proxy_does_not_require_a_write_capable_task_gate(tmp_path: Path) -> None:
    proof = enforce_task_worktree_policy(
        _config(tmp_path, kind="AgentProxy"),
        cli_path=tmp_path / "does-not-exist",
    )

    assert proof is None


def test_fixture_is_an_executable_process_not_a_subprocess_mock(
    real_policy_cli: Path,
) -> None:
    assert os.access(real_policy_cli, os.X_OK)


def test_health_restart_rechecks_policy_before_constructing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scitex_agent_container._lifecycle import _runtime_select, _worktree_policy
    from scitex_agent_container._lifecycle._instances import restart_and_record

    def refuse(_config) -> None:
        raise WorktreePolicyError("restart refusal")

    monkeypatch.setattr(_worktree_policy, "enforce_task_worktree_policy", refuse)

    with pytest.raises(WorktreePolicyError, match="restart refusal"):
        restart_and_record(_config(tmp_path), _runtime_select._get_runtime)
