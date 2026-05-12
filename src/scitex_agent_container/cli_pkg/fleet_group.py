"""``sac fleet`` noun group — peer-aware multi-agent orchestration.

Bridges the gap between (a) agent specs that live as YAML on the
local machine and (b) Claude Code processes that need to run on a
beefier peer (e.g. spartan-bm198 with 64 CPU / 256 GB RAM).

Phase-1 surface (this file):

  * ``sac fleet launch SPECDIR --peer PEER`` — rsync every spec under
    ``SPECDIR/<name>/<name>.yaml`` to PEER's agents dir, then run
    ``sac agent start <name>`` on PEER for each one.

Phase-2 (deferred): ``sac fleet status``, ``sac fleet stop``,
A2A-bidirectional live control, multi-peer round-robin distribution.

The choice of "rsync the spec, then ssh-launch" over "launch a
container with embedded spec" mirrors how ``sac host exec`` already
works — sac never opens its own ports; OpenSSH provides the
transport.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click

from .._state.host_config import build_ssh_argv, load
from ._helpers import _json_flag, console


@click.group(
    "fleet",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def fleet_group() -> None:
    """Multi-agent orchestration across peers.

    \b
    Examples:
      $ sac fleet launch ~/specs/quality-fanout/ --peer spartan
      $ sac fleet launch --peer spartan --spec res-canary --no-rsync
      $ sac fleet launch ~/specs/ --peer spartan --dry-run
    """


def _discover_specs(specdir: Path) -> list[str]:
    """Return agent names found under ``SPECDIR``.

    Accepts either ``SPECDIR/<name>/<name>.yaml`` (the v3 dir-as-SSoT
    layout) or ``SPECDIR/<name>/spec.yaml`` (legacy). Both shapes
    appear in the wild.
    """
    names: list[str] = []
    for sub in sorted(specdir.iterdir()):
        if not sub.is_dir():
            continue
        if (sub / f"{sub.name}.yaml").is_file():
            names.append(sub.name)
        elif (sub / "spec.yaml").is_file():
            names.append(sub.name)
    return names


@fleet_group.command("launch")
@click.argument(
    "specdir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    required=False,
)
@click.option(
    "--peer",
    required=True,
    help="Target peer name (must be defined under peers: in config.yaml).",
)
@click.option(
    "--spec",
    "specs_explicit",
    multiple=True,
    help="One or more agent names to launch (alternative to SPECDIR).",
)
@click.option(
    "--remote-agents-dir",
    default="~/.scitex/agent-container/agents",
    show_default=True,
    help="Where to place specs on the peer.",
)
@click.option(
    "--no-rsync",
    is_flag=True,
    help="Skip rsync; assume specs already exist on the peer.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the plan; rsync + ssh are NOT executed.",
)
@click.option(
    "--remote-sac",
    default="sac",
    show_default=True,
    help="Path / alias of the `sac` binary on the peer (use `bash -lc 'sac …'` "
    "if the peer needs a login shell for PATH).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a JSON summary on stdout.",
)
@click.pass_context
def fleet_launch(
    ctx: click.Context,
    specdir: Path | None,
    peer: str,
    specs_explicit: tuple[str, ...],
    remote_agents_dir: str,
    no_rsync: bool,
    dry_run: bool,
    remote_sac: str,
    as_json: bool,
) -> None:
    """Rsync specs to PEER and run ``sac agent start <name>`` for each.

    SPECDIR contains agent layout dirs in either v3 form
    (``<name>/<name>.yaml``) or legacy (``<name>/spec.yaml``). Each
    discovered subdir becomes one ``sac agent start`` call on PEER.

    \b
    Examples:
      $ sac fleet launch ~/specs/quality/ --peer spartan
      $ sac fleet launch --peer spartan --spec res-canary --no-rsync
    """
    cfg = load()
    if peer not in cfg.peers:
        click.echo(
            f"error: peer '{peer}' is not defined in {cfg.source_path}.\n"
            f"Add it under peers: in config.yaml, then re-run.",
            err=True,
        )
        raise SystemExit(2)

    names: list[str] = []
    if specdir is not None:
        names.extend(_discover_specs(specdir))
    names.extend(specs_explicit)
    if not names:
        click.echo(
            "error: no specs found. Pass SPECDIR with <name>/<name>.yaml "
            "layouts, or use --spec NAME (repeatable).",
            err=True,
        )
        raise SystemExit(2)

    plan = {
        "peer": peer,
        "specdir": str(specdir) if specdir else None,
        "remote_agents_dir": remote_agents_dir,
        "names": names,
        "rsync": (specdir is not None) and not no_rsync,
        "dry_run": dry_run,
    }

    if dry_run:
        if _json_flag(ctx, as_json):
            click.echo(json.dumps({"plan": plan, "rows": []}, indent=2))
            return
        console.print("[bold]DRY RUN[/bold]")
        console.print(
            f"  rsync:           {specdir} -> {peer}:{remote_agents_dir}/"
            if plan["rsync"]
            else "  rsync:           (skipped)"
        )
        for n in names:
            console.print(f"  start on {peer}: {n}")
        return

    # ---- rsync the spec dir to the peer ----
    if plan["rsync"]:
        peer_spec = cfg.peers[peer]
        target = f"{peer_spec.ssh}:{remote_agents_dir}/"
        rsync_argv = [
            "rsync",
            "-az",
            "--mkpath",
            f"{specdir}/",
            target,
        ]
        if peer_spec.via:
            chain = peer_spec.jump_chain(cfg.peers)
            if chain:
                rsync_argv.insert(1, "-e")
                rsync_argv.insert(2, f"ssh -J {','.join(chain)}")
        if not as_json:
            console.print(
                f"[bold]rsync[/bold]  {specdir} -> {peer}:{remote_agents_dir}/"
            )
        rc = subprocess.run(rsync_argv).returncode
        if rc != 0:
            click.echo(f"error: rsync failed (exit {rc})", err=True)
            raise SystemExit(1)

    # ---- ssh-launch each agent on the peer ----
    rows: list[dict] = []
    for name in names:
        argv = build_ssh_argv(
            peer,
            [remote_sac, "agent", "start", name],
            cfg.peers,
        )
        proc = subprocess.run(argv, capture_output=True, text=True)
        rows.append(
            {
                "name": name,
                "peer": peer,
                "exit": proc.returncode,
                "stdout": proc.stdout.strip()[:400],
                "stderr": proc.stderr.strip()[:400],
            }
        )
        if not as_json:
            status = (
                "[green]ok[/green]"
                if proc.returncode == 0
                else f"[red]fail[/red] exit={proc.returncode}"
            )
            console.print(f"  start {name:<32} {status}")
            if proc.stderr.strip():
                console.print(f"    [dim]{proc.stderr.strip()[:200]}[/dim]")

    if _json_flag(ctx, as_json):
        click.echo(json.dumps({"plan": plan, "rows": rows}, indent=2))

    failures = sum(1 for r in rows if r["exit"] != 0)
    if failures:
        raise SystemExit(1)


# EOF
