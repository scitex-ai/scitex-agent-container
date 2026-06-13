"""Thin ssh/SLURM wrappers used by ``sac pytest spartan run``.

Every binary is reached via real ``subprocess.run`` so the tests can
install a fake binary on PATH (via the shared ``subprocess_shim``
fixture) and verify argv shape + control stdout/exit without
monkeypatching anything.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from typing import Callable

import click


def _run_ssh(
    host: str, remote_cmd: str, *, runner: Callable = subprocess.run
) -> subprocess.CompletedProcess:
    """Run ``remote_cmd`` on ``host`` over ssh; return the CompletedProcess.

    ``runner`` is a seam so internal callers can supply a captured
    subprocess.run alternative — production code uses the default
    (real) ``subprocess.run`` and tests rely on the PATH-prepended
    shim fixture, no monkeypatching needed.
    """
    return runner(
        ["ssh", "-o", "BatchMode=yes", host, remote_cmd],
        capture_output=True,
        text=True,
    )


def _extract_job_id(stdout: str) -> str | None:
    """Pull the job id from ``Submitted batch job <id>``.  Pure function."""
    for token in stdout.split():
        if token.isdigit():
            return token
    return None


def _submit_sbatch(
    host: str,
    script: str,
    remote_path: str,
    *,
    runner: Callable = subprocess.run,
) -> str:
    """Write ``script`` to ``remote_path`` on ``host`` and ``sbatch`` it.

    Returns the SLURM job-id (string) extracted from the
    ``Submitted batch job <jobid>`` line.  Raises
    :class:`click.ClickException` on any failure.
    """
    parent = remote_path.rsplit("/", 1)[0]
    write_cmd = (
        f"mkdir -p {shlex.quote(parent)} && "
        f"cat > {shlex.quote(remote_path)} <<'__SAC_SBATCH_EOF__'\n"
        f"{script}\n"
        f"__SAC_SBATCH_EOF__\n"
        f"chmod +x {shlex.quote(remote_path)}"
    )
    write_res = _run_ssh(host, write_cmd, runner=runner)
    if write_res.returncode != 0:
        raise click.ClickException(
            f"failed to upload sbatch script ({write_res.returncode}): "
            f"{write_res.stderr.strip()[:400]}"
        )

    sub_res = _run_ssh(host, f"sbatch {shlex.quote(remote_path)}", runner=runner)
    if sub_res.returncode != 0:
        raise click.ClickException(
            f"sbatch failed ({sub_res.returncode}): {sub_res.stderr.strip()[:400]}"
        )

    job_id = _extract_job_id(sub_res.stdout)
    if job_id is None:
        raise click.ClickException(
            f"could not parse SLURM job id from: {sub_res.stdout.strip()[:200]}"
        )
    return job_id


def _poll_job(
    host: str,
    job_id: str,
    *,
    timeout_s: int,
    interval_s: int,
    runner: Callable = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll ``squeue -j <job_id>`` until the job leaves the queue.

    Returns the final ``sacct`` state (or ``"COMPLETED"`` when squeue
    has already dropped the job and we can't fetch sacct).  Raises
    :class:`click.ClickException` on timeout.
    """
    deadline = clock() + timeout_s
    while clock() < deadline:
        res = _run_ssh(
            host,
            f"squeue -h -j {shlex.quote(job_id)} -o %T",
            runner=runner,
        )
        state = res.stdout.strip()
        if not state:
            sacct = _run_ssh(
                host,
                f"sacct -j {shlex.quote(job_id)} -n -o State -X | head -n1",
                runner=runner,
            )
            final = sacct.stdout.strip() or "COMPLETED"
            return final.split()[0] if final else "COMPLETED"
        sleeper(interval_s)
    raise click.ClickException(f"SLURM job {job_id} did not finish within {timeout_s}s")


def _fetch_summary(
    host: str,
    remote_summary_path: str,
    *,
    runner: Callable = subprocess.run,
) -> str:
    """Read the remote ``summary.json`` content over ssh+cat.

    Using ``cat`` over ssh instead of ``scp`` keeps the dependency
    surface to a single binary (``ssh``) — easier to shim, and avoids
    a temp-file dance on the laptop side.
    """
    res = _run_ssh(host, f"cat {shlex.quote(remote_summary_path)}", runner=runner)
    if res.returncode != 0:
        raise click.ClickException(
            f"failed to fetch summary.json ({res.returncode}): "
            f"{res.stderr.strip()[:400]}"
        )
    return res.stdout


__all__ = [
    "_extract_job_id",
    "_fetch_summary",
    "_poll_job",
    "_run_ssh",
    "_submit_sbatch",
]
