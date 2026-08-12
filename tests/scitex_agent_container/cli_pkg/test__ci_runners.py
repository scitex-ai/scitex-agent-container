"""Tests for the runner read behind ``sac ci runners``.

Two regressions these exist for:

1. A job can be GREEN and still have run somewhere nobody expected, because the
   run was queued before the repo was repointed. Status is not the signal;
   ``runner_name`` is.
2. The first version of this module hardcoded ``BANNED_RUNNER_SUBSTRINGS =
   ("spartan",)``. That host ban was repealed the next day, which would have
   left a tool failing sanctioned runs. So the DEFAULT must gate nothing, and
   policy must come from the caller.

The ``run_gh`` injection seam (the same one ``_ci_why`` tests use) stands in for
the network. AAA, one assertion per test.
"""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.cli_pkg._ci_runners import (
    JobRunner,
    RunRunners,
    audit,
    matches_any,
    render_text,
    resolve_all_run_ids,
    run_job_runners,
)
from scitex_agent_container.cli_pkg._ci_why import CIWhyError


def _jobs_gh(jobs: list[dict]):
    """A gh seam that answers the jobs API with ``jobs``."""

    def _router(argv: list[str]) -> str:
        if argv and argv[0] == "api":
            return json.dumps({"jobs": jobs})
        raise AssertionError(f"unexpected gh call: {argv}")

    return _router


def _job(name="j", runner="scitex-01-org-cpu-01", labels=None, conclusion="success"):
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "runner_name": runner,
        "runner_group_name": "scitex-local-cpu",
        "labels": labels or ["self-hosted", "Linux", "X64", "scitex-org-cpu"],
        "html_url": "https://example/job",
    }


def test_matches_any_is_case_insensitive_on_a_substring():
    # Arrange
    name = "Spartan-CPU-org-01"
    # Act
    result = matches_any(name, ["spartan"])
    # Assert
    assert result is True


def test_matches_any_is_false_when_nothing_matches():
    # Arrange
    name = "scitex-03-org-cpu-01"
    # Act
    result = matches_any(name, ["spartan"])
    # Assert
    assert result is False


def test_matches_any_is_false_for_an_unassigned_runner():
    # Arrange: a queued job has no runner yet
    name = None
    # Act
    result = matches_any(name, ["spartan"])
    # Assert: absence of a host is not evidence about which host
    assert result is False


def test_no_policy_gates_nothing_even_on_a_green_job():
    # Arrange: THE second regression — a bare read must never fail a run
    gh = _jobs_gh([_job(runner="spartan-cpu-org-01", conclusion="success")])
    run = run_job_runners("1", run_gh=gh)
    # Act
    denied = run.denied([])
    # Assert
    assert denied == []


def test_a_green_job_on_a_denied_runner_is_reported():
    # Arrange: THE first regression — conclusion=success, ran somewhere denied
    gh = _jobs_gh([_job(runner="spartan-cpu-org-01", conclusion="success")])
    run = run_job_runners("1", run_gh=gh)
    # Act
    denied = run.denied(["spartan"])
    # Assert
    assert [j.job for j in denied] == ["j"]


def test_a_job_on_an_allowed_runner_is_not_denied():
    # Arrange
    gh = _jobs_gh([_job()])
    run = run_job_runners("1", run_gh=gh)
    # Act
    denied = run.denied(["spartan"])
    # Assert
    assert denied == []


def test_expect_flags_a_job_that_ran_outside_the_expectation():
    # Arrange
    gh = _jobs_gh([_job(runner="spartan-cpu-org-01")])
    run = run_job_runners("1", run_gh=gh)
    # Act
    unexpected = run.unexpected(["org-cpu-0"])
    # Assert
    assert [j.job for j in unexpected] == ["j"]


def test_expect_is_satisfied_when_the_runner_matches():
    # Arrange
    gh = _jobs_gh([_job(runner="scitex-02-org-cpu-01")])
    run = run_job_runners("1", run_gh=gh)
    # Act
    unexpected = run.unexpected(["org-cpu"])
    # Assert
    assert unexpected == []


def test_expect_ignores_a_job_that_has_not_run_anywhere_yet():
    # Arrange: queued job, no runner assigned
    gh = _jobs_gh([_job(runner=None, conclusion=None)])
    run = run_job_runners("1", run_gh=gh)
    # Act
    unexpected = run.unexpected(["org-cpu"])
    # Assert
    assert unexpected == []


