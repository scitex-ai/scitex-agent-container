"""SSH result parsing and scheduler-query refusal controls.

Use explicit transport responses and an owned SSH/accounting shell fixture.
No live cluster is contacted; the separate real round-trip retains its
existing opt-in in test__run_cmd.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import click
import pytest

from scitex_agent_container.cli_pkg.spartan_pytest import (
    _extract_job_id,
    _poll_job,
    _submit_sbatch,
)
from scitex_agent_container.cli_pkg.spartan_pytest._ssh import _resolve_scratch_root


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


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("warning: retry in 30 seconds\nSubmitted batch job 123456\n", "123456"),
        ("warning: retry in 30 seconds\n", None),
        ("123456", None),
        ("Submitted batch job 0", None),
        ("Submitted batch job -12", None),
        ("Submitted batch job １２３", None),
        ("Submitted batch job 123 trailing text", None),
        ("prefix Submitted batch job 123", None),
        ("Submitted batch job 123\nSubmitted batch job 456", None),
        ("Submitted batch job 123\nSubmitted batch job 123", None),
        ("\r\n Submitted batch job 123 \r\n", "123"),
    ],
)
def test_submission_receipt_does_not_guess_from_diagnostics(stdout, expected):
    # Arrange
    output = stdout
    # Act
    job_id = _extract_job_id(output)
    # Assert
    assert job_id == expected


@pytest.mark.parametrize(
    "stdout",
    [
        "warning: retry in 30 seconds\n",
        "Submitted batch job 123\nSubmitted batch job 456\n",
    ],
)
def test_ambiguous_submission_is_refused_after_upload_without_retry(stdout):
    # Arrange
    calls = []
    responses = iter([(0, "", ""), (0, stdout, "")])

    def runner(argv, **kwargs):
        calls.append(argv)
        code, output, stderr = next(responses)
        return subprocess.CompletedProcess(argv, code, output, stderr)

    # Act
    error = _poll_failure(
        lambda: _submit_sbatch(
            "owned-host", "#!/bin/sh\ntrue", "/owned/run/job.sbatch", runner=runner
        )
    )
    # Assert
    assert (
        isinstance(error, click.ClickException),
        "could not parse SLURM job id" in str(error),
        len(calls),
        calls[-1][-1],
    ) == (True, True, 2, "sbatch /owned/run/job.sbatch")


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


@pytest.fixture
def upload_shell(tmp_path, env_save_restore):
    """Real shell/cat upload with an inert scheduler; never execute job text."""
    binary = tmp_path / "bin"
    binary.mkdir()
    submitted = tmp_path / "submitted"
    real_cat = shutil.which("cat")
    (binary / "ssh").write_text(
        '#!/bin/sh\nfor command; do :; done\nexec sh -c "$command"\n'
    )
    (binary / "cat").write_text(
        '#!/bin/sh\nif [ "${SAC_FIXTURE_UPLOAD_FAIL:-0}" -ne 0 ]; then\n'
        '  exit "$SAC_FIXTURE_UPLOAD_FAIL"\nfi\n'
        f'exec {shlex.quote(real_cat)} "$@"\n'
    )
    (binary / "sbatch").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$1" >> {shlex.quote(str(submitted))}\n'
        "echo 'Submitted batch job 123'\n"
    )
    for path in binary.iterdir():
        path.chmod(0o700)
    env_save_restore.set("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    env_save_restore.set("SAC_FIXTURE_UPLOAD_FAIL", "0")
    return tmp_path, submitted


@pytest.mark.parametrize("ending", ["", "\n"])
def test_upload_preserves_script_as_data_without_executing_delimiter_text(
    upload_shell, ending
):
    # Arrange
    root, submitted = upload_shell
    path = root / "remote root 'quoted $ value" / "job.sbatch"
    marker = root / "upload-executed"
    script = (
        "#!/bin/sh\n# Unicode: é\n__SAC_SBATCH_EOF__\n"
        f"printf injected > {shlex.quote(str(marker))}\n# remaining data" + ending
    )
    # Act
    job_id = _submit_sbatch("owned", script, str(path))
    # Assert
    assert (
        job_id,
        path.read_text(),
        bool(path.stat().st_mode & 0o100),
        marker.exists(),
        submitted.read_text(),
    ) == ("123", script, True, False, str(path) + "\n")


def test_failed_script_upload_never_chmods_or_submits_partial_file(
    upload_shell, env_save_restore
):
    # Arrange
    root, submitted = upload_shell
    path = root / "job.sbatch"
    path.write_text("owned previous artifact")
    path.chmod(0o600)
    env_save_restore.set("SAC_FIXTURE_UPLOAD_FAIL", "8")
    # Act
    error = _poll_failure(lambda: _submit_sbatch("owned", "new data", str(path)))
    # Assert
    assert (
        isinstance(error, click.ClickException),
        "failed to upload sbatch script (8)" in str(error),
        bool(path.stat().st_mode & 0o100),
        submitted.exists(),
    ) == (True, True, False, False)


@pytest.mark.parametrize("path", ["/owned/root", "relative/root", "~/$OTHER"])
def test_explicit_scratch_path_does_not_query_or_expand_environment(path):
    # Arrange
    def forbidden_runner(*args, **kwargs):
        raise RuntimeError("explicit path must not contact SSH")

    # Act
    result = _resolve_scratch_root("owned", path, runner=forbidden_runner)
    # Assert
    assert result == path


@pytest.mark.parametrize(
    "path,expected",
    [
        ("$SCRATCH/sac-pytest", "/remote/owned root 'quoted $ value/sac-pytest"),
        ("${SCRATCH}/sac-pytest", "/remote/owned root 'quoted $ value/sac-pytest"),
        ("$SCRATCH", "/remote/owned root 'quoted $ value/"),
        ("${SCRATCH}", "/remote/owned root 'quoted $ value/"),
    ],
)
def test_scratch_token_resolves_only_the_remote_variable(path, expected):
    # Arrange
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, "/remote/owned root 'quoted $ value/\n", ""
        )

    # Act
    result = _resolve_scratch_root("owned", path, runner=runner)
    # Assert
    assert (result, len(calls), calls[0][-1]) == (
        expected,
        1,
        "printf '%s\\n' \"${SCRATCH-}\"",
    )


@pytest.mark.parametrize(
    "stdout",
    ["", "\n", "relative\n", "/owned\nnoise\n", "/owned\r\n", "/owned\x00bad\n"],
)
def test_missing_or_malformed_remote_scratch_is_refused(stdout):
    # Arrange
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    # Act
    # Assert
    with pytest.raises(click.ClickException, match="remote SCRATCH must be"):
        _resolve_scratch_root("owned", "$SCRATCH/sac-pytest", runner=runner)


def test_failed_remote_scratch_read_is_not_replaced_by_local_guess():
    # Arrange
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, "/untrusted\n", "refused")

    # Act
    # Assert
    with pytest.raises(
        click.ClickException, match=r"failed to read remote SCRATCH \(255\)"
    ):
        _resolve_scratch_root("owned", "$SCRATCH/sac-pytest", runner=runner)
