"""Render commands: emit runtime-specific artifacts to stdout.

External consumers (orochi bootstrap, HPC operators, CI scripts) use these
to produce the exact text sac would submit/install, without needing to
invoke the full start/stop lifecycle. Pure-function ports on top of the
runtime layer.
"""

from __future__ import annotations

import click

from ..config import load_config, resolve_config
from ..config._resolve import resolve_with_prefix
from ..runtimes.slurm import render_attach_command, render_sbatch_script


@click.command("render-sbatch")
@click.argument("name_or_path", type=str)
def render_sbatch(name_or_path: str) -> None:
    """Print the sbatch wrapper text for ``runtime: slurm`` agents.

    Suitable for piping into a file and submitting manually, or for CI
    linting. Emits to stdout regardless of whether sbatch is installed
    on the current host.

    \b
    Example:
      $ sac template render-sbatch head-spartan
      $ sac template render-sbatch head-spartan > head-spartan.sbatch
    """
    config_path = resolve_with_prefix(name_or_path)
    cfg = load_config(config_path)
    if cfg.runtime != "slurm":
        raise click.ClickException(
            f"render-sbatch requires runtime: slurm; "
            f"'{cfg.name}' declares runtime: {cfg.runtime}"
        )
    click.echo(render_sbatch_script(cfg), nl=False)


@click.command("render-attach")
@click.argument("name_or_path", type=str)
@click.option(
    "--job-id",
    "job_id",
    default=None,
    help="SLURM jobid to attach to (defaults to sac's recorded jobid).",
)
def render_attach(name_or_path: str, job_id: str | None) -> None:
    """Print the ``srun --pty`` command that reattaches to the agent's tmux.

    Requires a live SLURM allocation. If ``--job-id`` is not given, reads
    the recorded jobid from sac's slurm state file.

    \b
    Example:
      $ sac template render-attach head-spartan
      $ sac template render-attach head-spartan --job-id 12345678
    """
    config_path = resolve_with_prefix(name_or_path)
    cfg = load_config(config_path)
    if cfg.runtime != "slurm":
        raise click.ClickException(
            f"render-attach requires runtime: slurm; "
            f"'{cfg.name}' declares runtime: {cfg.runtime}"
        )
    click.echo(render_attach_command(cfg, job_id=job_id))


__all__ = ["render_sbatch", "render_attach"]
