"""Deterministic integration policy for the green-PR drain.

The policy is repository-owned and pure: GitHub remains the source of truth for
backlog, changed paths, reviews, and SHAs; this module only maps those observed
facts to a fleet mode and merge lane.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_POLICY = _REPO / ".github" / "ci" / "integration_policy.py"
_WORKFLOW = _REPO / ".github" / "workflows" / "auto-merge-to-develop.yaml"


@pytest.fixture(scope="module")
def policy():
    spec = importlib.util.spec_from_file_location("integration_policy", _POLICY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def merge_step() -> dict:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["automerge"]["steps"]
    matches = [step for step in steps if "gh pr merge" in step.get("run", "")]
    assert len(matches) == 1
    return matches[0]


def test_backlog_above_forty_enters_drain(policy) -> None:
    # Arrange
    open_green = 41
    # Act
    mode = policy.fleet_mode(open_green)
    # Assert
    assert mode == "DRAIN"


@pytest.mark.parametrize(
    ("open_green", "expected"),
    [
        (0, "BUILD"),
        (10, "BUILD"),
        (11, "BALANCED"),
        (20, "BALANCED"),
        (21, "INTEGRATION_HEAVY"),
        (40, "INTEGRATION_HEAVY"),
    ],
)
def test_backlog_boundaries_select_the_declared_mode(
    policy, open_green: int, expected: str
) -> None:
    # Arrange — parametrized at every threshold edge.
    backlog = open_green
    # Act
    mode = policy.fleet_mode(backlog)
    # Assert
    assert mode == expected


@pytest.mark.parametrize(
    "path",
    [
        "src/project/auth/session.py",
        "src/project/authn/session.py",
        "src/project/oauth/token.py",
        "src/project/oauth2/client.py",
        "src/project/authorization/policy.py",
        "src/project/authorize.py",
        "src/project/iam/policy.py",
        "src/project/rbac/policy.py",
        "src/scitex_agent_container/_authheal/_detect.py",
        "src/project/login/session.py",
        "src/project/secrets/loader.py",
        "src/project/billing/invoice.py",
        "src/project/payments/invoice.py",
        "src/project/tenancy/lease.py",
        "src/project/credentials/store.py",
        "src/scitex_agent_container/_account/creds_sync.py",
        "db/schema.sql",
        "migrations/0042_add_owner.sql",
        "scripts/migrate_containers_layout.sh",
        ".github/workflows/deploy-production.yml",
        "src/scitex_agent_container/runtimes/_to_home_deployers.py",
        ".github/workflows/release-production.yml",
    ],
)
def test_sensitive_paths_are_high_risk(policy, path: str) -> None:
    # Arrange
    changed_paths = [path]
    # Act
    risk = policy.classify_risk(changed_paths)
    # Assert
    assert risk == "HIGH"


@pytest.mark.parametrize(
    "changed_paths",
    [
        ["docs/integration.md"],
        ["README.md", "tests/integration/test_policy.py"],
        [".github/ci/ci-status.py", ".github/workflows/lint.yml"],
    ],
)
def test_documentation_tests_and_ci_only_changes_are_low_risk(
    policy, changed_paths: list[str]
) -> None:
    # Arrange — the paths are GitHub's complete changed-file set.
    paths = changed_paths
    # Act
    risk = policy.classify_risk(paths)
    # Assert
    assert risk == "LOW"


def test_source_change_defaults_to_normal_risk(policy) -> None:
    # Arrange
    changed_paths = ["src/project/widget.py"]
    # Act
    risk = policy.classify_risk(changed_paths)
    # Assert
    assert risk == "NORMAL"


def test_unreadable_empty_file_set_fails_safe_to_high_risk(policy) -> None:
    # Arrange
    changed_paths: list[str] = []
    # Act
    risk = policy.classify_risk(changed_paths)
    # Assert
    assert risk == "HIGH"


def test_one_sensitive_path_makes_a_mixed_change_high_risk(policy) -> None:
    # Arrange
    changed_paths = ["docs/billing.md", "src/project/billing/invoice.py"]
    # Act
    risk = policy.classify_risk(changed_paths)
    # Assert
    assert risk == "HIGH"


def test_cli_reports_drain_for_current_backlog() -> None:
    # Arrange
    command = [sys.executable, str(_POLICY), "mode", "61"]
    # Act
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (0, "DRAIN")


def test_cli_reads_complete_changed_path_set_from_stdin() -> None:
    # Arrange
    command = [sys.executable, str(_POLICY), "risk"]
    changed_paths = "docs/guide.md\nsrc/project/credentials/store.py\n"
    # Act
    completed = subprocess.run(
        command,
        input=changed_paths,
        capture_output=True,
        text=True,
        check=False,
    )
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (0, "HIGH")


def test_merge_sweep_measures_backlog_with_repository_policy(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        "integration_policy.py mode" in script,
        "FLEET MODE" in script,
        "mode=\"DRAIN\"" in script,
    )
    # Assert
    assert contract == (True, True, True)


def test_merge_sweep_holds_high_risk_changed_paths(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        'pulls/$pr/files' in script,
        "integration_policy.py risk" in script,
        '"$risk" = "HIGH"' in script,
        "HIGH risk" in script,
    )
    # Assert
    assert contract == (True, True, True, True)


def test_merge_sweep_updates_to_latest_develop_and_binds_the_head_sha(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        "headRefOid" in script,
        'compare/$dev_sha...$pr_sha' in script,
        "update-branch" in script,
        '--match-head-commit "$pr_sha"' in script,
    )
    # Assert
    assert contract == (True, True, True, True)


def test_normal_lane_requires_formal_approval_on_exact_head(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        '"$risk" = "NORMAL"' in script,
        'pulls/$pr/reviews' in script,
        "--paginate --slurp" in script,
        'integration_policy.py review "$pr_sha"' in script,
        '"$review_verdict" = "APPROVED"' in script,
    )
    # Assert
    assert contract == (True, True, True, True, True)


@pytest.mark.parametrize(
    ("reviews", "expected"),
    [
        ([{"state": "APPROVED", "commit_id": "head"}], "APPROVED"),
        ([{"state": "APPROVED", "commit_id": "stale"}], "WAITING"),
        ([{"state": "CHANGES_REQUESTED", "commit_id": "head"}], "CHANGES_REQUESTED"),
        (
            [
                {"state": "APPROVED", "commit_id": "head"},
                {"state": "CHANGES_REQUESTED", "commit_id": "head"},
            ],
            "CHANGES_REQUESTED",
        ),
    ],
)
def test_formal_review_verdict_is_bound_to_exact_head(
    policy, reviews: list[dict], expected: str
) -> None:
    # Arrange
    head_sha = "head"
    # Act
    verdict = policy.review_verdict(head_sha, reviews)
    # Assert
    assert verdict == expected


def test_later_approval_supersedes_same_reviewers_change_request(policy) -> None:
    # Arrange
    reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "commit_id": "head",
            "user": {"login": "reviewer"},
        },
        {
            "id": 2,
            "state": "APPROVED",
            "commit_id": "head",
            "user": {"login": "reviewer"},
        },
    ]
    # Act
    verdict = policy.review_verdict("head", reviews)
    # Assert
    assert verdict == "APPROVED"


def test_later_comment_does_not_erase_exact_head_approval(policy) -> None:
    # Arrange
    reviews = [
        {
            "id": 1,
            "state": "APPROVED",
            "commit_id": "head",
            "user": {"login": "reviewer"},
        },
        {
            "id": 2,
            "state": "COMMENTED",
            "commit_id": "head",
            "user": {"login": "reviewer"},
        },
    ]
    # Act
    verdict = policy.review_verdict("head", reviews)
    # Assert
    assert verdict == "APPROVED"


def test_review_cli_flattens_paginated_github_json() -> None:
    # Arrange
    command = [sys.executable, str(_POLICY), "review", "head"]
    pages = '[[{"state":"APPROVED","commit_id":"stale"}],'
    pages += '[{"state":"APPROVED","commit_id":"head"}]]'
    # Act
    completed = subprocess.run(
        command,
        input=pages,
        capture_output=True,
        text=True,
        check=False,
    )
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (0, "APPROVED")


def test_merge_attribution_records_the_deterministic_risk_lane(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    attribution = script[script.index("attribution=$(printf") : script.index("prior=")]
    # Assert
    assert r"deterministic risk lane = \`$risk\`" in attribution


def test_dry_run_never_calls_update_branch(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    stale_branch = script[script.index('if [ "$merge_base" != "$dev_sha" ]') :]
    update_at = stale_branch.index("update-branch")
    # Act
    guard = stale_branch[:update_at]
    # Assert
    assert ('"$DRY_RUN" = "true"' in guard, "continue" in guard) == (True, True)


def test_last_inch_rechecks_base_head_and_develop_after_attribution(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    after_comment = script[script.index("gh pr comment") :]
    merge_at = after_comment.index("gh pr merge")
    gate = after_comment[:merge_at]
    # Act
    contract = (
        "baseRefName,headRefOid" in gate,
        '"$last_base" != "develop"' in gate,
        '"$last_head" != "$pr_sha"' in gate,
        '"$last_dev" != "$dev_sha"' in gate,
    )
    # Assert
    assert contract == (True, True, True, True)


def test_risk_classification_is_bracketed_by_exact_head_reads(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    files_at = script.index('pulls/$pr/files')
    before = script[:files_at]
    after = script[files_at:]
    # Act
    contract = (
        before.rfind("headRefOid") > before.rfind("RISK LANE"),
        after.index("risk_head=") < after.index("LATEST-DEVELOP"),
        '"$risk_head" != "$pr_sha"' in after,
        ".previous_filename" in after,
    )
    # Assert
    assert contract == (True, True, True, True)


def test_server_side_strict_base_protection_is_required(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    strict_at = script.index("protection/required_status_checks")
    merge_at = script.index("gh pr merge")
    # Assert
    assert (strict_at < merge_at, '"$strict_base" != "true"' in script) == (
        True,
        True,
    )


def test_attribution_is_exact_head_and_workflow_identity_scoped(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        'attrib_marker="<!-- $ATTRIB_MARKER:$pr_sha -->"' in script,
        '== "github-actions"' in script,
        '== "github-actions[bot]"' in script,
        "Automation is attempting this merge now" in script,
        "No human read this diff" not in script,
        "Why it merged" not in script,
    )
    # Assert
    assert contract == (True, True, True, True, True, True)
