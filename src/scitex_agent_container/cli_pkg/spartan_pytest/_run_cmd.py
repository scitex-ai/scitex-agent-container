"""Click surface for ``sac pytest spartan run``.

Composes the pure renderer (``_render``), the summary parser
(``_summary``), and the ssh/SLURM IO wrappers (``_ssh``) into the
operator-facing CLI command.  See ``__init__.py`` for the full
architecture + Phase-2 follow-ups.
"""

from __future__ import annotations

import time

import click

from .._helpers import HelpRecursiveGroup
from ._render import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_RESERVATION,
    DEFAULT_SSH_HOST,
    DEFAULT_TIMEOUT_S,
    _render_sbatch_script,
)
from ._ssh import _fetch_summary, _poll_job, _submit_sbatch
from ._summary import (
    _format_summary,
    _parse_summary,
    _resolve_exit_code,
    _split_repo_at_branch,
)


@click.group(name="pytest", cls=HelpRecursiveGroup)
def pytest_group() -> None:
    """Run pytest on remote pools (Spartan SLURM, ...)."""


@pytest_group.group(name="spartan", cls=HelpRecursiveGroup)
def spartan_group() -> None:
    """Spartan-backed pytest runner (sapphire reservation by default)."""


@spartan_group.command(name="run")
@click.argument("target")
@click.option(
    "--ssh-host",
    "ssh_host",
    default=DEFAULT_SSH_HOST,
    help=f"SSH alias for the Spartan login node (default: {DEFAULT_SSH_HOST}).",
)
@click.option(
    "--reservation",
    "reservation",
    default=DEFAULT_RESERVATION,
    help=f"SLURM reservation name (default: {DEFAULT_RESERVATION}).",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=int,
    default=DEFAULT_TIMEOUT_S,
    help=f"Max seconds to wait for the SLURM job (default: {DEFAULT_TIMEOUT_S}).",
)
@click.option(
    "--poll-interval",
    "interval_s",
    type=int,
    default=DEFAULT_POLL_INTERVAL_S,
    help=f"Seconds between squeue polls (default: {DEFAULT_POLL_INTERVAL_S}).",
)
@click.option(
    "--scratch-root",
    "scratch_root",
    default="$SCRATCH/sac-pytest",
    help="Spartan scratch root for clones + summary.json "
    "(default: $SCRATCH/sac-pytest).",
)
def run_cmd(
    target: str,
    ssh_host: str,
    reservation: str,
    timeout_s: int,
    interval_s: int,
    scratch_root: str,
) -> None:
    """Submit + collect a Spartan pytest job for ``REPO@BRANCH``.

    \b
    REPO accepts a full git URL or ``owner/name`` shorthand (resolved
    against github.com on Spartan).

    \b
    Examples:
      $ sac pytest spartan run ywatanabe1989/sac@develop
      $ sac pytest spartan run git@github.com:me/repo.git@feature/x --reservation sapphire
    """
    repo, branch = _split_repo_at_branch(target)
    # SLURM caps job names at ~64 chars so we trim the branch suffix
    # aggressively to keep ``sac-pytest-<tag>`` well under the limit.
    job_tag = branch.replace("/", "-")[:32]
    # Per-submission scratch namespace so concurrent runs don't
    # stomp each other.  ``$SLURM_JOB_ID`` isn't available before
    # sbatch picks one up, so we use a wall-clock millisecond stamp.
    stamp = str(int(time.time() * 1000))
    scratch_dir = f"{scratch_root}/{stamp}"
    script = _render_sbatch_script(
        repo=repo,
        branch=branch,
        reservation=reservation,
        scratch_dir=scratch_dir,
        job_tag=job_tag,
    )
    remote_script_path = f"{scratch_dir}/job.sbatch"
    remote_summary_path = f"{scratch_dir}/summary.json"

    click.echo(f"[spartan] submitting {repo}@{branch} via {ssh_host}...")
    job_id = _submit_sbatch(ssh_host, script, remote_script_path)
    click.echo(f"[spartan] submitted as SLURM job {job_id}; polling...")

    final_state = _poll_job(
        ssh_host,
        job_id,
        timeout_s=timeout_s,
        interval_s=interval_s,
    )
    click.echo(f"[spartan] job {job_id} finished in state: {final_state}")

    blob = _fetch_summary(ssh_host, remote_summary_path)
    summary = _parse_summary(blob)
    click.echo(_format_summary(summary, repo=repo, branch=branch))
    raise SystemExit(_resolve_exit_code(summary))


__all__ = ["pytest_group", "run_cmd", "spartan_group"]
