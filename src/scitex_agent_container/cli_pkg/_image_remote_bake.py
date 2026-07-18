"""``sac image bake-remote`` — periodic Spartan-side SIF bake, pulled to the master.

OPERATOR DIRECTIVE (2026-07-17, verbatim): 「sif は最新版を定期焼きにしましょう。
spartan 側で。それでこちらには定期的に rsync する形で。どうでしょうか。cpu は
使わずに新しいものが得られると思います。」

Layering (operator ruling 2026-07-15 on card
``sac-periodic-sif-bake-keep3-rsync-to-consumers-20260715``):
scitex-container owns the NEUTRAL build/rotate primitives; sac is the
ADAPTER that knows the concrete WHERE (the Spartan lease), WHAT
(sac-base / sac-scitex), WHERE-TO (this host's containers dir) and
CADENCE (the ``sac.spartan-sif-bake`` timer). This module is that
adapter's CLI face. Nothing is deployed to Spartan: the bake script
ships in this wheel (``containers/spartan-sif-bake.sh``) and is PIPED
over ssh at run time; the build source is a dedicated https clone of
the public repo (operator constraint 「スパルタンに定義は一切置かない」 —
no specs, no tokens on Spartan).

The chain, per layer (each leg three-state and loud — see
``_remote_bake_core``):

1. BAKE   — remote script: resolve the lease BY NAME, ``srun --overlap``
            the build into it (never sbatch, never a login node), .def
            %post symbol gate at build time + artifact probe on the
            produced file, keep-N rotate, one ``SAC_BAKE_RESULT`` line.
2. PULL   — the master PULLS via rsync-over-ssh (Spartan cannot reach
            the master inbound) to a dot-prefixed ``.incoming-*`` name.
3. VERIFY — sha256 vs the remote sidecar + the SAME symbol probe via
            local ``apptainer exec`` on the received file.
4. SWAP   — atomic rename + atomic flips of BOTH live symlinks; a failed
            verify leaves the live image untouched.
5. PRUNE  — keep-N locally; live targets never pruned; names echoed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from . import _remote_bake_core as core
from ._remote_bake_core import (
    BakeVerdict,
    PullVerdict,
    RemoteBakeOutcome,
    parse_bake_result,
)


def run_remote_bake(
    *,
    host: str,
    layer: str,
    lease_name: str,
    remote_workdir: str,
    branch: str,
    retain: int,
    force: bool,
    timeout: int,
    script_path: Path | None = None,
) -> RemoteBakeOutcome:
    """Pipe the wheel-shipped bake script over ssh and parse its verdict."""
    script = script_path or core.BAKE_SCRIPT
    if not script.is_file():
        raise click.ClickException(f"bake script missing from wheel: {script}")
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        # A bake is a long-lived channel; without keepalives an idle NAT
        # dropped the FIRST live run's session mid-build (2026-07-17
        # 18:21) and the verdict was lost. Keepalives + the remote tee
        # keep the controller attached for the whole chain.
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=10",
        host,
        "bash -l -s -- "
        f"--layer {layer} --lease-name '{lease_name}' "
        f"--workdir '{remote_workdir}' --branch '{branch}' --retain {retain}"
        + (" --force" if force else ""),
    ]
    with script.open("rb") as fh:
        proc = core._run(
            args, stdin=fh, capture_output=True, text=True, timeout=timeout
        )
    # Stream the remote log through so journalctl / the operator sees the
    # whole bake, not a summary of it.
    if proc.stdout:
        click.echo(proc.stdout, nl=False)
    if proc.stderr:
        click.echo(proc.stderr, err=True, nl=False)
    outcome = parse_bake_result(proc.stdout or "", layer=layer)
    if proc.returncode != 0 and outcome.verdict in (
        BakeVerdict.BAKED,
        BakeVerdict.SKIPPED,
    ):
        # A green verdict line on a red exit is a contradiction — refuse it.
        return RemoteBakeOutcome(
            verdict=BakeVerdict.NO_RESULT,
            layer=layer,
            detail=(
                f"ssh rc={proc.returncode} contradicts verdict {outcome.verdict.value}"
            ),
        )
    return outcome


@click.command("bake-remote")
@click.option(
    "--host",
    default="spartan",
    show_default=True,
    help="ssh alias of the bake host.",
)
@click.option(
    "--layer",
    "layers",
    multiple=True,
    type=click.Choice(list(core.LAYERS)),
    help="Layer(s) to bake, in order. Default: base then scitex.",
)
@click.option(
    "--lease-name",
    default="spartan-cpu-32-cores-64-ram",
    show_default=True,
    help="Standing CPU lease job NAME (resolved remotely — never a job id).",
)
@click.option(
    "--remote-workdir",
    default="/data/gpfs/projects/punim2354/ywatanabe/sac-sif-bake",
    show_default=True,
    help=(
        "Dedicated bake workspace on the remote (clone + store; never the "
        "CI runners' checkout)."
    ),
)
@click.option("--branch", default="develop", show_default=True)
@click.option(
    "--retain",
    default=3,
    show_default=True,
    help="Generations kept per layer, both sides.",
)
@click.option(
    "--containers-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Local store (default: ~/.scitex/agent-container/containers, "
        "resolved at run time)."
    ),
)
@click.option(
    "--bake-only", is_flag=True, help="Remote bake + rotate only; no pull/swap."
)
@click.option("--force", is_flag=True, help="Rebake even if the source is unchanged.")
@click.option(
    "--ssh-timeout",
    default=7200,
    show_default=True,
    help="Hard cap (s) for one remote bake leg.",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Confirm (required non-interactively).",
)
def image_bake_remote(
    host: str,
    layers: tuple[str, ...],
    lease_name: str,
    remote_workdir: str,
    branch: str,
    retain: int,
    containers_dir: Path | None,
    bake_only: bool,
    force: bool,
    ssh_timeout: int,
    yes: bool,
) -> None:
    """Bake the SIF(s) on the remote lease, pull, re-verify, atomically swap.

    \b
    The periodic form (installed as the `sac.spartan-sif-bake` timer):
      $ sac image bake-remote --yes
    One layer, no local publish:
      $ sac image bake-remote --layer scitex --bake-only --yes
    """
    if not yes:
        click.echo("Refusing to bake remotely without --yes/-y.", err=True)
        raise SystemExit(2)
    layers = layers or core.LAYERS
    if containers_dir is None:
        # Resolved at RUN time, deliberately not an import-time constant
        # (an env-redirected $HOME must win — the import-time-constant
        # trap is a measured incident shape).
        containers_dir = Path.home() / ".scitex" / "agent-container" / "containers"

    failures: list[str] = []
    for layer in layers:
        click.echo(f"=== bake-remote: layer={layer} host={host} ===")
        try:
            outcome = run_remote_bake(
                host=host,
                layer=layer,
                lease_name=lease_name,
                remote_workdir=remote_workdir,
                branch=branch,
                retain=retain,
                force=force,
                timeout=ssh_timeout,
            )
        except subprocess.TimeoutExpired:
            failures.append(
                f"{layer}: remote bake exceeded --ssh-timeout={ssh_timeout}s"
            )
            click.echo(failures[-1], err=True)
            continue
        if outcome.verdict in (BakeVerdict.FAILED, BakeVerdict.NO_RESULT):
            failures.append(f"{layer}: bake {outcome.verdict.value} ({outcome.detail})")
            click.echo(failures[-1], err=True)
            continue
        click.echo(f"bake: {outcome.verdict.value} {outcome.sif}")
        if bake_only:
            continue
        pull = core.pull_and_publish(
            host=host, outcome=outcome, containers_dir=containers_dir, retain=retain
        )
        click.echo(f"publish: {pull.verdict.value} — {pull.detail}")
        if pull.verdict is PullVerdict.FAILED:
            failures.append(f"{layer}: publish FAILED ({pull.detail})")

    if failures:
        click.echo("bake-remote FAILED:", err=True)
        for f in failures:
            click.echo(f"  - {f}", err=True)
        raise SystemExit(1)
    click.echo("bake-remote: all layers ok")


__all__ = ["image_bake_remote", "run_remote_bake"]
