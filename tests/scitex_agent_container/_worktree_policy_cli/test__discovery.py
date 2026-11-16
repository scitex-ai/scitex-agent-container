"""Agent discovery and native policy decisions share the same operations."""

import hashlib
import json
import subprocess

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container._worktree_policy_cli import PACKAGED_POLICY
from scitex_agent_container.cli import main


def test_policy_discovery_lists_the_real_operations():
    # Arrange
    policy = main.commands["worktree"].commands["policy"]
    # Act
    info = policy.to_info_dict(click.Context(policy))
    # Assert
    assert set(info.get("commands", {})) == {
        "inspect",
        "assert-context",
        "check-edit",
        "check-shell",
        "generate-projections",
        "check-projections",
    }


def test_discovery_exposes_required_intent_and_generation_approval():
    # Arrange
    policy = main.commands["worktree"].commands["policy"]
    # Act
    context = policy.commands["assert-context"]
    generation = policy.commands["generate-projections"]
    # Assert
    assert (
        [(p.name, p.required) for p in context.params],
        context.params[1].type.choices,
        {p.name for p in generation.params},
    ) == (
        [("repo", True), ("intent", True)],
        ("read", "edit", "commit", "push", "branch-change"),
        {"output_dir", "dry_run", "yes"},
    )


def test_generation_preview_preserves_the_native_no_read_contract(tmp_path):
    # Arrange
    missing = tmp_path / "missing.json"
    output = tmp_path / "generated"
    # Act
    result = CliRunner().invoke(
        main,
        [
            "worktree",
            "policy",
            "--policy",
            str(missing),
            "generate-projections",
            "--output-dir",
            str(output),
            "--dry-run",
        ],
    )
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        payload["decision"],
        payload["scope"],
        list(tmp_path.iterdir()),
    ) == (0, "preview", "arguments_only", [])


def test_generation_refuses_unapproved_writes_before_reading_policy(tmp_path):
    # Arrange
    missing = tmp_path / "missing.json"
    # Act
    result = CliRunner().invoke(
        main,
        [
            "worktree",
            "policy",
            "--policy",
            str(missing),
            "generate-projections",
        ],
    )
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        payload["decision"],
        "--yes" in payload["error"],
        list(tmp_path.iterdir()),
    ) == (3, "error", True, [])


def test_unknown_policy_arguments_keep_typed_json_and_exit_three():
    # Arrange
    args = ["worktree", "policy", "inspect", "--not-a-policy-option"]
    # Act
    result = CliRunner().invoke(main, args)
    payload = json.loads(result.stdout)
    # Assert
    assert (result.exit_code, payload["allowed"], payload["decision"]) == (
        3,
        False,
        "error",
    )


@pytest.fixture
def policy_repository(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "develop", str(repo)], check=True)
    (repo / "tracked.txt").write_text("preserve this content\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Policy Fixture",
            "-c",
            "user.email=policy@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Initial owned fixture",
        ],
        check=True,
    )
    policy = tmp_path / "policy.json"
    policy.write_bytes(PACKAGED_POLICY.read_bytes())
    tip = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    return repo, policy, tip


def test_policy_inspection_preserves_actual_git_state(policy_repository):
    # Arrange
    repo, policy, tip = policy_repository
    # Act
    result = CliRunner().invoke(
        main,
        [
            "worktree",
            "policy",
            "--policy",
            str(policy),
            "inspect",
            "--repo",
            str(repo),
        ],
    )
    payload = json.loads(result.stdout)
    current_tip = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    # Assert
    assert (
        result.exit_code,
        payload["allowed"],
        payload["branch"],
        payload["policy_sha256"],
        current_tip,
        (repo / "tracked.txt").read_text(),
        (policy.parent / "generated").exists(),
    ) == (
        0,
        True,
        "develop",
        hashlib.sha256(policy.read_bytes()).hexdigest(),
        tip,
        "preserve this content\n",
        False,
    )


def test_discovered_operation_executes_the_native_preview(tmp_path):
    # Arrange
    policy = main.commands["worktree"].commands["policy"]
    operation = policy.commands["generate-projections"]
    # Act
    result = CliRunner().invoke(operation, ["--output-dir", str(tmp_path), "--dry-run"])
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        payload["decision"],
        payload["scope"],
        list(tmp_path.iterdir()),
    ) == (0, "preview", "arguments_only", [])
