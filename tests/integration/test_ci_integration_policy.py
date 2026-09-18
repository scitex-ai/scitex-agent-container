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


def test_pressure_uses_the_largest_downstream_signal(policy) -> None:
    # Arrange
    signals = {"open_green": 12, "open_prs": 30, "p90_minutes": 60}
    # Act
    pressure = policy.integration_pressure(**signals)
    # Assert
    assert pressure == pytest.approx(1.2)


@pytest.mark.parametrize(
    ("pressure", "expected"),
    [
        (0.49, "BUILD"),
        (0.5, "BALANCED"),
        (1.0, "BALANCED"),
        (1.01, "INTEGRATION_HEAVY"),
        (2.0, "INTEGRATION_HEAVY"),
        (2.01, "DRAIN"),
    ],
)
def test_pressure_boundaries_select_capacity_mode(
    policy, pressure: float, expected: str
) -> None:
    # Arrange
    measured = pressure
    # Act
    mode = policy.pressure_mode(measured)
    # Assert
    assert mode == expected


@pytest.mark.parametrize("mode", ["", "unknown", "drain"])
def test_unknown_or_malformed_mode_rejects_dispatch(policy, mode: str) -> None:
    # Arrange
    work_kind = "operator_p0"
    # Act
    allowed = policy.dispatch_allowed(mode, work_kind)
    # Assert
    assert allowed is False


@pytest.mark.parametrize("kind", ["", "feature-ish"])
def test_unknown_work_kind_rejects_dispatch(policy, kind: str) -> None:
    # Arrange
    mode = "BUILD"
    # Act
    allowed = policy.dispatch_allowed(mode, kind)
    # Assert
    assert allowed is False


@pytest.mark.parametrize("kind", ["feature", "routine", "nice_to_have"])
def test_drain_rejects_routine_feature_dispatch(policy, kind: str) -> None:
    # Arrange
    mode = "DRAIN"
    # Act
    allowed = policy.dispatch_allowed(mode, kind)
    # Assert
    assert allowed is False


@pytest.mark.parametrize(
    "kind",
    ["operator_p0", "security", "dependency_unblock", "bug_fix", "integration", "review"],
)
def test_drain_admits_only_explicit_exception_classes(policy, kind: str) -> None:
    # Arrange
    mode = "DRAIN"
    # Act
    allowed = policy.dispatch_allowed(mode, kind)
    # Assert
    assert allowed is True


@pytest.mark.parametrize(
    "kind", ["operator-P0", "dependency-unblock", "bug-fix"]
)
def test_drain_accepts_contract_spelling_for_exception_classes(
    policy, kind: str
) -> None:
    # Arrange
    mode = "DRAIN"
    # Act
    allowed = policy.dispatch_allowed(mode, kind)
    # Assert
    assert allowed is True


def test_drain_rejects_contract_nice_spelling(policy) -> None:
    # Arrange
    mode = "DRAIN"
    # Act
    allowed = policy.dispatch_allowed(mode, "nice")
    # Assert
    assert allowed is False


