"""``sac host list`` — local host row + configured peers + the host registry.

Split out of ``host_group.py`` to keep that orchestrator under the per-file line
cap; the command is attached onto the ``host`` group at import time via
:func:`register_list_command` (same pattern as ``_account_list_cmd.py``).
"""

from __future__ import annotations

import json

import click

from .._state.host_config import host_interfaces, load
from ._helpers import _json_flag, console

# Virtual interfaces hidden from ``host list`` by default — docker
# bridges, k8s CNI bridges, VirtualBox, vEthernet pairs, tap devices.
# These rarely belong to "where can sac reach this host from?" and
# noise up the output for laptops that have docker installed even
# when it isn't in active use. Pass ``--all-interfaces`` to include.
_VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "flannel", "cni", "vboxnet", "tap")


def _filter_interfaces(ifaces: list, include_virtual: bool) -> list:
    if include_virtual:
        return ifaces
    return [
        i
        for i in ifaces
        if not any(i["iface"].startswith(p) for p in _VIRTUAL_IFACE_PREFIXES)
    ]


@click.command("list")
@click.option(
    "--all-interfaces",
    is_flag=True,
    default=False,
    help="Include virtual interfaces (docker, br-*, veth, etc.); default hides them.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_list(ctx: click.Context, all_interfaces: bool, as_json: bool) -> None:
    """Local host + peers configured under config.yaml's ``peers:`` block.

    The first row is always the local host (the machine running
    ``sac``); peers under ``config.yaml``'s ``peers:`` block follow.
    Virtual / container-managed interfaces (docker, br-*, veth, ...)
    are hidden by default — pass ``--all-interfaces`` to include them.

    \b
    Example:
      $ sac host list
      $ sac host list --json
      $ sac host list --all-interfaces   # include docker0, br-*, etc.
    """
    from .._state._host_ssh import resolve_peer_scitex_root
    from .._state.host_registry import registry_hosts

    cfg = load()
    src = cfg.source_path
    config_path = str(src) if (src and src.is_file()) else None
    local = {
        "name": cfg.canonical_host(),
        "scope": "local",
        "aliases": cfg.host.aliases,
        "interfaces": _filter_interfaces(host_interfaces(), all_interfaces),
    }
    peers = []
    for name, peer in sorted(cfg.peers.items()):
        peers.append(
            {
                "name": name,
                "scope": "peer",
                "ssh": peer.ssh,
                "via": list(peer.via),
                # Where a remote sac WILL write its state — resolved through
                # the scitex-dev registry (SSOT), inheriting through ``via:``
                # for glob compute nodes. None = home-relative default on the
                # peer (the registry root is ``~/.scitex``, or the host is
                # unregistered). Surfaced so the operator can SEE the answer
                # rather than discover it from a misplaced 1.4GB SIF.
                "scitex_root": resolve_peer_scitex_root(name, cfg.peers),
            }
        )
    registry = [
        {
            "name": h.name,
            "kind": h.kind,
            "ssh_alias": h.ssh_alias,
            "scitex_root": h.scitex_root,
        }
        for h in registry_hosts()
    ]
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "config_path": config_path,
                    "local": local,
                    "peers": peers,
                    "registry": registry,
                },
                indent=2,
            )
        )
        return
    # Header: where did our config come from?
    if config_path:
        console.print(f"[dim]config_path  {config_path}[/dim]")
    else:
        console.print(
            "[dim]config_path  (no config.yaml found — using built-in defaults)[/dim]"
        )
    # Local host always present.
    console.print(f"[bold]local[/bold]       {local['name']}")
    if local["aliases"]:
        for raw, alias in sorted(local["aliases"].items()):
            console.print(f"  alias       {raw}  ->  {alias}")
    for iface in local["interfaces"]:
        console.print(f"  {iface['iface']:<11} {iface['family']:<6} {iface['addr']}")
    # Peers.
    if peers:
        console.print("[bold]peers[/bold]")
        for r in peers:
            via = f"  via={','.join(r['via'])}" if r["via"] else ""
            console.print(f"  {r['name']:<11} ssh={r['ssh']}{via}")
            if r["scitex_root"]:
                console.print(
                    f"              scitex_root={r['scitex_root']} [dim](registry)[/dim]"
                )
    else:
        console.print("[dim](no peers configured)[/dim]")
    # Registry (scitex-dev hosts.yaml) — the SSOT sac resolves through.
    if registry:
        console.print("[bold]registry[/bold] [dim](scitex_dev.hosts — SSOT)[/dim]")
        for h in registry:
            alias = f"  ssh_alias={h['ssh_alias']}" if h["ssh_alias"] else ""
            console.print(
                f"  {h['name']:<11} {h['kind']:<12} scitex_root={h['scitex_root']}{alias}"
            )
    else:
        console.print(
            "[dim](no scitex-dev host registry found — "
            "peers fall back to the remote's ~/.scitex)[/dim]"
        )


def register_list_command(group: click.Group) -> None:
    """Attach the ``list`` command onto the ``host`` group."""
    group.add_command(host_list)
