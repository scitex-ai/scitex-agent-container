"""``sac sandbox`` noun group — sealed runs that are not agents.

``sac sandbox exec`` runs ONE plain command (no harness, no model key) with
the guarantees of a sealed agent (``spec.sandbox``): ``--network none`` +
the allowlisting egress proxy, the env seal, a fresh writable layer per run
on durable disk, and ``<run_dir>/out/outcome.json``. See
:mod:`scitex_agent_container._sandbox._exec` and ``docs/sandbox.md``.
"""

from __future__ import annotations

import json
import os
import shutil

import click

__all__ = ["sandbox_group"]


@click.group(
    "sandbox",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def sandbox_group() -> None:
    """Sealed runs of plain commands (no agent harness, no model key).

    \b
    Examples:
      $ sac sandbox exec --image ./img.sif --run-root /scratch/$USER/runs \\
            --timeout 60 -- python3 -c 'print(1+1)'
    """


@sandbox_group.command(
    "exec",
    context_settings={
        "ignore_unknown_options": True,
        "allow_interspersed_args": False,
        "help_option_names": ["-h", "--help"],
    },
)
@click.option("--image", required=True, help="SIF image the command runs in.")
@click.option(
    "--run-root",
    required=True,
    help="Durable dir for run layers (/tmp, /var/tmp, /dev/shm, /run refused).",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    required=True,
    help="Wall-clock limit in seconds; on expiry the run is stopped (exit 124).",
)
@click.option(
    "--egress-allow",
    multiple=True,
    metavar="HOST:PORT",
    help="Reachable host:port (repeatable). Default: no egress at all.",
)
@click.option(
    "--bind",
    "binds",
    multiple=True,
    metavar="SRC:DST[:ro|rw]",
    help="Host path to mount (repeatable); read-only unless ':rw'.",
)
@click.option(
    "--env-allow",
    multiple=True,
    metavar="NAME",
    help="Caller env var passed through by name (repeatable).",
)
@click.option(
    "--size-mb",
    type=int,
    default=4_096,
    show_default=True,
    help="Cap on the run's writable layers (overlay + $HOME + /tmp).",
)
@click.option(
    "--after",
    type=click.Choice(["archive", "remove"]),
    default="archive",
    show_default=True,
    help="Keep (archive) or delete (remove) the run's layers; out/ is always kept.",
)
@click.option(
    "--name",
    default="exec",
    show_default=True,
    help="Run group: <run-root>/<name>/<run_id>.",
)
@click.option("--pwd", default=None, help="Working directory inside the container.")
@click.option("--dry-run", is_flag=True, help="Print the plan as JSON; run nothing.")
@click.argument("argv", nargs=-1, type=click.UNPROCESSED, required=True)
def sandbox_exec(
    image, run_root, timeout_s, egress_allow, binds, env_allow, size_mb, after,
    name, pwd, dry_run, argv,
) -> None:  # fmt: skip
    """Run ARGV sealed: no network but --egress-allow, no host env but --env-allow.

    The command's stdout/stderr stream to this terminal AND to
    <run_dir>/out/stdout.log / stderr.log; sac exits with the command's
    status (124 on --timeout, 96 when the run could not start).
    <run_dir>/out/outcome.json records timestamps, exit, image sha256, SAC
    version, env NAMES, egress allowed/blocked logs and files written.

    \b
    Examples:
      $ sac sandbox exec --image ./img.sif --run-root /scratch/$USER/runs \\
            --timeout 600 --bind $PWD/bundle:/bundle --pwd /bundle \\
            --egress-allow pypi.org:443 \\
            --egress-allow files.pythonhosted.org:443 -- clew bundle rerun .
    """
    from .._sandbox._exec import build_request, caller_env, plan, run_exec
    from .._sandbox._run import SandboxRunError

    try:
        req = build_request(
            # A plan needs no runtime; a run refuses without one.
            apptainer=shutil.which("apptainer") or ("apptainer" if dry_run else None),
            image=image,
            argv=list(argv),
            run_root=run_root,
            timeout_s=timeout_s,
            egress_allow=egress_allow,
            binds=binds,
            env_allow=env_allow,
            size_mb=size_mb,
            after=after,
            name=name,
            pwd=pwd,
        )
        caller_env(req, os.environ)  # a value apptainer would rewrite is refused here
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    if dry_run:
        click.echo(json.dumps(plan(req), indent=2))
        return
    try:
        result = run_exec(req, os.environ)
    except (SandboxRunError, ValueError, OSError) as exc:
        click.echo(f"[sac-sandbox] {exc}", err=True)
        raise SystemExit(96) from exc
    click.echo(
        f"[sac-sandbox] exit {result.status}; outcome: "
        f"{result.run_dir / 'out' / 'outcome.json'}",
        err=True,
    )
    raise SystemExit(result.status)