@pytest.mark.parametrize(
    ("open_green", "expected"),
    [
        (0, "BUILD"),
        (4, "BUILD"),
        (5, "BALANCED"),
        (10, "BALANCED"),
        (11, "INTEGRATION_HEAVY"),
        (20, "INTEGRATION_HEAVY"),
        (21, "DRAIN"),
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
        ".github/workflows/lint.yml",
        ".github/ci/integration_policy.py",
        "policy/review-gate.yaml",
        "policies/fleet-admission.yaml",
        "__global__/fleet-policy.yaml",
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


def test_renamed_control_plane_previous_filename_alias_is_high(policy) -> None:
    # Arrange — workflow feeds both filename and previous_filename into this list.
    changed_paths = ["docs/renamed.md", ".github/workflows/old-dispatch.yml"]
    # Act
    risk = policy.classify_risk(changed_paths)
    # Assert
    assert risk == "HIGH"


def test_cli_reports_drain_for_current_backlog() -> None:
    # Arrange
    command = [sys.executable, str(_POLICY), "mode", "30", "69", "120"]
    # Act
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (0, "DRAIN")


def test_cli_mode_rejects_legacy_single_metric_invocation() -> None:
    # Arrange
    command = [sys.executable, str(_POLICY), "mode", "41"]
    # Act
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    # Assert
    assert completed.returncode != 0


def test_files_pages_must_match_pr_changed_files_count(policy) -> None:
    # Arrange
    pages = [[{"filename": "src/a.py"}], [{"filename": "src/b.py"}]]
    # Act
    paths = policy.changed_paths_from_pages(2, pages)
    # Assert
    assert paths == ["src/a.py", "src/b.py"]


def test_files_api_truncation_fails_closed(policy) -> None:
    # Arrange
    pages = [[{"filename": f"src/{index}.py"} for index in range(3)]]
    # Act
    act = lambda: policy.changed_paths_from_pages(4, pages)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="truncated"):
        act()


def test_search_count_rejects_incomplete_results(policy) -> None:
    # Arrange
    payload = {"total_count": 3, "incomplete_results": True}
    # Act
    act = lambda: policy.complete_search_count(payload)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="incomplete"):
        act()


def test_check_counts_are_derived_from_one_snapshot(policy) -> None:
    # Arrange
    snapshot = {
        "total_count": 4,
        "check_runs": [
            {"name": "ok", "status": "completed", "conclusion": "success"},
            {"name": "busy", "status": "in_progress", "conclusion": None},
            {"name": "bad", "status": "completed", "conclusion": "failure"},
            {"name": "codecov", "status": "completed", "conclusion": "failure"},
        ]
    }
    # Act
    counts = policy.check_counts(snapshot)
    # Assert
    assert counts == (1, 1, 3)


def test_check_counts_reject_truncated_snapshot(policy) -> None:
    # Arrange
    snapshot = {"total_count": 2, "check_runs": [{"name": "only-one"}]}
    # Act
    act = lambda: policy.check_counts(snapshot)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="truncated"):
        act()


@pytest.mark.parametrize(
    "conclusion",
    ["cancelled", "stale", "startup_failure", None, "future_verdict"],
)
def test_check_counts_fail_closed_on_nonaccepted_completed_conclusion(
    policy, conclusion: str | None
) -> None:
    # Arrange
    snapshot = {
        "total_count": 1,
        "check_runs": [
            {"name": "develop-gate", "status": "completed", "conclusion": conclusion}
        ],
    }
    # Act
    counts = policy.check_counts(snapshot)
    # Assert
    assert counts == (1, 0, 1)


