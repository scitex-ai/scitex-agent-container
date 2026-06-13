"""Tests for ``_render_sbatch_script`` + ``DEFAULT_RESERVATION``.

Pure-string output: the renderer takes (repo, branch, reservation,
scratch_dir, job_tag) and emits the sbatch script body. No shell-out,
no mocks — every assertion is on the rendered text.

Style: STX-TQ002 AAA markers, STX-TQ007 one assert per test, no mocks
(the renderer is a pure function — there is nothing to mock).
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg.spartan_pytest import (
    DEFAULT_RESERVATION,
    _render_sbatch_script,
)


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
    # Arrange
    kwargs = dict(
        repo="o/r",
        branch="b",
        reservation="r",
        scratch_dir="/s/x",
        job_tag="b",
    )
    # Act
    script = _render_sbatch_script(**kwargs)
    # Assert — Phase 1 runs pytest with --no-cov so cov isn't required.
    assert "pytest -q --no-cov --maxfail=20" in script


def test_default_reservation_is_operator_sapphire_pool():
    # Arrange — operator's directive pinned to ``sapphire`` for Phase 1.
    expected = "sapphire"
    # Act
    actual = DEFAULT_RESERVATION
    # Assert
    assert actual == expected
