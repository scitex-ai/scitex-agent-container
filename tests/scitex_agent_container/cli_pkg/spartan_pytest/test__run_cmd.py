"""Tests for the click command surface in :mod:`spartan_pytest._run_cmd`.

CliRunner drives the actual click group — no mocks. The opt-in live
round-trip is gated behind ``$SAC_SPARTAN_HOST`` so the default suite
never reaches out to a remote SLURM cluster from a laptop.

Style: STX-TQ002 AAA markers, STX-TQ007 one assert per test, no mocks.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.spartan_pytest import pytest_group


def test_pytest_group_help_lists_spartan_subgroup():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(pytest_group, ["--help"])
    # Assert
    assert "spartan" in result.output


def test_spartan_run_help_documents_reservation_flag():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(pytest_group, ["spartan", "run", "--help"])
    # Assert
    assert "--reservation" in result.output


def test_spartan_run_rejects_target_without_at_sign():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(pytest_group, ["spartan", "run", "bare-repo"])
    # Assert — UsageError translates to non-zero exit + the format hint.
    assert "REPO@BRANCH" in result.output


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--timeout", "0"),
        ("--timeout", "-1"),
        ("--poll-interval", "0"),
        ("--poll-interval", "-1"),
    ],
)
def test_invalid_poll_bounds_are_refused_before_any_remote_action(
    scheduler_fixture, flag, value
):
    # Arrange
    calls = scheduler_fixture(
        {
            "passed": 3,
            "failed": 0,
            "errors": 0,
            "pytest_exit": 0,
            "capture_exit": 0,
            "slurm_job_id": "123",
        },
        "COMPLETED",
        0,
    )
    # Act
    result = CliRunner().invoke(
        pytest_group, ["spartan", "run", "owned/fixture@main", flag, value]
    )
    # Assert
    assert (result.exit_code, "x>=1" in result.output, calls.exists()) == (
        2,
        True,
        False,
    )


@pytest.mark.parametrize("field", ["reservation", "scratch-root", "target"])
@pytest.mark.parametrize("control", ["\r", "\n", "\x00"])
def test_line_breaking_headers_are_refused_before_any_remote_action(
    scheduler_fixture, field, control
):
    # Arrange
    calls = scheduler_fixture(
        {
            "passed": 3,
            "failed": 0,
            "errors": 0,
            "pytest_exit": 0,
            "capture_exit": 0,
            "slurm_job_id": "123",
        },
        "COMPLETED",
        0,
    )
    bad = "main" + control + "#SBATCH --time=99:00:00"
    cases = {
        "reservation": ["owned/fixture@main", "--reservation", bad],
        "scratch-root": ["owned/fixture@main", "--scratch-root", "/owned/" + bad],
        "target": ["owned/fixture@" + bad],
    }
    # Act
    result = CliRunner().invoke(pytest_group, ["spartan", "run", *cases[field]])
    # Assert
    assert (result.exit_code, "single-line" in result.output, calls.exists()) == (
        2,
        True,
        False,
    )


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
    runner = CliRunner()
    # Act
    result = runner.invoke(
        pytest_group,
        ["spartan", "run", target, "--yes", "--ssh-host", host, "--timeout", "1800"],
    )
    # Assert — either green or operator-meaningful failure surface.
    assert "Spartan pytest" in result.output


@pytest.fixture
def scheduler_fixture(tmp_path, env_save_restore):
    """Owned SSH executable reports staged scheduler/summary observations."""
    directory = tmp_path / "bin"
    directory.mkdir()
    calls = tmp_path / "calls"
    state_file = tmp_path / "state"
    summary_file = tmp_path / "summary.json"
    queue_code = tmp_path / "queue-code"
    upload = calls.with_suffix(".upload")
    fetched = calls.with_suffix(".fetch")
    script = directory / "ssh"
    script.write_text(
        "#!/bin/sh\nfor command; do :; done\n"
        f'case "$command" in\n'
        f"  'printf '*) echo scratch >> {shlex.quote(str(calls))}; "
        f"printf '%s\\n' {shlex.quote(str(tmp_path / 'remote-scratch'))};;\n"
        f"  'mkdir -p '*) echo upload >> {shlex.quote(str(calls))}; "
        f"printf '%s\\n' \"$command\" > {shlex.quote(str(upload))};;\n"
        f"  'sbatch '*) echo submit >> {shlex.quote(str(calls))}; echo 'Submitted batch job 123';;\n"
        f"  'squeue '*) echo queue >> {shlex.quote(str(calls))}; exit \"$(cat {shlex.quote(str(queue_code))})\";;\n"
        f"  'sacct '*) echo accounting >> {shlex.quote(str(calls))}; cat {shlex.quote(str(state_file))};;\n"
        f"  'cat '*) echo summary >> {shlex.quote(str(calls))}; "
        f"printf '%s\\n' \"$command\" > {shlex.quote(str(fetched))}; "
        f"cat {shlex.quote(str(summary_file))};;\n"
        "  *) exit 99;;\nesac\n"
    )
    script.chmod(0o700)
    env_save_restore.set("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("SCITEX_DIR", str(tmp_path / ".scitex"))

    def configure(payload, state, queue_exit):
        summary_file.write_text(json.dumps(payload))
        state_file.write_text(state + "\n")
        queue_code.write_text(str(queue_exit))
        return calls

    return configure


@pytest.mark.parametrize(
    "payload,state,queue_exit,code,passed,call_count",
    [
        (
            {
                "passed": 3,
                "failed": 0,
                "errors": 0,
                "pytest_exit": 0,
                "capture_exit": 0,
                "slurm_job_id": "123",
            },
            "COMPLETED",
            0,
            0,
            True,
            6,
        ),
        (
            {
                "passed": 3,
                "failed": 0,
                "errors": 0,
                "pytest_exit": 0,
                "capture_exit": 0,
                "slurm_job_id": "123",
            },
            "FAILED",
            0,
            1,
            False,
            6,
        ),
        (
            {
                "passed": 3,
                "failed": 0,
                "errors": 0,
                "pytest_exit": 0,
                "capture_exit": 0,
                "slurm_job_id": "123",
            },
            "RUNNING",
            0,
            1,
            False,
            6,
        ),
        ({}, "COMPLETED", 0, 1, False, 6),
        ({"passed": 3, "failed": 0, "errors": 0}, "COMPLETED", 0, 1, False, 6),
        (
            {
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "pytest_exit": 5,
                "capture_exit": 0,
                "slurm_job_id": "123",
            },
            "COMPLETED",
            0,
            1,
            False,
            6,
        ),
        (
            {
                "passed": 3,
                "failed": 0,
                "errors": 0,
                "pytest_exit": 0,
                "capture_exit": 1,
                "slurm_job_id": "123",
            },
            "COMPLETED",
            0,
            1,
            False,
            6,
        ),
        (
            {
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "pytest_exit": 0,
                "capture_exit": 0,
                "slurm_job_id": "123",
            },
            "COMPLETED",
            0,
            1,
            False,
            6,
        ),
        (
            {
                "passed": 2,
                "failed": 1,
                "errors": 0,
                "pytest_exit": 1,
                "capture_exit": 0,
                "slurm_job_id": "123",
            },
            "FAILED",
            0,
            1,
            False,
            6,
        ),
        (
            {
                "passed": 3,
                "failed": 0,
                "errors": 0,
                "pytest_exit": 0,
                "capture_exit": 0,
                "slurm_job_id": "123",
            },
            "COMPLETED",
            255,
            1,
            False,
            4,
        ),
    ],
)
def test_spartan_cli_never_passes_without_process_and_scheduler_evidence(
    scheduler_fixture, payload, state, queue_exit, code, passed, call_count
):
    # Arrange
    calls = scheduler_fixture(payload, state, queue_exit)
    # Act
    result = CliRunner().invoke(
        pytest_group,
        ["spartan", "run", "owner/fixture@main", "--yes", "--ssh-host", "owned-fixture"],
    )
    # Assert
    assert (
        result.exit_code,
        "Spartan pytest PASS:" in result.stdout,
        len(calls.read_text().splitlines()),
    ) == (code, passed, call_count)


def test_repeated_submissions_have_distinct_uuid_paths_and_matching_summary(
    scheduler_fixture, tmp_path
):
    # Arrange
    calls = scheduler_fixture(
        {
            "passed": 3,
            "failed": 0,
            "errors": 0,
            "pytest_exit": 0,
            "capture_exit": 0,
            "slurm_job_id": "123",
        },
        "COMPLETED",
        0,
    )
    scratch = tmp_path / "scratch with spaces"
    args = ["spartan", "run", "owned/fixture@main", "--yes", "--scratch-root", str(scratch)]
    runner = CliRunner()
    # Act
    first = runner.invoke(pytest_group, args)
    first_dir = Path(
        shlex.split(calls.with_suffix(".upload").read_text().splitlines()[0])[2]
    )
    first_summary = Path(shlex.split(calls.with_suffix(".fetch").read_text())[1])
    second = runner.invoke(pytest_group, args)
    second_dir = Path(
        shlex.split(calls.with_suffix(".upload").read_text().splitlines()[0])[2]
    )
    second_summary = Path(shlex.split(calls.with_suffix(".fetch").read_text())[1])
    # Assert
    assert (
        first.exit_code,
        second.exit_code,
        uuid.UUID(first_dir.name).version,
        uuid.UUID(second_dir.name).version,
        first_dir != second_dir,
        first_dir.parent,
        second_dir.parent,
        first_summary.parent,
        second_summary.parent,
        f"planned artifact directory: {first_dir}" in first.stdout,
        f"planned artifact directory: {second_dir}" in second.stdout,
    ) == (
        0,
        0,
        4,
        4,
        True,
        scratch,
        scratch,
        first_dir,
        second_dir,
        True,
        True,
    )


@pytest.mark.parametrize(
    "metadata,code,passed",
    [
        ({"slurm_job_id": "123"}, 0, True),
        ({"slurm_job_id": "999"}, 1, False),
        ({}, 1, False),
        ({"slurm_job_id": 123}, 1, False),
    ],
)
def test_collected_summary_must_name_the_submitted_job(
    scheduler_fixture, metadata, code, passed
):
    # Arrange
    payload = {
        "passed": 3,
        "failed": 0,
        "errors": 0,
        "pytest_exit": 0,
        "capture_exit": 0,
        **metadata,
    }
    calls = scheduler_fixture(payload, "COMPLETED", 0)
    # Act
    result = CliRunner().invoke(
        pytest_group,
        ["spartan", "run", "owned/fixture@main", "--yes", "--scratch-root", "/owned/explicit"],
    )
    # Assert
    assert (
        result.exit_code,
        "Spartan pytest PASS:" in result.stdout,
        len(calls.read_text().splitlines()),
    ) == (code, passed, 5)
