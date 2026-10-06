"""Packaged worktree policy works without host scripts or a source checkout."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys

import pytest

from scitex_agent_container import _worktree_policy_cli as policy
from scitex_agent_container._lifecycle._worktree_policy import (
    WorktreePolicyError,
    enforce_task_worktree_policy,
)
from scitex_agent_container.config import AgentConfig


def _run(tmp_path, *args, extra_env=None):
    env = dict(os.environ)
    env["SCITEX_AGENT_CONTAINER_HOME"] = str(tmp_path / "sac-state")
    env["SCITEX_DIR"] = str(tmp_path / "scitex")
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(Path(policy.__file__).with_name("__main__.py")), *args],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("facet, expected", [("exit_code", 0), ("decision", "current")])
def test_packaged_defaults_are_complete(tmp_path, facet, expected):
    # Arrange
    operation = "check-projections"
    # Act
    result = _run(tmp_path, operation)
    observed = {
        "exit_code": result.returncode,
        "decision": json.loads(result.stdout)["decision"],
    }
    # Assert
    assert observed[facet] == expected, result.stderr


def _authority(repo):
    subprocess.run(["git", "init", "-q", "-b", "develop", str(repo)], check=True)
    (repo / "tracked.txt").write_text("initial\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


@pytest.mark.parametrize("facet, expected", [("exit_code", 2), ("allowed", False)])
def test_policy_preserves_authority_edit_refusal(tmp_path, facet, expected):
    # Arrange
    _authority(tmp_path)
    # Act
    result = _run(
        tmp_path, "assert-context", "--repo", str(tmp_path), "--intent", "edit"
    )
    observed = {
        "exit_code": result.returncode,
        "allowed": json.loads(result.stdout)["allowed"],
    }
    # Assert
    assert observed[facet] == expected, result.stderr


@pytest.mark.parametrize("content", ["{}\n", "[]\n", "null\n", "invalid JSON\n"])
@pytest.mark.parametrize(
    "facet, expected", [("exit_code", 3), ("allowed", False), ("traceback", False)]
)
def test_invalid_host_policy_is_not_replaced_by_defaults(
    tmp_path, content, facet, expected
):
    # Arrange
    config = tmp_path / "sac-state" / "worktree-policy" / "worktree-policy.json"
    config.parent.mkdir(parents=True)
    config.write_text(content)
    # Act
    result = _run(tmp_path, "check-projections")
    observed = {
        "exit_code": result.returncode,
        "allowed": json.loads(result.stdout)["allowed"],
        "traceback": "Traceback" in result.stderr,
    }
    # Assert
    assert observed[facet] == expected, result.stderr


@pytest.mark.parametrize(
    "facet", ["exit_code", "host_policy", "packaged_policy", "projection_exit"]
)
def test_generation_initializes_host_configuration(tmp_path, facet):
    # Arrange
    before = policy.PACKAGED_POLICY.read_bytes()
    configured = tmp_path / "sac-state" / "worktree-policy" / "worktree-policy.json"
    expected = {
        "exit_code": 0,
        "host_policy": before,
        "packaged_policy": before,
        "projection_exit": 0,
    }
    # Act
    result = _run(tmp_path, "generate-projections")
    observed = {
        "exit_code": result.returncode,
        "host_policy": configured.read_bytes(),
        "packaged_policy": policy.PACKAGED_POLICY.read_bytes(),
        "projection_exit": _run(tmp_path, "check-projections").returncode,
    }
    # Assert
    assert observed[facet] == expected[facet], result.stderr


@pytest.mark.parametrize("facet, expected", [("exit_code", 0), ("decision", "current")])
def test_absolute_checker_path_ignores_other_checkout(tmp_path, facet, expected):
    # Arrange
    other = tmp_path / "other-checkout"
    package = other / "scitex_agent_container"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise RuntimeError('wrong checkout')\n")
    # Act
    result = _run(tmp_path, "check-projections", extra_env={"PYTHONPATH": str(other)})
    observed = {
        "exit_code": result.returncode,
        "decision": json.loads(result.stdout)["decision"],
    }
    # Assert
    assert observed[facet] == expected, result.stderr


@contextmanager
def _native_context(tmp_path):
    state = tmp_path / "sac-state"
    environment = {
        "SCITEX_AGENT_CONTAINER_HOME": str(state),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(state / "runtime"),
        "SCITEX_DIR": str(tmp_path / "scitex"),
    }
    prior = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        repo = tmp_path / "repo"
        _authority(repo)
        yield repo
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.mark.parametrize(
    "facet, expected",
    [
        ("created_action", "create"),
        ("created_surface", "linked-worktree"),
        ("reused_action", "reuse"),
        ("same_branch", True),
        ("same_path", True),
        ("unfinished_content", "unfinished work\n"),
        ("authority_content", "initial\n"),
    ],
)
def test_native_gate_provisions_then_reuses_owned_worktree(tmp_path, facet, expected):
    # Arrange
    with _native_context(tmp_path) as repo:
        config = AgentConfig(name="native-policy-test", workdir=str(repo))
        successor = AgentConfig(name=config.name, workdir=str(repo))
        # Act
        first = enforce_task_worktree_policy(config)
        tracked = Path(first.resolved_workdir) / "tracked.txt"
        tracked.write_text("unfinished work\n")
        second = enforce_task_worktree_policy(successor)
        observed = {
            "created_action": first.worktree_action,
            "created_surface": first.surface,
            "reused_action": second.worktree_action,
            "same_branch": second.branch == first.branch,
            "same_path": second.resolved_workdir == first.resolved_workdir,
            "unfinished_content": tracked.read_text(),
            "authority_content": (repo / "tracked.txt").read_text(),
        }
        # Assert
        assert observed[facet] == expected


def _refusal(config):
    try:
        enforce_task_worktree_policy(config)
    except WorktreePolicyError as exc:
        return str(exc)
    return ""


@pytest.mark.parametrize(
    "facet, expected",
    [
        ("ownership_refusal", True),
        ("existing_content", "preserve\n"),
        ("owner_created", False),
    ],
)
def test_native_gate_refuses_existing_unowned_target(tmp_path, facet, expected):
    # Arrange
    with _native_context(tmp_path) as repo:
        name = "native-unowned-test"
        preview = enforce_task_worktree_policy(
            AgentConfig(name=name, workdir=str(repo)), provision=False
        )
        target = Path(preview.resolved_workdir)
        target.mkdir(parents=True)
        marker = target / "existing.txt"
        marker.write_text("preserve\n")
        config = AgentConfig(name=name, workdir=str(repo))
        owner = tmp_path / "sac-state" / "runtime" / name / "worktree-owner.json"
        # Act
        refusal = _refusal(config)
        observed = {
            "ownership_refusal": "exists without SAC ownership" in refusal,
            "existing_content": marker.read_text(),
            "owner_created": owner.exists(),
        }
        # Assert
        assert observed[facet] == expected
