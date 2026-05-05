"""``sac host`` noun group — local host identity + peer routing (F-CS12).

Phase 1 surface (read-only inspection):

  * ``sac host show`` — canonical name + aliases + interfaces
  * ``sac host list`` — configured peers with their ssh routes

Phase 2 (deferred): ``sac host probe <peer>``, ``sac host exec
<peer> -- <args>``, the ``--on <peer>`` global flag, and fold-ins
of ``sac network probe`` / ``sac installation boot``.
"""

from __future__ import annotations

import json
import subprocess

import click

from .._state.host_config import build_ssh_argv, host_interfaces, load
from ._helpers import _json_flag, console


@click.group(
    "host",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def host_group() -> None:
    """Local host identity and peer routing for sac.

    \b
    Examples:
      $ sac host show
      $ sac host list
    """


@host_group.command("show")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_show(ctx: click.Context, as_json: bool) -> None:
    """Canonical hostname + alias map + network interfaces.

    \b
    Example:
      $ sac host show
      $ sac host show --json
    """
    cfg = load()
    src = cfg.source_path
    config_path = str(src) if (src and src.is_file()) else None
    payload = {
        "canonical": cfg.canonical_host(),
        "config_path": config_path,
        "aliases": cfg.host.aliases,
        "fallback": cfg.host.fallback,
        "interfaces": host_interfaces(),
    }
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
        return
    console.print(f"[bold]canonical[/bold]   {payload['canonical']}")
    if payload["config_path"]:
        console.print(f"[dim]config_path  {payload['config_path']}[/dim]")
    if cfg.host.aliases:
        console.print("[bold]aliases[/bold]")
        for raw, alias in sorted(cfg.host.aliases.items()):
            console.print(f"  {raw}  ->  {alias}")
    if payload["interfaces"]:
        console.print("[bold]interfaces[/bold]")
        for iface in payload["interfaces"]:
            console.print(
                f"  {iface['iface']:<10} {iface['family']:<6} {iface['addr']}"
            )


@host_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_list(ctx: click.Context, as_json: bool) -> None:
    """Peers configured under sac.yaml's ``peers:`` block.

    \b
    Example:
      $ sac host list
      $ sac host list --json
    """
    cfg = load()
    rows = []
    for name, peer in sorted(cfg.peers.items()):
        rows.append(
            {
                "name": name,
                "ssh": peer.ssh,
                "via": list(peer.via),
            }
        )
    if _json_flag(ctx, as_json):
        click.echo(json.dumps({"peers": rows}, indent=2))
        return
    if not rows:
        console.print("[dim](no peers configured in sac.yaml)[/dim]")
        return
    console.print("[bold]peers[/bold]")
    for r in rows:
        via = f"  via={','.join(r['via'])}" if r["via"] else ""
        console.print(f"  {r['name']:<12} ssh={r['ssh']}{via}")


@host_group.command("validate")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_validate(ctx: click.Context, as_json: bool) -> None:
    """Check sac.yaml for misconfiguration; exit non-zero on errors.

    \b
    Example:
      $ sac host validate
      $ sac host validate --json
    """
    cfg = load()
    errors = cfg.validate()
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "source": str(cfg.source_path) if cfg.source_path else None,
                    "errors": errors,
                },
                indent=2,
            )
        )
    else:
        if errors:
            for e in errors:
                console.print(f"[red]error:[/red] {e}")
        else:
            console.print("[green]ok[/green]  sac.yaml is valid")
    if errors:
        raise SystemExit(1)


def split_on_flag(argv: list[str]) -> tuple[str | None, list[str]]:
    """If ``argv`` contains ``--on PEER`` (or ``--on=PEER``), strip the
    flag and return ``(peer, remaining_argv)``. Otherwise ``(None, argv)``.

    Used by the entry-point shim before Click parses anything: when the
    user typed ``sac --on spartan agent list``, we want ``agent list``
    to run on spartan with no further sac-side processing. Click
    normally consumes ``--on`` during parsing of ``main``, but the
    flag must be honoured BEFORE the subcommand is dispatched.
    """
    out: list[str] = []
    peer: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--on":
            if i + 1 >= len(argv):
                raise click.UsageError("--on requires a peer name")
            peer = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--on="):
            peer = tok.split("=", 1)[1]
            i += 1
            continue
        out.append(tok)
        i += 1
    return peer, out


