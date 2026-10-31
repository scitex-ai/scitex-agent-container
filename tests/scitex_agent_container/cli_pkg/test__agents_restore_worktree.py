"""CLI contract for the explicit, default-dry-run owned restore verb."""

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.agent_group import agent_group


@pytest.mark.parametrize("flag", ["--expected-tip", "--receipt-sha256", "--apply"])
def test_restore_verb_exposes_exact_commit_and_reviewed_receipt(flag):
    # Arrange
    # Act
    result = CliRunner().invoke(agent_group, ["restore-worktree", "--help"])
    # Assert
    assert flag in result.output


def test_apply_missing_receipt_is_refused_before_loading_identity(tmp_path):
    # Arrange
    # A nonexistent explicit path would fail resolution if the guard ran late.
    missing = str(tmp_path / "absent/spec.yaml")
    # Act
    result = CliRunner().invoke(
        agent_group,
        ["restore-worktree", missing, "--expected-tip", "1" * 40, "--apply"],
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