def test_expect_with_no_expectation_flags_nothing():
    # Arrange
    gh = _jobs_gh([_job(runner="anything-at-all")])
    run = run_job_runners("1", run_gh=gh)
    # Act
    unexpected = run.unexpected([])
    # Assert
    assert unexpected == []


def test_tally_counts_jobs_per_runner():
    # Arrange
    gh = _jobs_gh(
        [
            _job(name="a", runner="scitex-01-org-cpu-01"),
            _job(name="b", runner="scitex-01-org-cpu-01"),
            _job(name="c", runner="scitex-02-org-cpu-01"),
        ]
    )
    run = run_job_runners("1", run_gh=gh)
    # Act
    tally = run.tally
    # Assert
    assert tally == {"scitex-01-org-cpu-01": 2, "scitex-02-org-cpu-01": 1}


def test_the_runner_name_is_reported_verbatim():
    # Arrange
    gh = _jobs_gh([_job(runner="scitex-03-org-cpu-01")])
    run = run_job_runners("1", run_gh=gh)
    # Act
    name = run.jobs[0].runner_name
    # Assert
    assert name == "scitex-03-org-cpu-01"


def test_an_unassigned_job_renders_as_unassigned_not_as_blank():
    # Arrange
    gh = _jobs_gh([_job(runner=None, conclusion=None)])
    run = run_job_runners("1", run_gh=gh)
    # Act
    where = run.jobs[0].where
    # Assert
    assert where == "<unassigned>"


def test_unparseable_jobs_payload_raises_rather_than_reading_as_clean():
    # Arrange
    def _bad(_argv):
        return "not json"

    # Act
    def call():
        return run_job_runners("1", run_gh=_bad)

    # Assert
    with pytest.raises(CIWhyError):
        call()


def test_a_payload_without_jobs_raises_rather_than_reading_as_clean():
    # Arrange
    def _empty(_argv):
        return json.dumps({})

    # Act
    def call():
        return run_job_runners("1", run_gh=_empty)

    # Assert
    with pytest.raises(CIWhyError):
        call()


def test_a_pr_resolves_through_all_checks_including_passing_ones():
    # Arrange: _ci_why keeps only FAILING runs; this must see the green ones
    def _router(argv):
        if argv[0] == "pr":
            return json.dumps(
                [
                    {"name": "a", "state": "SUCCESS", "link": "x/actions/runs/111"},
                    {"name": "b", "state": "FAILURE", "link": "x/actions/runs/222"},
                ]
            )
        raise AssertionError(argv)

    # Act
    ids = resolve_all_run_ids("572", run_gh=_router)
    # Assert
    assert ids == ["111", "222"]


def test_a_run_id_resolves_to_itself_without_a_network_call():
    # Arrange
    def _boom(_argv):
        raise AssertionError("should not call gh")

    # Act
    ids = resolve_all_run_ids("31546807064", run_gh=_boom)
    # Assert
    assert ids == ["31546807064"]


def test_audit_reports_every_run_behind_the_target():
    # Arrange
    gh = _jobs_gh([_job()])
    # Act
    runs = audit("31546807064", run_gh=gh)
    # Assert
    assert len(runs) == 1


def test_render_shows_the_per_runner_tally():
    # Arrange
    run = RunRunners(
        run_id="1",
        jobs=[
            JobRunner(
                job="a",
                status="completed",
                conclusion="success",
                runner_name="scitex-01-org-cpu-01",
                runner_group="scitex-local-cpu",
            )
        ],
    )
    # Act
    text = render_text(run)
    # Assert
    assert "1 job(s) on scitex-01-org-cpu-01" in text


def test_render_marks_a_denied_job_but_only_when_a_policy_is_given():
    # Arrange
    run = RunRunners(
        run_id="1",
        jobs=[
            JobRunner(
                job="import-smoke",
                status="completed",
                conclusion="success",
                runner_name="spartan-cpu-org-01",
                runner_group="spartan-cpu",
            )
        ],
    )
    # Act
    text = render_text(run, deny=["spartan"])
    # Assert
    assert "FLAG" in text


def test_render_does_not_mark_anything_without_a_policy():
    # Arrange
    run = RunRunners(
        run_id="1",
        jobs=[
            JobRunner(
                job="import-smoke",
                status="completed",
                conclusion="success",
                runner_name="spartan-cpu-org-01",
                runner_group="spartan-cpu",
            )
        ],
    )
    # Act
    text = render_text(run)
    # Assert
    assert "FLAG" not in text
