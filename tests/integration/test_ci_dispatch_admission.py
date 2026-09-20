"""Executable, fail-closed fleet dispatch admission workflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "fleet-dispatch-admission.yaml"
_DISPATCHER = _REPO / ".github" / "workflows" / "fleet-work-dispatch.yaml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def admission_step(workflow: dict) -> dict:
    steps = workflow["jobs"]["admission"]["steps"]
    matches = [step for step in steps if "integration_policy.py admit" in step.get("run", "")]
    assert len(matches) == 1
    return matches[0]


def test_workflow_is_an_executable_dispatch_gate() -> None:
    # Arrange
    source = _WORKFLOW.read_text(encoding="utf-8")
    # Act
    triggers = (
        "workflow_call:" in source,
        "workflow_dispatch:" in source,
        "repository_dispatch:" in source,
        "fleet-dispatch-admission" in source,
    )
    # Assert
    assert triggers == (True, True, True, True)


def test_inventory_measurement_requires_dedicated_read_token(workflow: dict) -> None:
    # Arrange
    steps = workflow["jobs"]["admission"]["steps"]
    script = "\n".join(step.get("run", "") for step in steps)
    source = _WORKFLOW.read_text(encoding="utf-8")
    # Act
    contract = (
        "SAC_FLEET_INVENTORY_READ_TOKEN" in source,
        "FLEET_READ_TOKEN is absent" in script,
        "issuesAndPullRequests" in source,
        "status:success" in source,
        "incomplete_results" in source,
        "GREEN_TO_MERGE_P90_MINUTES" in source,
    )
    # Assert
    assert contract == (True, True, True, True, True, True)


def test_reviewed_policy_is_checked_out_before_it_executes(workflow: dict) -> None:
    # Arrange
    steps = workflow["jobs"]["admission"]["steps"]
    # Act
    checkout_at = next(i for i, step in enumerate(steps) if step.get("uses") == "actions/checkout@v4")
    policy_at = next(i for i, step in enumerate(steps) if "integration_policy.py admit" in step.get("run", ""))
    # Assert
    assert checkout_at < policy_at


def test_repository_dispatcher_consumes_admission_before_side_effect() -> None:
    # Arrange
    source = _DISPATCHER.read_text(encoding="utf-8")
    doc = yaml.safe_load(source)
    jobs = doc["jobs"]
    # Act
    contract = (
        jobs["admission"]["uses"] == "./.github/workflows/fleet-dispatch-admission.yaml",
        jobs["dispatch"]["needs"] == "admission",
        "SAC_FLEET_DISPATCH_TOKEN" in source,
        "SAC_FLEET_DISPATCH_REPO" in source,
        'POST /repos/{owner}/{repo}/dispatches' in source,
        "fleet-work-admitted" in source,
    )
    # Assert
    assert contract == (True, True, True, True, True, True)


def _run_admission(admission_step: dict, **overrides: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "OPEN_GREEN": "30",
        "OPEN_PRS": "69",
        "P90_MINUTES": "120",
        "WORK_KIND": "feature",
        **overrides,
    }
    return subprocess.run(
        ["bash", "-c", admission_step["run"]],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_shipped_shell_rejects_feature_dispatch_in_drain(admission_step: dict) -> None:
    # Arrange
    # Act
    completed = _run_admission(admission_step)
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (2, "REJECT DRAIN")


@pytest.mark.parametrize(
    ("name", "value"),
    [("OPEN_GREEN", ""), ("OPEN_PRS", "unreadable"), ("P90_MINUTES", "nan")],
)
def test_shipped_shell_fails_closed_on_unreadable_pressure(
    admission_step: dict, name: str, value: str
) -> None:
    # Arrange
    # Act
    completed = _run_admission(admission_step, **{name: value, "WORK_KIND": "operator_p0"})
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (2, "REJECT DRAIN")


def test_shipped_shell_admits_explicit_security_exception(admission_step: dict) -> None:
    # Arrange
    # Act
    completed = _run_admission(admission_step, WORK_KIND="security")
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (0, "ALLOW DRAIN")


def test_admission_shell_is_strict_and_does_not_mask_policy_failure(
    admission_step: dict,
) -> None:
    # Arrange
    script = admission_step["run"]
    # Act
    contract = (
        "set -euo pipefail" in script,
        "|| true" not in script,
        "integration_policy.py admit" in script,
    )
    # Assert
    assert contract == (True, True, True)
