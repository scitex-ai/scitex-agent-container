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

import click

from .._state.host_config import host_interfaces, load
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
    """Canonical hostname + alias map + network interfaces."""
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
    """Peers configured under sac.yaml's ``peers:`` block."""
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
    """Check sac.yaml for misconfiguration; exit non-zero on errors."""
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
