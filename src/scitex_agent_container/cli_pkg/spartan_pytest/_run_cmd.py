"""Click surface for ``sac pytest spartan submit-run``.

Composes the pure renderer (``_render``), the summary parser
(``_summary``), and the ssh/SLURM IO wrappers (``_ssh``) into the
operator-facing CLI command.  See ``__init__.py`` for the full
architecture + Phase-2 follow-ups.
"""

from __future__ import annotations

import uuid

import click

from .._helpers import HelpRecursiveGroup, renamed_redirect
from ._render import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_RESERVATION,
    DEFAULT_SSH_HOST,
    DEFAULT_TIMEOUT_S,
    _render_sbatch_script,
    _validate_header_fields,
)
from ._ssh import _fetch_summary, _poll_job, _resolve_scratch_root, _submit_sbatch
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


@spartan_group.command(name="submit-run")
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
    type=click.IntRange(min=1),
    default=DEFAULT_TIMEOUT_S,
    help=f"Max seconds to wait for the SLURM job (default: {DEFAULT_TIMEOUT_S}).",
)
@click.option(
    "--poll-interval",
    "interval_s",
    type=click.IntRange(min=1),
    default=DEFAULT_POLL_INTERVAL_S,
    help=f"Seconds between squeue polls (default: {DEFAULT_POLL_INTERVAL_S}).",
)
@click.option(
    "--scratch-root",
    "scratch_root",
    default="$SCRATCH/sac-pytest",
    help="Spartan scratch root for clones + summary.json "
    "(default: $SCRATCH/sac-pytest; leading SCRATCH token resolved on SSH host).",
)
@click.option("--dry-run", is_flag=True, help="Preview arguments as JSON without SSH.")
@click.option(
    "-y", "--yes", is_flag=True, help="Approve remote upload and job submission."
)
def run_cmd(
    target: str,
    ssh_host: str,
    reservation: str,
    timeout_s: int,
    interval_s: int,
    scratch_root: str,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Submit + collect a Spartan pytest job for ``REPO@BRANCH``.

    \b
    REPO accepts a full git URL or ``owner/name`` shorthand (resolved
    against github.com on Spartan).

    \b
    Examples:
      $ sac pytest spartan submit-run owned/example@main --dry-run
      $ sac pytest spartan submit-run ywatanabe1989/sac@develop --yes
      $ sac pytest spartan submit-run git@github.com:me/repo.git@feature/x --yes --reservation sapphire
    """
    repo, branch = _split_repo_at_branch(target)
    # SLURM caps job names at ~64 chars so we trim the branch suffix
    # aggressively to keep ``sac-pytest-<tag>`` well under the limit.
    job_tag = branch.replace("/", "-")[:32]
    try:
        _validate_header_fields(
            reservation=reservation, scratch_dir=scratch_root, job_tag=job_tag
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    if dry_run:
        from ._preview import show_run_preview

        show_run_preview(
            repo=repo,
            branch=branch,
            ssh_host=ssh_host,
            reservation=reservation,
            timeout_s=timeout_s,
            interval_s=interval_s,
            scratch_root=scratch_root,
        )
        return
    from ._approval import require_run_approval

    require_run_approval(yes)
    scratch_root = _resolve_scratch_root(ssh_host, scratch_root)
    # Allocate the namespace before upload: a scheduler job ID is not yet
    # available, and equal wall-clock milliseconds are not distinct runs.
    submission_id = uuid.uuid4().hex
    scratch_dir = f"{scratch_root}/{submission_id}"
    try:
        script = _render_sbatch_script(
            repo=repo,
            branch=branch,
            reservation=reservation,
            scratch_dir=scratch_dir,
            job_tag=job_tag,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    click.echo(f"[spartan] planned artifact directory: {scratch_dir}")
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
    click.echo(f"[spartan] job {job_id} accounting state: {final_state}")

    blob = _fetch_summary(ssh_host, remote_summary_path)
    summary = _parse_summary(blob)
    if summary.pytest_exit is None or summary.capture_exit is None:
        raise click.ClickException(
            "Spartan pytest cannot qualify this summary: it is invalid or lacks "
            "pytest/capture exit evidence. Inspect the retained job logs."
        )
    if summary.slurm_job_id != job_id:
        raise click.ClickException(
            "Spartan pytest cannot qualify this summary: missing or mismatched "
            "scheduler job ID. Inspect the retained job logs."
        )
    if summary.all_passed and (final_state != "COMPLETED" or summary.passed == 0):
        raise click.ClickException(
            "Spartan pytest cannot PASS without COMPLETED accounting and "
            "at least one passing test. Inspect the retained job logs."
        )
    click.echo(_format_summary(summary, repo=repo, branch=branch))
    raise SystemExit(_resolve_exit_code(summary))


_legacy_run = renamed_redirect(
    run_cmd, new_path="sac pytest spartan submit-run", old_path="sac pytest spartan run"
)
_legacy_run.name = "run"
_legacy_run.hidden = True
spartan_group.add_command(_legacy_run)


__all__ = ["pytest_group", "run_cmd", "spartan_group"]
