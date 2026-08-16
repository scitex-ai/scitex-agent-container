"""Tests for the GitHub-CI poll wrapper (sac #404).

feedback.pdf §3: sac polls GitHub CI on its own schedule. This module
wraps the two ``gh`` reads the poller needs — a PR's CI conclusion and
its head sha — behind a ``run`` injection seam so tests drive canned
``gh`` output without a network call (DI seam, not a mock; same pattern
as ``periodic_drive_loop``'s ``agents_source`` / ``now_fn``).

Conventions: one assertion per test (STX-TQ007); AAA markers; no mocks /
monkeypatch (STX-NM) — the ``run`` callable is dependency-injected.
"""

from __future__ import annotations

import json

from scitex_agent_container._lifecycle._github_ci import (
    gh_ready,
    list_open_prs,
    pr_ci_conclusion,
    pr_head_sha,
)


def test_all_pass_buckets_yield_success_conclusion():
    # Arrange
    out = json.dumps([{"bucket": "pass"}, {"bucket": "skipping"}])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=lambda args: out)
    # Assert
    assert conclusion == "success"


def test_any_fail_bucket_yields_failure_conclusion():
    # Arrange
    out = json.dumps([{"bucket": "pass"}, {"bucket": "fail"}])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=lambda args: out)
    # Assert
    assert conclusion == "failure"


def test_cancel_bucket_yields_failure_conclusion():
    # Arrange
    out = json.dumps([{"bucket": "pass"}, {"bucket": "cancel"}])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=lambda args: out)
    # Assert
    assert conclusion == "failure"


def test_pending_without_fail_yields_pending_conclusion():
    # Arrange
    out = json.dumps([{"bucket": "pass"}, {"bucket": "pending"}])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=lambda args: out)
    # Assert
    assert conclusion == "pending"


def test_empty_checks_list_yields_none_conclusion():
    # Arrange
    out = json.dumps([])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=lambda args: out)
    # Assert
    assert conclusion == "none"


def test_malformed_json_yields_none_conclusion():
    # Arrange — gh emitted garbage / nothing; the poller must not crash.
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=lambda args: "not json{")
    # Assert
    assert conclusion == "none"


def test_head_sha_returns_stripped_sha():
    # Arrange — `gh api ... --jq .head.sha` prints the sha + trailing nl.
    # Act
    sha = pr_head_sha("o/r", 1, run=lambda args: "c3211e1f0d71\n")
    # Assert
    assert sha == "c3211e1f0d71"


def test_list_open_prs_parses_number_sha_and_body():
    # Arrange
    out = json.dumps([{"number": 7, "headRefOid": "abc123", "body": "Owner: proj-x"}])
    # Act
    prs = list_open_prs("o/r", run=lambda args: out)
    # Assert
    assert prs == [{"number": 7, "head_sha": "abc123", "body": "Owner: proj-x"}]


def test_list_open_prs_empty_output_yields_empty_list():
    # Arrange — gh emitted nothing (no open PRs / transient blip).
    empty = ""
    # Act
    prs = list_open_prs("o/r", run=lambda args: empty)
    # Assert
    assert prs == []


def test_gh_ready_true_when_probe_reports_authenticated():
    # Arrange — probe seam stands in for `gh auth status` succeeding.
    probe = lambda: True  # noqa: E731
    # Act
    ready = gh_ready(probe=probe)
    # Assert
    assert ready is True


def test_gh_ready_false_when_probe_reports_unauthenticated():
    # Arrange — probe seam stands in for `gh auth status` failing.
    probe = lambda: False  # noqa: E731
    # Act
    ready = gh_ready(probe=probe)
    # Assert
    assert ready is False


# --- failing_check_names (names the stuck check for the escalation) -----


def test_failing_check_names_returns_only_failing_buckets():
    # Arrange
    from scitex_agent_container._lifecycle._github_ci import failing_check_names

    payload = (
        '[{"name":"CodeQL","bucket":"fail"},'
        ' {"name":"pytest","bucket":"pass"},'
        ' {"name":"lint","bucket":"cancel"}]'
    )
    # Act
    names = failing_check_names("o/r", 1, run=lambda args: payload)
    # Assert
    assert names == ["CodeQL", "lint"]


def test_failing_check_names_requests_the_name_field():
    # Arrange — `--json bucket` alone discards the name, so the ring cannot
    # tell "same check every time" from "a new one".
    from scitex_agent_container._lifecycle._github_ci import failing_check_names

    seen: list[list] = []

    def spy(args):
        seen.append(args)
        return "[]"

    # Act
    failing_check_names("o/r", 1, run=spy)
    # Assert
    assert "name,bucket" in seen[0]


def test_failing_check_names_is_empty_on_unparseable_output():
    # Arrange — a caller must always be able to render the escalation.
    from scitex_agent_container._lifecycle._github_ci import failing_check_names

    # Act
    names = failing_check_names("o/r", 1, run=lambda args: "not json")
    # Assert
    assert names == []


def test_failing_check_names_dedupes_and_sorts():
    # Arrange — a matrix leg can appear more than once.
    from scitex_agent_container._lifecycle._github_ci import failing_check_names

    payload = (
        '[{"name":"zeta","bucket":"fail"},'
        ' {"name":"alpha","bucket":"fail"},'
        ' {"name":"alpha","bucket":"fail"}]'
    )
    # Act
    names = failing_check_names("o/r", 1, run=lambda args: payload)
    # Assert
    assert names == ["alpha", "zeta"]
