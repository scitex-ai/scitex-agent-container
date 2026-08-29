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




def _rest(check_runs=None, statuses=None, sha="deadbeef"):
    """A ``run`` double speaking the REST shapes ``pr_ci_conclusion`` now reads.

    The function used to make ONE call (`gh pr checks --json bucket`) and the
    tests could answer with a single string. It now makes up to three REST
    calls — head sha, check-runs, commit statuses — because `gh pr checks`
    merged check-runs AND commit statuses and dropping the second would have
    made a green verdict green by stopping looking (measured 2026-08-19:
    scitex-dev had check_runs=16 statuses=1 on a live PR).

    So the double DISPATCHES ON THE URL rather than returning one blob. Each
    test below still asserts exactly the property it asserted before; only
    the wire shape moved.
    """
    import json as _json

    def run(args):
        url = args[1] if len(args) > 1 else ""
        if "/check-runs" in url:
            return _json.dumps({"check_runs": check_runs or []})
        if url.endswith("/status"):
            return _json.dumps({"statuses": statuses or []})
        return sha

    return run


def _completed(conclusion):
    return {"status": "completed", "conclusion": conclusion}


def test_all_pass_buckets_yield_success_conclusion():
    # Arrange — every check completed green or skipped.
    run = _rest(check_runs=[_completed("success"), _completed("skipped")])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=run)
    # Assert
    assert conclusion == "success"


def test_any_fail_bucket_yields_failure_conclusion():
    # Arrange — one red among greens must dominate.
    run = _rest(check_runs=[_completed("success"), _completed("failure")])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=run)
    # Assert
    assert conclusion == "failure"


def test_cancel_bucket_yields_failure_conclusion():
    # Arrange — a cancelled run is NOT a pass; it is unfinished work whose
    # verdict nobody has. It counted as failure before and must still.
    run = _rest(check_runs=[_completed("success"), _completed("cancelled")])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=run)
    # Assert
    assert conclusion == "failure"


def test_pending_without_fail_yields_pending_conclusion():
    # Arrange — an in-flight run (status != completed) with no red.
    run = _rest(
        check_runs=[_completed("success"), {"status": "in_progress"}]
    )
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=run)
    # Assert
    assert conclusion == "pending"


def test_empty_checks_list_yields_none_conclusion():
    # Arrange — no check-runs AND no statuses: nothing to report, and the
    # poller must deliver nothing rather than invent a verdict.
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

def test_a_commit_status_is_not_dropped():
    """`gh pr checks` merged check-runs AND commit statuses; REST does not.

    Substituting only /check-runs would silently ignore external CI that
    reports through the older Status API — a green verdict produced by
    looking at less. scitex-dev had exactly one such status on a live PR
    when this was measured, so it is not hypothetical.
    """
    # Arrange — all check-runs green, one commit status red.
    run = _rest(
        check_runs=[_completed("success")],
        statuses=[{"state": "failure"}],
    )
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=run)
    # Assert
    assert conclusion == "failure"


def test_an_unknown_conclusion_is_pending_not_success():
    """A conclusion GitHub adds later must never read as green.

    The mapping is explicit and closed; anything outside it falls to
    pending. Defaulting the other way would make a future GitHub release
    turn unknown states into passes, silently, everywhere.
    """
    # Arrange
    run = _rest(check_runs=[_completed("some_new_state_github_added")])
    # Act
    conclusion = pr_ci_conclusion("o/r", 1, run=run)
    # Assert
    assert conclusion == "pending"


def test_the_caller_can_supply_the_head_sha_and_save_a_call():
    """The poll loop already has the sha from `list_open_prs`.

    Passing it removes one REST call per PR per tick — the whole point of
    this change being about call VOLUME, not just which pool it spends.
    """
    # Arrange
    seen: list = []

    def run(args):
        seen.append(args[1] if len(args) > 1 else "")
        if "/check-runs" in (args[1] if len(args) > 1 else ""):
            return json.dumps({"check_runs": [_completed("success")]})
        return json.dumps({"statuses": []})

    # Act
    conclusion = pr_ci_conclusion("o/r", 1, head_sha="cafe1234", run=run)
    # Assert
    assert conclusion == "success" and not any(
        u.endswith("/pulls/1") for u in seen
    )


# EOF
