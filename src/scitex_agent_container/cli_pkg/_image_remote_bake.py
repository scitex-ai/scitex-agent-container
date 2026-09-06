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
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import click

from ._bake_lock import (
    BakeAlreadyRunningError,
    acquire_bake_lock,
    release_bake_lock,
)

from . import _remote_bake_core as core
from ._remote_bake_core import (
    BakeVerdict,
    PullVerdict,
    RemoteBakeOutcome,
    parse_bake_result,
)


def _first_line(text: str) -> str:
    """First non-blank line — the headline summary of a multi-line reason."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "(no reason given)"


def _installed_version() -> str:
    """Version of the sac actually imported here (never a hardcoded value)."""
    try:
        return metadata.version("scitex-agent-container")
    except metadata.PackageNotFoundError:  # pragma: no cover - source checkout
        return "unknown"


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
    # PREFLIGHT — the script we are ABOUT TO PIPE is the one that runs, and
    # it is read off the INSTALLED wheel, not the checkout. PR #771's fix
    # lived in develop for a full day while every bake still ran the
    # pre-fix bytes from a cache-hit wheel under an unchanged version
    # string. Refuse to spend an hour of lease on a script we can already
    # see is broken, and say exactly which file and which lines.
    source = script.read_text(encoding="utf-8")
    offenders = core.unguarded_srun_invocations(source)
    if offenders:
        return RemoteBakeOutcome(
            verdict=BakeVerdict.FAILED,
            layer=layer,
            detail=core.stale_bake_script_error(
                script=script, offenders=offenders, version=_installed_version()
            ),
        )
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        # A bake is a long-lived (~1h) channel, so keep the controller
        # attached rather than relying on an idle NAT's goodwill.
        # NOTE: these keepalives were originally added believing an idle
        # drop was what lost the runs of 2026-07-17..19. It was not — the
        # channel was healthy and ssh exited 0 every time; an unguarded
        # `srun` on the remote was EATING THIS SCRIPT off the same stdin
        # pipe (see the STDIN RULE in containers/spartan-sif-bake.sh).
        # They are kept because a long channel wants them anyway, not
        # because they fix anything.
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
                f"ssh rc={proc.returncode} contradicts verdict "
                f"{outcome.verdict.value}\n"
                + core.describe_remote_failure(
                    verdict=BakeVerdict.NO_RESULT,
                    script=script,
                    ssh_rc=proc.returncode,
                    stdout=proc.stdout or "",
                    stderr=proc.stderr or "",
                )
            ),
        )
    if outcome.verdict in (BakeVerdict.FAILED, BakeVerdict.NO_RESULT):
        # Carry the EVIDENCE, not just the label. The remote's exit status
        # and the tail of its stderr are the whole diagnosis, and both were
        # being thrown away here — which is how six silent runs went by
        # with nothing to read but the word FAILED.
        described = core.describe_remote_failure(
            verdict=outcome.verdict,
            script=script,
            ssh_rc=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
        remote_reason = outcome.detail or "(remote gave no reason)"
        return replace(outcome, detail=f"{remote_reason}\n{described}")
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

    # ONE BAKE AT A TIME PER CONTAINERS DIR. Measured 2026-09-06/07: a
    # supervisor restart began a SECOND ~7.6G pull of the same artifact
    # while the first was mid-transfer; both used `rsync --partial`, so
    # the newcomer resumed from the incumbent's partial AND wrote its own
    # temp. scitex-compute-03 went 17G free -> 3.8G with three concurrent
    # pulls, and earlier the same evening the identical loop drove the
    # disk to zero and the ecosystem supervisor to 273 restarts.
    #
    # DECLINING IS EXIT 0, NOT A FAILURE. The caller is a supervised job
    # under Restart=always: a non-zero exit here would be read as a crash
    # and restarted, which is precisely the loop this prevents. Nothing
    # went wrong when a second bake declines — the first one is doing the
    # work.
    lock_dir = Path.home() / ".scitex" / "agent-container" / "runtime"
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Held for the lifetime of this one-shot command. The kernel
        # releases the flock when the process exits — including SIGKILL
        # and OOM — so a crashed bake never jams the pipeline and no
        # stale-lock reconciliation is needed. Bound to a name so the fd
        # is not garbage-collected (closing it would release the lock).
        _bake_lock = acquire_bake_lock(
            containers_dir=containers_dir, lock_dir=lock_dir
        )
    except BakeAlreadyRunningError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(0) from exc

    try:
        failures: list[str] = []
        # One-line-per-failure summaries for the headline. The full reason is
        # multi-line by design (remote stderr, remedy), and a headline that
        # ends at the colon is unreadable in a journal: `bake-remote FAILED:`
        # matched a grep and told the reader NOTHING.
        failure_heads: list[str] = []
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
                failure_heads.append(
                    f"{layer}: remote bake exceeded --ssh-timeout={ssh_timeout}s"
                )
                failures.append(failure_heads[-1])
                click.echo(failures[-1], err=True)
                continue
            if outcome.verdict in (BakeVerdict.FAILED, BakeVerdict.NO_RESULT):
                failure_heads.append(
                    f"{layer}: bake {outcome.verdict.value} — {_first_line(outcome.detail)}"
                )
                failures.append(f"{layer}: bake {outcome.verdict.value}\n{outcome.detail}")
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
                failure_heads.append(
                    f"{layer}: publish FAILED — {_first_line(pull.detail)}"
                )
                failures.append(f"{layer}: publish FAILED ({pull.detail})")

        if failures:
            # The headline itself names what broke. Anything less makes the
            # whole message invisible to the grep that a tired operator at
            # 03:00 actually runs.
            click.echo(f"bake-remote FAILED: {'; '.join(failure_heads)}", err=True)
            for f in failures:
                click.echo(f"  - {f}", err=True)
            raise SystemExit(1)
        click.echo("bake-remote: all layers ok")
    finally:
        # Explicit release so a second invocation IN THE SAME PROCESS
        # (CliRunner, a future in-process caller) can acquire. Relying
        # on process exit alone was measured wrong here: the existing
        # CliRunner tests share one process, so the first invocation
        # held the lock and every later one silently declined with
        # exit 0. The kernel still covers dirty exit.
        release_bake_lock(_bake_lock)


__all__ = ["image_bake_remote", "run_remote_bake"]
