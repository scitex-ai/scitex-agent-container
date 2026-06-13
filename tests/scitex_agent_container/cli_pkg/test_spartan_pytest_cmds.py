"""CLI tests for ``sac pytest spartan run`` (Phase 1 Spartan pytest runner).

No mocks: the click command is driven through a real ``CliRunner``,
and every shell-out goes through the shared ``subprocess_shim`` fixture
(real fake binary on PATH).  The end-to-end ssh+SLURM round-trip is
gated behind ``$SAC_SPARTAN_HOST`` — skipped by default, opt-in for
operators with a Spartan account.

Every test follows STX-TQ002 (AAA markers) + STX-TQ007 (single
assertion) + STX-TQ003 (3+-word descriptive names).
"""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.spartan_pytest import (
    DEFAULT_RESERVATION,
    PytestSummary,
    _extract_job_id,
    _format_summary,
    _parse_summary,
    _render_sbatch_script,
    _resolve_exit_code,
    _split_repo_at_branch,
    pytest_group,
)

# ---------------------------------------------------------------------------
# sbatch script renderer — pure string output.
# ---------------------------------------------------------------------------


def test_render_includes_repo_and_branch_tokens():
    # Arrange
    repo = "ywatanabe1989/sac"
    branch = "feature/spartan-pytest"
    # Act
    script = _render_sbatch_script(
        repo=repo,
        branch=branch,
        reservation="sapphire",
        scratch_dir="/scratch/u/sac/123",
        job_tag="feature-spartan-pytest",
    )
    # Assert — branch + repo both surface in the rendered script.
    assert repo in script and branch in script


def test_render_pins_requested_reservation_in_header():
    # Arrange
    reservation = "custom-pool"
    # Act
    script = _render_sbatch_script(
        repo="owner/r",
        branch="main",
        reservation=reservation,
        scratch_dir="/s/x",
        job_tag="main",
    )
    # Assert
    assert f"#SBATCH --reservation={reservation}" in script


def test_render_quotes_scratch_dir_safely():
    # Arrange — scratch dir contains a shell metachar that must be quoted.
    scratch = "/scratch/u/sac/123;rm -rf /"
    # Act
    script = _render_sbatch_script(
        repo="o/r",
        branch="b",
        reservation="r",
        scratch_dir=scratch,
        job_tag="b",
    )
    # Assert — the raw unquoted form must not appear (only the shlex-quoted form).
    assert f"mkdir -p {scratch}\n" not in script


def test_render_invokes_pytest_with_no_cov_flag():
    # Arrange / Act
    script = _render_sbatch_script(
        repo="o/r",
        branch="b",
        reservation="r",
        scratch_dir="/s/x",
        job_tag="b",
    )
    # Assert — Phase 1 runs pytest with --no-cov so cov isn't required.
    assert "pytest -q --no-cov --maxfail=20" in script


# ---------------------------------------------------------------------------
# Summary JSON parser.
# ---------------------------------------------------------------------------


def test_parse_summary_reads_pass_count():
    # Arrange
    blob = json.dumps({"passed": 42, "failed": 0, "errors": 0, "duration_s": 12.3})
    # Act
    summary = _parse_summary(blob)
    # Assert
    assert summary.passed == 42


def test_parse_summary_reads_failed_tests_list():
    # Arrange
    blob = json.dumps(
        {
            "passed": 5,
            "failed": 2,
            "errors": 0,
            "duration_s": 1.0,
            "failed_tests": ["tests/test_a.py::t1", "tests/test_b.py::t2"],
        }
    )
    # Act
    summary = _parse_summary(blob)
    # Assert
    assert summary.failed_tests == ["tests/test_a.py::t1", "tests/test_b.py::t2"]


def test_parse_summary_returns_errors_on_garbage_json():
    # Arrange
    # Act
    summary = _parse_summary("not json at all {[}")
    # Assert — defensive fallback marks the run as errored, not green.
    assert summary.errors == 1


def test_parse_summary_returns_errors_on_non_dict_payload():
    # Arrange
    # Act
    summary = _parse_summary("[1, 2, 3]")
    # Assert
    assert summary.errors == 1


# ---------------------------------------------------------------------------
# Exit-code mapping.
# ---------------------------------------------------------------------------


def test_exit_code_zero_when_all_green():
    # Arrange
    summary = PytestSummary(passed=10, failed=0, errors=0)
    # Act
    code = _resolve_exit_code(summary)
    # Assert
    assert code == 0


def test_exit_code_one_when_any_failure():
    # Arrange
    summary = PytestSummary(passed=10, failed=1, errors=0)
    # Act
    code = _resolve_exit_code(summary)
    # Assert
    assert code == 1