def dispatch_remote(peer: str, argv: list[str], ssh_argv0: str = "sac") -> int:
    """Run ``sac <argv>`` on ``peer`` via ssh; return the remote exit code.

    Used when the entry point detects ``--on PEER`` in sys.argv. The
    remote command is always ``sac <argv>`` — orchestrators rely on
    that prefix so peers can be set up to alias ``sac`` to the right
    binary path on hosts where it isn't on $PATH.
    """
    cfg = load()
    if peer not in cfg.peers:
        click.echo(
            f"error: --on peer '{peer}' is not defined in {cfg.source_path}.\n"
            f"Add it under peers: in sac.yaml, then re-run.",
            err=True,
        )
        return 2
    ssh_argv = build_ssh_argv(peer, [ssh_argv0, *argv], cfg.peers)
    proc = subprocess.run(ssh_argv)
    return proc.returncode


@host_group.command(
    "exec",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
)
@click.argument("peer", required=True)
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
def host_exec(peer: str, argv: tuple[str, ...]) -> None:
    """Run a command on PEER over ssh, multi-hop aware.

    \b
    Example:
      $ sac host exec spartan -- agent list --json
      $ sac host exec bm198 -- sac db export --since 2026-05-01

    PEER must be defined under sac.yaml's ``peers:`` block. The peer's
    ``via:`` chain renders into ssh's ``-J`` flag automatically; sac
    never opens a port. Stdio is inherited so streaming output works.
    """
    cfg = load()
    if peer not in cfg.peers:
        click.echo(
            f"error: peer '{peer}' is not defined in {cfg.source_path}.\n"
            f"Add it under peers: in sac.yaml, then re-run.",
            err=True,
        )
        raise SystemExit(2)
    if not argv:
        click.echo(
            "error: no command supplied. Try: sac host exec PEER -- <cmd>", err=True
        )
        raise SystemExit(2)
    ssh_argv = build_ssh_argv(peer, list(argv), cfg.peers)
    proc = subprocess.run(ssh_argv)
    raise SystemExit(proc.returncode)


@host_group.command("probe")
@click.argument("peer", required=True)
@click.option("--timeout", type=int, default=10, help="ssh ConnectTimeout seconds.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_probe(ctx: click.Context, peer: str, timeout: int, as_json: bool) -> None:
    """One ssh round-trip to PEER; report reachability + remote canonical hostname.

    Cheap liveness check used by orchestrators (and eventually by
    F-CS14's pull cron). Runs ``sac host show --json`` on the remote
    so the report contains the peer's reported canonical name plus
    a measured round-trip duration.

    \b
    Example:
      $ sac host probe spartan-bm198
      $ sac host probe nas --timeout 10 --json
    """
    import time

    cfg = load()
    if peer not in cfg.peers:
        msg = f"peer '{peer}' is not defined in sac.yaml"
        if _json_flag(ctx, as_json):
            click.echo(json.dumps({"peer": peer, "reachable": False, "error": msg}))
        else:
            click.echo(f"[red]error:[/red] {msg}", err=True)
        raise SystemExit(2)

    remote_argv = ["sac", "host", "show", "--json"]
    ssh_argv = build_ssh_argv(
        peer,
        remote_argv,
        cfg.peers,
        extra_opts=["-o", f"ConnectTimeout={timeout}"],
    )
    started = time.monotonic()
    proc = subprocess.run(ssh_argv, capture_output=True, text=True)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    reachable = proc.returncode == 0
    remote_canonical: str | None = None
    if reachable:
        try:
            remote_canonical = json.loads(proc.stdout).get("canonical")
        except (
            ValueError,
            KeyError,
        ):  # stx-allow: fallback (reason: malformed remote output tolerated)
            remote_canonical = None
    payload = {
        "peer": peer,
        "reachable": reachable,
        "elapsed_ms": elapsed_ms,
        "remote_canonical": remote_canonical,
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip() if proc.stderr else "",
    }
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
    elif reachable:
        console.print(
            f"[green]ok[/green]  {peer}  {elapsed_ms}ms  remote={remote_canonical}"
        )
    else:
        console.print(f"[red]unreachable[/red]  {peer}  exit={proc.returncode}")
        if proc.stderr.strip():
            console.print(f"[dim]{proc.stderr.strip()[:200]}[/dim]")
    if not reachable:
        raise SystemExit(1)