def test_rollup_counts_revalidate_every_non_advisory_check(policy) -> None:
    # Arrange
    payload = {
        "statusCheckRollup": [
            {"name": "ok", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "neutral", "status": "COMPLETED", "conclusion": "NEUTRAL"},
            {"name": "bad", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"name": "busy", "status": "IN_PROGRESS", "conclusion": "SUCCESS"},
            {"name": "codecov", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
    }
    # Act
    counts = policy.rollup_counts(payload)
    # Assert
    assert counts == (2, 1, 1, 4)


def test_rollup_counts_treat_unknown_verdict_as_not_yet_known(policy) -> None:
    # Arrange
    payload = {
        "statusCheckRollup": [
            {"name": "future", "status": "COMPLETED", "conclusion": "WORMHOLE"}
        ]
    }
    # Act
    counts = policy.rollup_counts(payload)
    # Assert
    assert counts == (0, 0, 1, 1)


def test_rollup_counts_reject_head_mismatch(policy) -> None:
    # Arrange
    payload = {
        "headRefOid": "other-head",
        "statusCheckRollup": [
            {"name": "ok", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    # Act
    act = lambda: policy.rollup_counts(payload, expected_head="expected-head")  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="head"):
        act()


def test_protection_requires_strict_and_every_required_context(policy) -> None:
    # Arrange
    payload = {
        "strict": True,
        "contexts": ["required-a"],
        "checks": [{"context": "required-b", "app_id": 1}],
    }
    # Act
    missing = policy.missing_protected_contexts(
        payload, ("required-a", "required-b", "required-c")
    )
    # Assert
    assert missing == ["required-c"]


def test_protection_rejects_non_strict_configuration(policy) -> None:
    # Arrange
    payload = {"strict": False, "contexts": ["required-a"]}
    # Act
    act = lambda: policy.missing_protected_contexts(payload, ("required-a",))  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="strict"):
        act()


def test_required_contexts_demand_success_not_skipped_or_neutral(policy) -> None:
    # Arrange
    payload = {
        "statusCheckRollup": [
            {"name": "required-a", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"name": "required-b", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        ]
    }
    # Act
    missing = policy.missing_required_contexts(payload, ("required-a", "required-b"))
    # Assert
    assert missing == ["required-a", "required-b"]


def test_required_contexts_accept_only_exact_success(policy) -> None:
    # Arrange
    payload = {
        "statusCheckRollup": [
            {"name": "required-a", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "required-b", "state": "SUCCESS"},
        ]
    }
    # Act
    missing = policy.missing_required_contexts(payload, ("required-a", "required-b"))
    # Assert
    assert missing == []


@pytest.mark.parametrize("other_verdict", ["SKIPPED", "NEUTRAL", "FAILURE"])
def test_required_contexts_reject_duplicate_same_name_verdicts(
    policy, other_verdict: str
) -> None:
    # Arrange — GitHub may retain a stale same-name check run beside its rerun.
    payload = {
        "statusCheckRollup": [
            {
                "name": "ci-verdict-to-pushing-agent",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "name": "ci-verdict-to-pushing-agent",
                "status": "COMPLETED",
                "conclusion": other_verdict,
            },
        ]
    }
    # Act
    missing = policy.missing_required_contexts(
        payload, ("ci-verdict-to-pushing-agent",)
    )
    # Assert — one stale success cannot launder a second current/ambiguous row.
    assert missing == ["ci-verdict-to-pushing-agent"]


def test_cli_rejects_feature_dispatch_under_drain_pressure() -> None:
    # Arrange
    command = [
        sys.executable,
        str(_POLICY),
        "admit",
        "--open-green",
        "30",
        "--open-prs",
        "69",
        "--p90-minutes",
        "120",
        "--work-kind",
        "feature",
    ]
    # Act
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (2, "REJECT DRAIN")


def test_cli_fails_closed_when_pressure_input_is_unreadable() -> None:
    # Arrange
    command = [
        sys.executable,
        str(_POLICY),
        "admit",
        "--open-green",
        "unreadable",
        "--open-prs",
        "69",
        "--p90-minutes",
        "120",
        "--work-kind",
        "operator_p0",
    ]
    # Act
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    # Assert
    assert (completed.returncode, completed.stdout.strip()) == (2, "REJECT DRAIN")


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
    mode_call = script[script.index("integration_policy.py mode") :][:180]
    # Act
    contract = (
        "integration_policy.py mode" in script,
        "FLEET MODE" in script,
        "mode=\"DRAIN\"" in script,
        "open_prs" in script,
        "GREEN_TO_MERGE_P90_MINUTES" in str(merge_step.get("env", {})),
        all(name in mode_call for name in ("$open_green", "$open_prs", "$p90_minutes")),
    )
    # Assert
    assert contract == (True, True, True, True, True, True)


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


def test_merge_sweep_detects_changed_files_api_truncation(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        "changedFiles" in script,
        "integration_policy.py paths" in script,
        "changed-files API was truncated" in script,
    )
    # Assert
    assert contract == (True, True, True)


def test_develop_health_reads_only_exact_sha_snapshots(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        script.count('commits/$dev_sha/check-runs?per_page=100') == 2,
        "integration_policy.py check-counts" in script,
    )
    # Assert
    assert contract == (True, True)


def test_auto_merge_pressure_rejects_incomplete_search(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    contract = (
        "integration_policy.py search-count" in script,
        "incomplete_results" in _POLICY.read_text(encoding="utf-8"),
    )
    # Assert
    assert contract == (True, True)


def test_dry_run_counts_would_merge_not_actual_merge(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    dry_branch = script[
        script.index('if [ "$DRY_RUN" = "true" ]') : script.index("ATTRIBUTION FIRST")
    ]
    # Act
    contract = (
        "would_merge=$((would_merge + 1))" in dry_branch,
        "merges=$((merges + 1))" not in dry_branch,
        "would-merge" in script,
    )
    # Assert
    assert contract == (True, True, True)


def test_missing_required_context_blocks_before_attribution(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    required_at = script.index("REQUIRED_CONTEXTS")
    attrib_at = script.index("attribution=$(printf")
    # Act
    gate = script[required_at:attrib_at]
    # Assert
    assert (
        "required context" in gate,
        "continue" in gate,
        required_at < attrib_at,
    ) == (True, True, True)


def test_required_contexts_are_reread_before_attribution_and_merge(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    attribution_at = script.index("ATTRIBUTION FIRST")
    merge_at = script.index("gh pr merge")
    # Act
    calls = [
        index
        for index in range(len(script))
        if script.startswith("integration_policy.py required-contexts", index)
    ]
    # Assert
    assert (
        any(index < attribution_at for index in calls),
        any(attribution_at < index < merge_at for index in calls),
    ) == (True, True)


def test_dry_run_budget_matches_one_real_merge_per_tick(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    # Act
    budget = script[script.index("for pr in $prs") : script.index("RISK LANE")]
    dispatch_tail = script[script.index("github.token pushes fire no workflows") :]
    # Assert
    assert (
        "merges + would_merge" in budget,
        '"$would_merge" -gt 0' in dispatch_tail,
        "DRY RUN: would dispatch post-merge gates" in dispatch_tail,
    ) == (True, True, True)


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


def test_candidate_is_fully_revalidated_before_and_after_attribution(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    helper = script[
        script.index("revalidate_candidate()") : script.index(
            'if ! revalidate_candidate "before attribution"'
        )
    ]
    # Act
    contract = (
        "isDraft" in helper and "labels" in helper,
        "--json headRefOid,statusCheckRollup" in helper
        and '--expected-head "$pr_sha"' in helper,
        'commits/$dev_sha/check-runs' in helper and "check-counts" in helper,
        'pulls/$pr/reviews' in helper and 'review "$pr_sha"' in helper,
        script.count('revalidate_candidate "before attribution"') == 1,
        script.count('revalidate_candidate "after attribution"') == 1,
    )
    # Assert
    assert contract == (True, True, True, True, True, True)


def test_main_retarget_race_is_blocked_by_server_side_actor_restriction(
    merge_step: dict,
) -> None:
    # Arrange
    script = merge_step["run"]
    candidate_loop_at = script.index("for pr in $prs")
    # Act
    gate = script[:candidate_loop_at]
    contract = (
        "branches/main/protection/restrictions/apps" in gate,
        "github-actions" in gate,
        "refusing automatic merges" in gate,
    )
    # Assert
    assert contract == (True, True, True)


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
    assert (
        strict_at < merge_at,
        "integration_policy.py protection" in script,
        "$REQUIRED_CONTEXTS" in script[strict_at:merge_at],
        'GH_TOKEN="$PROTECTION_TOKEN" gh api' in script,
        "SAC_BRANCH_PROTECTION_READ_TOKEN is absent" in script,
    ) == (
        True,
        True,
        True,
        True,
        True,
    )


def test_candidate_api_failures_make_the_sweep_red(merge_step: dict) -> None:
    # Arrange
    script = merge_step["run"]
    messages = (
        "could not resolve pull requests for check-suite head",
        "could not list open develop pull requests",
    )
    # Act
    guarded = tuple(
        "::error::" in script[script.index(message) - 20 : script.index(message) + 180]
        and "exit 1" in script[script.index(message) : script.index(message) + 220]
        for message in messages
    )
    # Assert
    assert guarded == (True, True)


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
