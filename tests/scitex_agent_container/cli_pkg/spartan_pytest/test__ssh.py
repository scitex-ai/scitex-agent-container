"""SSH result parsing and scheduler-query refusal controls.

Use explicit transport responses and an owned SSH/accounting shell fixture.
No live cluster is contacted; the separate real round-trip retains its
existing opt-in in test__run_cmd.
"""

from __future__ import annotations

import os
import subprocess

import click
import pytest

from scitex_agent_container.cli_pkg.spartan_pytest import _extract_job_id, _poll_job


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


def _poll_failure(action):
    """Capture only the expected refusal type as data for one assertion."""
    try:
        action()
    except click.ClickException as exc:
        return exc
    return None


@pytest.mark.parametrize(
    "responses,fragment",
    [
        ([(255, "", "refused")], "failed to query"),
        ([(1, "RUNNING", "refused")], "failed to query"),
        ([(0, "", ""), (1, "COMPLETED", "failed")], "accounting state"),
        ([(0, "", ""), (0, "", "")], "accounting state"),
    ],
)
def test_failed_or_missing_scheduler_query_never_implies_completion(
    responses, fragment
):
    # Arrange
    calls = []
    pending = iter(responses)

    def runner(argv, **kwargs):
        calls.append(argv)
        code, stdout, stderr = next(pending)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    # Act
    error = _poll_failure(
        lambda: _poll_job(
            "owned",
            "123",
            timeout_s=10,
            interval_s=1,
            runner=runner,
            clock=lambda: 0,
            sleeper=lambda _: None,
        )
    )
    # Assert
    assert (
        isinstance(error, click.ClickException),
        fragment in str(error),
        len(calls),
    ) == (
        True,
        True,
        len(responses),
    )


def test_observed_completion_retains_poll_interval_and_accounting_state():
    # Arrange
    responses = iter([(0, "RUNNING", ""), (0, "", ""), (0, "COMPLETED", "")])
    sleeps = []

    def runner(argv, **kwargs):
        code, stdout, stderr = next(responses)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    # Act
    state = _poll_job(
        "owned",
        "123",
        timeout_s=10,
        interval_s=7,
        runner=runner,
        clock=lambda: 0,
        sleeper=sleeps.append,
    )
    # Assert
    assert (state, sleeps) == ("COMPLETED", [7])


@pytest.fixture
def failed_accounting_shell(tmp_path, env_save_restore):
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "ssh").write_text(
        '#!/bin/sh\nfor command; do :; done\nexec sh -c "$command"\n'
    )
    (binary / "squeue").write_text("#!/bin/sh\nexit 0\n")
    (binary / "sacct").write_text("#!/bin/sh\necho COMPLETED\nexit 7\n")
    for path in binary.iterdir():
        path.chmod(0o700)
    env_save_restore.set("PATH", str(binary) + os.pathsep + os.environ["PATH"])


def test_real_shell_cannot_hide_failed_accounting_behind_head(failed_accounting_shell):
    # Arrange
    host = "owned-fixture"
    # Act
    error = _poll_failure(lambda: _poll_job(host, "123", timeout_s=10, interval_s=1))
    # Assert
    assert (
        isinstance(error, click.ClickException),
        "accounting state for SLURM job 123 (7)" in str(error),
    ) == (True, True)
