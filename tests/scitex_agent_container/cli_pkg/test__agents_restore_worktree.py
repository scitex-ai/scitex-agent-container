"""CLI contract for the explicit, default-dry-run owned restore verb."""

import importlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.agent_group import agent_group
from scitex_agent_container.config import AgentConfig


@pytest.mark.parametrize(
    "flag",
    ["--expected-tip", "--receipt-sha256", "--apply", "--yes", "-y", "--dry-run"],
)
def test_restore_verb_exposes_exact_commit_and_reviewed_receipt(flag):
    # Arrange
    # Act
    result = CliRunner().invoke(agent_group, ["restore-worktree", "--help"])
    # Assert
    assert flag in result.output


@pytest.mark.parametrize("approval", ["--apply", "--yes", "-y"])
def test_apply_missing_receipt_is_refused_before_loading_identity(tmp_path, approval):
    # Arrange
    # A nonexistent explicit path would fail resolution if the guard ran late.
    missing = str(tmp_path / "absent/spec.yaml")
    # Act
    result = CliRunner().invoke(
        agent_group,
        ["restore-worktree", missing, "--expected-tip", "1" * 40, approval],
    )
    # Assert
    assert (result.exit_code, "--receipt-sha256" in result.output) == (1, True)


@pytest.fixture
def private_registry(tmp_path):
    path = tmp_path / "state/agent-container/agents/scitex-scholar/spec.yaml"
    path.parent.mkdir(parents=True)
    reviewed = Path(__file__).parents[1] / "runtimes/_fixtures/scitex-scholar/spec.yaml"
    path.write_bytes(reviewed.read_bytes())
    saved = {key: os.environ.get(key) for key in ("SCITEX_DIR", "SAC_AGENT_SCOPE")}
    os.environ.update(SCITEX_DIR=str(tmp_path / "state"), SAC_AGENT_SCOPE="user")
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_restore_rejects_prefix_alias_before_reaching_policy_or_store(private_registry):
    # Arrange
    prefix = "scitex-scho"
    # Act
    result = CliRunner().invoke(
        agent_group, ["restore-worktree", prefix, "--expected-tip", "1" * 40]
    )
    # Assert
    assert (result.exit_code, "exact canonical agent name" in result.output) == (
        1,
        True,
    ), result.output


@pytest.fixture
def restore_recorder():
    module = importlib.import_module(
        "scitex_agent_container.cli_pkg._agents_restore_worktree"
    )
    calls = []
    originals = {
        name: getattr(module, name)
        for name in (
            "resolve_with_prefix",
            "load_config",
            "restore_owned_task_worktree",
        )
    }

    def resolve(name):
        calls.append(("resolve", name))
        return name

    def load(name):
        calls.append(("load", name))
        return AgentConfig(name=name, runtime="tui")

    def restore(config, **kwargs):
        calls.append(("restore", config.name, kwargs))
        return {"name": config.name, **kwargs}

    module.resolve_with_prefix = resolve
    module.load_config = load
    module.restore_owned_task_worktree = restore
    try:
        yield calls
    finally:
        for name, original in originals.items():
            setattr(module, name, original)


@pytest.mark.parametrize("approval", ["--apply", "--yes", "-y"])
def test_restore_conflicting_flags_refuse_before_identity(restore_recorder, approval):
    # Arrange
    args = [
        "restore-worktree",
        "worker",
        "--expected-tip",
        "1" * 40,
        "--dry-run",
        approval,
        "--receipt-sha256",
        "2" * 64,
    ]
    # Act
    result = CliRunner().invoke(agent_group, args)
    # Assert
    assert (
        result.exit_code,
        "cannot be combined" in result.output,
        restore_recorder,
    ) == (2, True, [])


@pytest.mark.parametrize(
    "flags, applied",
    [
        ([], False),
        (["--dry-run"], False),
        (["--apply"], True),
        (["--yes"], True),
        (["-y"], True),
    ],
)
def test_restore_forwards_approval_and_receipt(restore_recorder, flags, applied):
    # Arrange
    args = [
        "restore-worktree",
        "worker",
        "--expected-tip",
        "1" * 40,
        "--receipt-sha256",
        "2" * 64,
        *flags,
    ]
    # Act
    result = CliRunner().invoke(agent_group, args)
    # Assert
    assert (result.exit_code, json.loads(result.output), restore_recorder) == (
        0,
        {
            "name": "worker",
            "expected_tip": "1" * 40,
            "apply": applied,
            "receipt_sha256": "2" * 64,
        },
        [
            ("resolve", "worker"),
            ("load", "worker"),
            (
                "restore",
                "worker",
                {
                    "expected_tip": "1" * 40,
                    "apply": applied,
                    "receipt_sha256": "2" * 64,
                },
            ),
        ],
    )