def test_exit_code_one_when_any_error():
    # Arrange
    summary = PytestSummary(passed=10, failed=0, errors=2)
    # Act
    code = _resolve_exit_code(summary)
    # Assert
    assert code == 1


# ---------------------------------------------------------------------------
# Human-readable summary formatter.
# ---------------------------------------------------------------------------


def test_format_summary_labels_pass_run_as_pass():
    # Arrange
    summary = PytestSummary(passed=3, failed=0, errors=0)
    # Act
    text = _format_summary(summary, repo="o/r", branch="b")
    # Assert
    assert "PASS" in text


def test_format_summary_labels_failed_run_as_fail():
    # Arrange
    summary = PytestSummary(passed=3, failed=1, errors=0)
    # Act
    text = _format_summary(summary, repo="o/r", branch="b")
    # Assert
    assert "FAIL" in text


def test_format_summary_lists_failed_tests_when_present():
    # Arrange
    summary = PytestSummary(
        passed=0,
        failed=1,
        errors=0,
        failed_tests=["tests/test_a.py::t1"],
    )
    # Act
    text = _format_summary(summary, repo="o/r", branch="b")
    # Assert
    assert "tests/test_a.py::t1" in text


# ---------------------------------------------------------------------------
# Job-id extraction.
# ---------------------------------------------------------------------------


def test_extract_job_id_finds_canonical_sbatch_output():
    # Arrange
    stdout = "Submitted batch job 123456\n"
    # Act
    job_id = _extract_job_id(stdout)
    # Assert
    assert job_id == "123456"


def test_extract_job_id_returns_none_when_no_digits():
    # Arrange
    stdout = "sbatch: error: invalid reservation"
    # Act
    job_id = _extract_job_id(stdout)
    # Assert
    assert job_id is None


# ---------------------------------------------------------------------------
# REPO@BRANCH splitter.
# ---------------------------------------------------------------------------


def test_split_repo_at_branch_simple_owner_name():
    # Arrange
    # Act
    repo, branch = _split_repo_at_branch("owner/name@develop")
    # Assert
    assert (repo, branch) == ("owner/name", "develop")


def test_split_repo_at_branch_handles_git_url_with_at():
    # Arrange — git@github.com:owner/repo.git@feature
    target = "git@github.com:owner/repo.git@feature/x"
    # Act
    repo, branch = _split_repo_at_branch(target)
    # Assert — rpartition keeps the SSH ``git@host`` left of the
    # final ``@``, so we recover the right branch suffix.
    assert branch == "feature/x"


def test_split_repo_at_branch_rejects_missing_at():
    # Arrange
    import click as _click

    # Act / Assert — UsageError for malformed input.
    with pytest.raises(_click.UsageError):
        _split_repo_at_branch("owner/name")


# ---------------------------------------------------------------------------
# Click help/surface.
# ---------------------------------------------------------------------------


def test_pytest_group_help_lists_spartan_subgroup():
    # Arrange
    # Act
    result = CliRunner().invoke(pytest_group, ["--help"])
    # Assert
    assert "spartan" in result.output


def test_spartan_run_help_documents_reservation_flag():
    # Arrange
    # Act
    result = CliRunner().invoke(pytest_group, ["spartan", "run", "--help"])
    # Assert
    assert "--reservation" in result.output


def test_spartan_run_rejects_target_without_at_sign():
    # Arrange
    # Act
    result = CliRunner().invoke(pytest_group, ["spartan", "run", "bare-repo"])
    # Assert — UsageError translates to non-zero exit + the format hint.
    assert "REPO@BRANCH" in result.output


def test_default_reservation_is_operator_sapphire_pool():
    # Arrange / Act
    # Assert — operator's directive pinned to ``sapphire`` for Phase 1.
    assert DEFAULT_RESERVATION == "sapphire"


# ---------------------------------------------------------------------------
# Integration leg — opt-in via $SAC_SPARTAN_HOST.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("SAC_SPARTAN_HOST"),
    reason="Set SAC_SPARTAN_HOST=<ssh-alias> to run the real Spartan round-trip.",
)
def test_real_spartan_round_trip_against_live_host():
    """End-to-end smoke against a live Spartan reservation.

    Reserved for opt-in CI/local runs; default suite skips this so
    laptop pytest never reaches out to a remote SLURM cluster.
    """
    # Arrange
    target = os.environ.get(
        "SAC_SPARTAN_SMOKE_TARGET", "ywatanabe1989/scitex-agent-container@develop"
    )
    host = os.environ["SAC_SPARTAN_HOST"]
    # Act
    result = CliRunner().invoke(
        pytest_group,
        ["spartan", "run", target, "--ssh-host", host, "--timeout", "1800"],
    )
    # Assert — either green or operator-meaningful failure surface.
    assert "Spartan pytest" in result.output
