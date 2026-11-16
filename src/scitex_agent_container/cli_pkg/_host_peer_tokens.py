"""CLI commands for the separate per-host bearer registry."""

from __future__ import annotations

import json

import click
from scitex_dev.ecosystem import CliHelp, Example, SpecCommand

from .._logging import render_rich
from ._helpers import _json_flag

_ADD_HELP = CliHelp(
    summary="Register a peer host's listen bearer with explicit approval.",
    description=(
        "--dry-run prints supplied arguments without inspecting the registry "
        "or validating the target. It takes precedence over --yes. Applying "
        "requires --yes/-y and retains host/path validation and private writes. "
        "Token values are omitted from output.",
    ),
    examples=(
        Example("{prog} host add-peer host-a TOKEN --dry-run --json"),
        Example("{prog} host add-peer host-a TOKEN --yes"),
    ),
    exit_codes={
        0: "Preview printed or credential written.",
        1: "Registry I/O failed.",
        2: "Invalid usage or unsafe target.",
    },
    version_of="scitex-agent-container",
)

_REMOVE_HELP = CliHelp(
    summary="Remove a peer host's listen bearer with explicit approval.",
    description=(
        "--dry-run prints supplied arguments without inspecting the registry "
        "or checking whether a credential exists. It takes precedence over "
        "--yes. Applying requires --yes/-y; removing an absent peer is a no-op.",
    ),
    examples=(
        Example("{prog} host remove-peer host-a --dry-run --json"),
        Example("{prog} host remove-peer host-a --yes"),
    ),
    exit_codes={
        0: "Preview printed, credential removed or already absent.",
        1: "Registry I/O failed.",
        2: "Invalid usage or unsafe target.",
    },
    version_of="scitex-agent-container",
)


def _preview(ctx, as_json, action, peer_host):
    report = {
        "dry_run": True,
        "action": action,
        "peer_host": peer_host,
        "validated": False,
        "token_value_included": False,
    }
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(report, indent=2))
    else:
        click.echo(
            f"Would {action} {peer_host!r}; arguments only, target not validated; token value omitted."
        )


# WI-4 Q4(b) — peer bearer-token registry. ``sac host add-peer`` /
# ``list-peers`` / ``remove-peer`` manage the
# ``peer-tokens/<peer-host>.token`` files the cross-host forwarder
# consults to authenticate at a destination ``sac listen``. Each
# entry is per-host scoped, so leaking one host's token compromises
# only that host (the per-host blast radius the lead asked for).


@click.command("add-peer", cls=SpecCommand, help_spec=_ADD_HELP)
@click.argument("peer_host")
@click.argument("token")
@click.option(
    "--dry-run", is_flag=True, help="Preview arguments without registry access."
)
@click.option("--yes", "-y", is_flag=True, help="Approve the credential write.")
@click.option(
    "--json", "as_json", is_flag=True, help="Emit metadata as JSON; omit token values."
)
@click.pass_context
def host_add_peer(
    ctx: click.Context,
    peer_host: str,
    token: str,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Register a peer bearer after approval, or preview arguments only."""
    if dry_run:
        _preview(ctx, as_json, "add-peer", peer_host)
        return
    if not yes:
        raise click.UsageError(
            "Credential writes require --yes/-y; use --dry-run to preview arguments."
        )

    from .._listen.peer_tokens import PeerTokenError, write_peer_token

    if not peer_host:
        raise click.UsageError("PEER_HOST must be non-empty")
    if not token:
        raise click.UsageError("TOKEN must be non-empty")
    try:
        dst = write_peer_token(peer_host=peer_host, token=token)
    except PeerTokenError as exc:
        raise click.UsageError(str(exc)) from exc
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {"action": "add-peer", "peer_host": peer_host, "path": str(dst)},
                indent=2,
            )
        )
    else:
        render_rich(f"[green]ok[/green]  wrote {dst}", __name__)


@click.command("list-peers")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit peer names as JSON, never token values.",
)
@click.pass_context
def host_list_peers(ctx: click.Context, as_json: bool) -> None:
    """List the peer hosts that have a registered listen bearer.

    Token values are NEVER printed — only the peer-host names. To
    see the on-disk file paths run
    ``ls -la ~/.scitex/agent-container/peer-tokens/``.

    \b
    Examples:
      sac host list-peers
      sac host list-peers --json
      sac --json host list-peers
    """
    from .._listen.peer_tokens import (
        PeerTokenError,
        default_peer_tokens_dir,
        list_peer_hosts,
    )

    tdir = default_peer_tokens_dir()
    try:
        hosts = list_peer_hosts()
    except PeerTokenError as exc:
        raise click.ClickException(str(exc)) from exc
    if _json_flag(ctx, as_json):
        click.echo(json.dumps({"peer_hosts": hosts}, indent=2))
        return
    if not hosts:
        render_rich(
            f"no peer tokens registered (dir: {tdir}). "
            "Add one with: sac host add-peer <host> <token> --yes",
            __name__,
        )
        return
    render_rich(f"peer-tokens dir: {tdir}", __name__)
    for h in hosts:
        render_rich(f"  {h}", __name__)


@click.command("remove-peer", cls=SpecCommand, help_spec=_REMOVE_HELP)
@click.argument("peer_host")
@click.option(
    "--dry-run", is_flag=True, help="Preview arguments without registry access."
)
@click.option("--yes", "-y", is_flag=True, help="Approve removing the credential.")
@click.option("--json", "as_json", is_flag=True, help="Emit removal metadata as JSON.")
@click.pass_context
def host_remove_peer(
    ctx: click.Context, peer_host: str, dry_run: bool, yes: bool, as_json: bool
) -> None:
    """Remove one peer bearer after approval; absent credentials are a no-op."""
    if dry_run:
        _preview(ctx, as_json, "remove-peer", peer_host)
        return
    if not yes:
        raise click.UsageError(
            "Credential removal requires --yes/-y; use --dry-run to preview arguments."
        )

    from .._listen.peer_tokens import PeerTokenError, peer_token_path

    if not peer_host:
        raise click.UsageError("PEER_HOST must be non-empty")
    try:
        path = peer_token_path(peer_host)
    except PeerTokenError as exc:
        raise click.UsageError(str(exc)) from exc
    removed = path.exists()
    if removed:
        path.unlink()
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "action": "remove-peer",
                    "peer_host": peer_host,
                    "path": str(path),
                    "removed": removed,
                },
                indent=2,
            )
        )
    elif removed:
        render_rich(f"[green]ok[/green]  removed {path}", __name__)
    else:
        render_rich(f"no peer token to remove at {path}", __name__)


def register_peer_token_commands(group: click.Group) -> None:
    """Attach the same command objects to the host group."""
    for command in (host_add_peer, host_list_peers, host_remove_peer):
        group.add_command(command)
