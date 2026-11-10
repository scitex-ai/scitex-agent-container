"""Argument-only Spartan pytest plans; no SSH, UUID or job submission."""

import json

import click


def show_run_preview(
    *,
    repo: str,
    branch: str,
    ssh_host: str,
    reservation: str,
    timeout_s: int,
    interval_s: int,
    scratch_root: str,
) -> None:
    """Report admitted arguments without claiming remote state or readiness."""
    click.echo(
        json.dumps(
            {
                "action": "run",
                "target": "spartan-pytest",
                "dry_run": True,
                "scope": "arguments_only",
                "repo": repo,
                "branch": branch,
                "ssh_host": ssh_host,
                "reservation": reservation,
                "timeout_s": timeout_s,
                "poll_interval_s": interval_s,
                "scratch_root": scratch_root,
                "artifact_directory": None,
                "slurm_job_id": None,
                "unverified": [
                    "remote scratch expansion and filesystem",
                    "repository, branch and commit identity",
                    "SSH access, scheduler and reservation availability",
                    "upload, submission, completion and test results",
                ],
            },
            sort_keys=True,
        )
    )
