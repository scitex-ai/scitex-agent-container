"""Contract tests for the external, harness-neutral worktree policy gate."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle._worktree_policy import (
    WorktreePolicyError,
    enforce_task_worktree_policy,
    refresh_task_worktree_owner,
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


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Iterator[Path]:
    """Use an isolated real runtime directory and restore the environment."""
    key = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"
    previous = os.environ.get(key)
    value = tmp_path / "runtime"
    os.environ[key] = str(value)
    try:
        yield value
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@contextmanager
def _environment(key: str, value: str) -> Iterator[None]:
    previous = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@contextmanager
def _replace_attribute(target: object, name: str, value: object) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def _captured_policy_error(call) -> str:
    try:
        call()
    except WorktreePolicyError as exc:
        return str(exc)
    return ""


def _config(workdir: Path, *, kind: str = "Agent") -> AgentConfig:
    return AgentConfig(name="policy-test", kind=kind, workdir=str(workdir))


def _authority(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "develop"], check=True)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return path


@pytest.fixture
def explicit_linked_context(tmp_path, runtime_dir, real_policy_cli):
    repo = _authority(tmp_path / "explicit-repo")
    linked = repo / ".worktrees/spec-linked"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/spec-linked",
            str(linked),
        ],
        check=True,
    )
    config = _config(linked)
    enforce_task_worktree_policy(config, cli_path=real_policy_cli)
    return repo, linked, config


def test_explicit_linked_plan_records_the_verified_primary_root(
    explicit_linked_context,
):
    # Arrange
    repo, _, config = explicit_linked_context
    # Act
    root = config._worktree_plan.primary_repo_root
    # Assert
    assert root == str(repo)


def test_explicit_linked_plan_retains_its_existing_neutral_root(
    explicit_linked_context,
):
    # Arrange
    _, linked, config = explicit_linked_context
    # Act
    root = config._worktree_plan.repo_root
    # Assert
    assert root == str(linked)


def test_explicit_linked_owner_keeps_its_original_root_semantics(
    explicit_linked_context,
):
    # Arrange
    _, linked, config = explicit_linked_context
    # Act
    owner = json.loads(Path(config._worktree_plan.owner_file).read_text())
    # Assert
    assert owner["repo_root"] == str(linked)


def test_explicit_linked_reuse_keeps_retained_owner_bytes(
    explicit_linked_context, real_policy_cli
):
    # Arrange
    _, _, config = explicit_linked_context
    owner = Path(config._worktree_plan.owner_file)
    before = owner.read_bytes()
    # Act
    enforce_task_worktree_policy(config, cli_path=real_policy_cli)
    # Assert
    assert owner.read_bytes() == before


@pytest.fixture(params=["repo_root", "git_common_dir"])
def conflicting_linked_metadata(
    explicit_linked_context, tmp_path, real_policy_cli, request
):
    _, _, config = explicit_linked_context
    other = _authority(tmp_path / "different-real-repo")
    replacements = {"repo_root": str(other), "git_common_dir": str(other / ".git")}
    wrapper = tmp_path / "conflicting-neutral-metadata"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys\n"
        f"result = subprocess.run([{str(real_policy_cli)!r}, *sys.argv[1:]], capture_output=True, text=True)\n"
        "payload = json.loads(result.stdout)\n"
        "if sys.argv[1] == 'inspect' and payload.get('surface') == 'linked-worktree':\n"
        f"    payload[{request.param!r}] = {replacements[request.param]!r}\n"
        "print(json.dumps(payload))\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    owner = Path(config._worktree_plan.owner_file)
    before = owner.read_bytes()
    error = _captured_policy_error(
        lambda: enforce_task_worktree_policy(config, cli_path=wrapper)
    )
    return error, owner, before


def test_conflicting_neutral_git_metadata_refuses_adoption(conflicting_linked_metadata):
    # Arrange
    error, _, _ = conflicting_linked_metadata
    # Act
    refused = "does not match its Git checkout" in error
    # Assert
    assert refused is True


def test_conflicting_neutral_git_metadata_preserves_the_owner(
    conflicting_linked_metadata,
):
    # Arrange
    _, owner, before = conflicting_linked_metadata
    # Act
    after = owner.read_bytes()
    # Assert
    assert after == before


def test_nonstandard_git_common_directory_gains_no_primary_trust(
    tmp_path, runtime_dir, real_policy_cli
):
    # Arrange
    repo = _authority(tmp_path / "external-git-repo")
    common = tmp_path / "separate-git-metadata"
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", f"--separate-git-dir={common}"],
        check=True,
    )
    linked = repo / ".worktrees/separate-linked"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/separate-linked",
            str(linked),
        ],
        check=True,
    )
    config = _config(linked)
    # Act
    enforce_task_worktree_policy(config, cli_path=real_policy_cli)
    # Assert
    assert config._worktree_plan.primary_repo_root == ""


def test_real_cli_allow_attaches_the_resolved_proof(
    real_policy_cli: Path, tmp_path: Path, runtime_dir: Path
) -> None:
    # Arrange
    repo = _authority(tmp_path / "repo")
    cfg = _config(repo)

    # Act
    proof = enforce_task_worktree_policy(cfg, cli_path=real_policy_cli)

    # Assert
    assert proof is not None and (
        proof.policy_sha256,
        proof.projection_sha256,
        proof.surface,
        proof.authored_workdir,
        proof.resolved_workdir.startswith(str(repo / ".worktrees")),
        Path(proof.resolved_workdir).is_dir(),
        cfg.workdir,
        cfg._worktree_policy_proof,  # type: ignore[attr-defined]
    ) == (
        "1" * 64,
        "2" * 64,
        "linked-worktree",
        str(repo),
        True,
        True,
        proof.resolved_workdir,
        proof,
    )


def test_dry_plan_names_worktree_without_creating_it(
    real_policy_cli: Path, tmp_path: Path, runtime_dir: Path
) -> None:
    # Arrange
    repo = _authority(tmp_path / "repo")
    cfg = _config(repo)

    # Act
    proof = enforce_task_worktree_policy(cfg, provision=False, cli_path=real_policy_cli)

    # Assert
    assert proof is not None and (
        proof.surface,
        Path(proof.resolved_workdir).exists(),
        cfg.workdir,
    ) == ("planned-linked-worktree", False, proof.resolved_workdir)


def test_owned_dirty_worktree_is_reused_without_losing_agent_work(
    real_policy_cli: Path, tmp_path: Path, runtime_dir: Path
) -> None:
    # Arrange
    repo = _authority(tmp_path / "repo")
    first = enforce_task_worktree_policy(_config(repo), cli_path=real_policy_cli)
    if first is None:
        raise AssertionError("agent policy unexpectedly returned no proof")
    marker = Path(first.resolved_workdir) / "agent-work.txt"
    marker.write_text("unfinished\n", encoding="utf-8")

    # Act
    second = enforce_task_worktree_policy(_config(repo), cli_path=real_policy_cli)

    # Assert
    assert second is not None and (
        second.resolved_workdir,
        marker.read_text(encoding="utf-8"),
    ) == (first.resolved_workdir, "unfinished\n")


def test_reuse_refreshes_session_identity_in_owner_record(
    real_policy_cli: Path, tmp_path: Path, runtime_dir: Path
) -> None:
    # Arrange
    repo = _authority(tmp_path / "repo")
    first_config = _config(repo)
    first_config.claude.session = "fresh"
    enforce_task_worktree_policy(first_config, cli_path=real_policy_cli)

    resumed = _config(repo)
    resumed.claude.session = "resume"
    resumed.claude.resume_id = "session-123"

    # Act
    enforce_task_worktree_policy(resumed, cli_path=real_policy_cli)
    resumed.env["SAC_INSTANCE_UUID"] = "incarnation-456"
    refresh_task_worktree_owner(resumed)

    owner = json.loads(
        (runtime_dir / "policy-test" / "worktree-owner.json").read_text(
            encoding="utf-8"
        )
    )

    # Assert
    assert (owner["session"], owner["resume_id"], owner["incarnation"]) == (
        "resume",
        "session-123",
        "incarnation-456",
    )


def test_existing_unowned_branch_refuses_without_attaching_it(
    real_policy_cli: Path, tmp_path: Path, runtime_dir: Path
) -> None:
    # Arrange
    repo = _authority(tmp_path / "repo")
    cfg = _config(repo)
    name = "policy-test-bcf1ebd4"
    branch = f"feature/sac-{name}"
    subprocess.run(["git", "-C", str(repo), "branch", branch], check=True)

    # Act
    error = _captured_policy_error(
        lambda: enforce_task_worktree_policy(cfg, cli_path=real_policy_cli)
    )

    # Assert
    assert (
        "branch exists without ownership" in error,
        (repo / ".worktrees" / f"sac-{name}").exists(),
    ) == (True, False)


def test_dirty_authority_refuses_and_names_preserved_changes(
    real_policy_cli: Path, tmp_path: Path, runtime_dir: Path
) -> None:
    # Arrange
    repo = _authority(tmp_path / "repo")
    (repo / "tracked.txt").write_text("unfinished\n", encoding="utf-8")

    # Act
    error = _captured_policy_error(
        lambda: enforce_task_worktree_policy(_config(repo), cli_path=real_policy_cli)
    )

    # Assert
    assert (
        "tracked.txt" in error,
        (repo / "tracked.txt").read_text(encoding="utf-8"),
    ) == (True, "unfinished\n")


@pytest.mark.parametrize("mode", ["deny", "stale", "invalid-json", "hash-drift"])
def test_real_cli_non_approval_fails_closed(
    real_policy_cli: Path,
    tmp_path: Path,
    runtime_dir: Path,
    mode: str,
) -> None:
    # Arrange
    repo = _authority(tmp_path / "repo")

    # Act
    with _environment("SCITEX_POLICY_FIXTURE_MODE", mode):
        # Assert
        with pytest.raises(WorktreePolicyError):
            enforce_task_worktree_policy(_config(repo), cli_path=real_policy_cli)


def test_missing_cli_fails_closed(tmp_path: Path) -> None:
    # Arrange
    missing = tmp_path / "does-not-exist"

    # Act
    # Assert
    with pytest.raises(WorktreePolicyError, match="unavailable"):
        enforce_task_worktree_policy(_config(tmp_path), cli_path=missing)


def test_proxy_does_not_require_a_write_capable_task_gate(tmp_path: Path) -> None:
    # Arrange
    config = _config(tmp_path, kind="AgentProxy")

    # Act
    proof = enforce_task_worktree_policy(
        config,
        cli_path=tmp_path / "does-not-exist",
    )

    # Assert
    assert proof is None


def test_fixture_is_an_executable_process_not_a_subprocess_mock(
    real_policy_cli: Path,
) -> None:
    # Arrange
    executable_bit = os.X_OK

    # Act
    executable = os.access(real_policy_cli, executable_bit)

    # Assert
    assert executable


def test_health_restart_rechecks_policy_before_constructing_runtime(
    tmp_path: Path,
) -> None:
    from scitex_agent_container._lifecycle import _runtime_select, _worktree_policy
    from scitex_agent_container._lifecycle._instances import restart_and_record

    # Arrange
    def refuse(_config, **_kwargs) -> None:
        raise WorktreePolicyError("restart refusal")

    # Act
    # Assert
    with _replace_attribute(_worktree_policy, "enforce_task_worktree_policy", refuse):
        with pytest.raises(WorktreePolicyError, match="restart refusal"):
            restart_and_record(_config(tmp_path), _runtime_select._get_runtime)
