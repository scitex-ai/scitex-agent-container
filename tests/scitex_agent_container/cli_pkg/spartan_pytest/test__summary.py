"""Tests for the summary JSON parser, formatter, exit-code mapper, and
the ``REPO@BRANCH`` splitter — all from :mod:`spartan_pytest._summary`.

The Spartan job writes a ``summary.json`` blob; we parse it back into
a ``PytestSummary``, map that to an exit code, and format it for the
operator. The REPO@BRANCH splitter is the inverse — taking the
operator-facing ``owner/name@branch`` string into the (repo, branch)
tuple the renderer wants.

Style: STX-TQ002 AAA markers, STX-TQ007 one assert per test, no mocks
(all units are pure functions).
"""

from __future__ import annotations

import json

import click as _click

from scitex_agent_container.cli_pkg.spartan_pytest import (
    PytestSummary,
    _format_summary,
    _parse_summary,
    _resolve_exit_code,
    _split_repo_at_branch,
)

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
    blob = "not json at all {[}"
    # Act
    summary = _parse_summary(blob)
    # Assert — defensive fallback marks the run as errored, not green.
    assert summary.errors == 1


def test_parse_summary_returns_errors_on_non_dict_payload():
    # Arrange
    blob = "[1, 2, 3]"
    # Act
    summary = _parse_summary(blob)
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
# REPO@BRANCH splitter.
# ---------------------------------------------------------------------------


def test_split_repo_at_branch_simple_owner_name():
    # Arrange
    target = "owner/name@develop"
    # Act
    repo, branch = _split_repo_at_branch(target)
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
    # Arrange — STX-TQ002/007 split: capture into a variable so Act
    # and Assert can live on separate lines instead of pytest.raises.
    raised: BaseException | None = None
    # Act
    try:
        _split_repo_at_branch("owner/name")
    except _click.UsageError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the helper is contracted to raise UsageError on malformed REPO@BRANCH.)
        raised = exc
    # Assert
    assert isinstance(raised, _click.UsageError)
