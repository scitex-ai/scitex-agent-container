"""Tests for the runner-compliance read behind ``sac ci runners``.

The regression these exist for: a job can be GREEN and still have run on
banned hardware, because the run was queued before the repo was repointed.
Status is therefore not the signal — ``runner_name`` is. The ``run_gh``
injection seam (the same one ``_ci_why`` tests use) stands in for the network.
AAA, one assertion per test.
"""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.cli_pkg._ci_runners import (
    JobRunner,
    RunRunners,
    ambiguous_labels,
    audit,
    is_banned,
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


def test_is_banned_matches_a_spartan_runner_name():
    # Arrange
    name = "spartan-cpu-org-01"
    # Act
    result = is_banned(name)
    # Assert
    assert result is True


def test_is_banned_is_false_for_a_compliant_runner():
    # Arrange
    name = "scitex-03-org-cpu-01"
    # Act
    result = is_banned(name)
    # Assert
    assert result is False


def test_is_banned_is_false_when_the_runner_is_unassigned():
    # Arrange: a queued job has no runner yet
    name = None
    # Act
    result = is_banned(name)
    # Assert: absence must not read as a violation
    assert result is False


def test_ambiguous_labels_flags_scitex_ci():
    # Arrange
    labels = ["self-hosted", "scitex-ci"]
    # Act
    result = ambiguous_labels(labels)
    # Assert
    assert result == ["scitex-ci"]


def test_ambiguous_labels_is_empty_for_an_unambiguous_selector():
    # Arrange
    labels = ["self-hosted", "scitex-org-cpu"]
    # Act
    result = ambiguous_labels(labels)
    # Assert
    assert result == []


def test_a_green_job_on_a_banned_runner_is_a_violation():
    # Arrange: THE regression — conclusion=success, but it ran on Spartan
    gh = _jobs_gh([_job(runner="spartan-cpu-org-01", conclusion="success")])
    # Act
    run = run_job_runners("1", run_gh=gh)
    # Assert
    assert [j.job for j in run.violations] == ["j"]


def test_a_green_job_on_a_compliant_runner_is_not_a_violation():
    # Arrange
    gh = _jobs_gh([_job()])
    # Act
    run = run_job_runners("1", run_gh=gh)
    # Assert
    assert run.violations == []


def test_an_ambiguous_selector_is_a_warning_not_a_violation():
    # Arrange
    gh = _jobs_gh([_job(labels=["self-hosted", "scitex-ci"])])
    # Act
    run = run_job_runners("1", run_gh=gh)
    # Assert
    assert [j.job for j in run.warnings] == ["j"]


def test_the_runner_name_is_reported_verbatim():
    # Arrange
    gh = _jobs_gh([_job(runner="scitex-03-org-cpu-01")])
    # Act
    run = run_job_runners("1", run_gh=gh)
    # Assert
    assert run.jobs[0].runner_name == "scitex-03-org-cpu-01"


def test_unparseable_jobs_payload_raises_rather_than_reading_as_clean():
    # Arrange
    def _bad(_argv):
        return "not json"

    # Act / Assert guarded by pytest.raises
    with pytest.raises(CIWhyError):
        # Assert
        run_job_runners("1", run_gh=_bad)


def test_a_payload_without_jobs_raises_rather_than_reading_as_clean():
    # Arrange
    def _empty(_argv):
        return json.dumps({})

    # Act / Assert guarded by pytest.raises
    with pytest.raises(CIWhyError):
        # Assert
        run_job_runners("1", run_gh=_empty)


def test_a_pr_resolves_through_all_checks_including_passing_ones():
    # Arrange: _ci_why keeps only FAILING runs; compliance must see green ones
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


def test_render_marks_a_banned_runner_visibly():
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
    assert "BANNED" in text


def test_render_names_the_ambiguous_selector_in_the_warning():
    # Arrange
    run = RunRunners(
        run_id="1",
        jobs=[
            JobRunner(
                job="j",
                status="completed",
                conclusion="success",
                runner_name="scitex-01-org-cpu-01",
                runner_group="scitex-local-cpu",
                labels=["scitex-ci"],
            )
        ],
    )
    # Act
    text = render_text(run)
    # Assert
    assert "scitex-ci" in text
