"""Renderer text and actual Bash pipeline controls in owned fixtures.

The renderer is pure. Pipeline tests execute its Bash output with real tee
and summary serialization around inert git/install/pytest tools; they do
not install dependencies or run a real target package or cluster job.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from scitex_agent_container.cli_pkg.spartan_pytest import (
    DEFAULT_RESERVATION,
    _parse_summary,
    _render_sbatch_script,
    _resolve_exit_code,
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


@pytest.mark.parametrize("field", ["reservation", "scratch_dir", "job_tag"])
@pytest.mark.parametrize("control", ["\r", "\n", "\x00"])
def test_render_refuses_line_breaking_header_values(field, control):
    # Arrange
    fields = dict(
        repo="owned/fixture",
        branch="main",
        reservation="owned",
        scratch_dir="/owned/run",
        job_tag="main",
    )
    fields[field] += control + "#SBATCH --time=99:00:00"
    # Act
    # Assert
    with pytest.raises(ValueError, match=field + " must be single-line"):
        _render_sbatch_script(**fields)


@pytest.fixture
def rendered_job_tools(tmp_path, env_save_restore):
    """Use inert install/test tools around the actual rendered Bash pipeline."""
    binary = tmp_path / "bin"
    binary.mkdir()
    real_tee = shutil.which("tee")
    (binary / "git").write_text(
        '#!/bin/sh\nif [ "$1" = clone ]; then mkdir -p checkout; fi\n'
    )
    (binary / "python").write_text(
        '#!/bin/sh\ncase "${1-}:${2-}" in\n'
        "  -m:pip) exit 0;;\n"
        '  -m:pytest) printf "%s\\n" "$SAC_FIXTURE_PYTEST_LOG"; exit "$SAC_FIXTURE_PYTEST_EXIT";;\n'
        f'  *) exec {shlex.quote(sys.executable)} "$@";;\nesac\n'
    )
    (binary / "tee").write_text(
        f'#!/bin/sh\n{shlex.quote(real_tee)} "$@"\nexit "$SAC_FIXTURE_CAPTURE_EXIT"\n'
    )
    for path in binary.iterdir():
        path.chmod(0o700)
    env_save_restore.set("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    "pytest_exit,capture_exit,expected",
    [
        (0, 0, 0),
        (5, 0, 5),
        (3, 0, 3),
        (0, 1, 1),
        (1, 1, 1),
    ],
)
def test_rendered_bash_retains_real_pipeline_exit_evidence(
    rendered_job_tools, env_save_restore, pytest_exit, capture_exit, expected
):
    # Arrange
    root = rendered_job_tools
    env_save_restore.set("SAC_FIXTURE_PYTEST_LOG", "3 passed in 0.1s")
    env_save_restore.set("SAC_FIXTURE_PYTEST_EXIT", str(pytest_exit))
    env_save_restore.set("SAC_FIXTURE_CAPTURE_EXIT", str(capture_exit))
    scratch = root / "scratch"
    script = root / "job.sh"
    script.write_text(
        _render_sbatch_script(
            repo="owned/fixture",
            branch="main",
            reservation="owned",
            scratch_dir=str(scratch),
            job_tag="fixture",
        )
    )
    # Act
    result = subprocess.run(
        ["bash", str(script)], cwd=root, capture_output=True, text=True, timeout=30
    )
    summary = json.loads((scratch / "summary.json").read_text())
    # Assert
    assert (
        result.returncode,
        summary["pytest_exit"],
        summary["capture_exit"],
        summary["passed"],
        _resolve_exit_code(_parse_summary(json.dumps(summary))),
    ) == (expected, pytest_exit, capture_exit, 3, int(expected != 0))
