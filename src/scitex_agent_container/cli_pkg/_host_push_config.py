"""``sac host push-config`` — generated client config, master → peer.

Sibling of :mod:`._host_sync` (one-way code sync) and registered onto
``host_group`` the same way. Where ``host sync`` moves CODE, this verb
moves the peer's minimal GENERATED client config (ADR-0021: the master's
config.yaml is the fleet's only hand-edited topology file).

Rendering rule, inherited verbatim from the sibling: **there is no quiet
success path.** Every peer prints its verdict and its evidence, a
refusal prints the offending diff and names the next command, and an
UNKNOWN peer exits non-zero rather than passing as clean.
"""

from __future__ import annotations

import json

import click

from .._hostsync import (
    ConfigVerdict,
    PushConfigResult,
    check_config_peer,
    push_config_peer,
    syncable_peers,
)
from .._state.host_config import load as _load_cfg
from ._helpers import _json_flag, console

# Colour per verdict. Refusals and unknowns are loud on purpose.
_STYLE = {
    ConfigVerdict.CURRENT: ("green", "current"),
    ConfigVerdict.STALE_GENERATED: ("yellow", "STALE"),
    ConfigVerdict.HAND_EDITED: ("red", "HAND-EDITED"),
    ConfigVerdict.ABSENT: ("yellow", "ABSENT"),
    ConfigVerdict.UNDETERMINED: ("magenta", "UNKNOWN"),
}


def _evidence(text: str) -> None:
    """One evidence line WITHOUT rich's word-wrap (grep-able from cron logs)."""
    console.print(text, soft_wrap=True)


def _print_result(result: PushConfigResult, *, show_diff: bool) -> None:
    colour, label = _STYLE[result.verdict]
    _evidence(
        f"[{colour}]{label:<12}[/{colour}] {result.peer}  "
        f"[dim]action: {result.action}[/dim]"
    )
    for line in result.detail.splitlines():
        _evidence(f"    {line}" if line.strip() else "")
    if result.backup:
        _evidence(f"    backup     {result.backup}")
    # A HAND-EDITED refusal ALWAYS shows its diff — nobody should have to
    # re-run with --diff to see what a refusal was protecting.
    must_show = result.verdict is ConfigVerdict.HAND_EDITED
    if result.diff and (show_diff or must_show):
        for line in result.diff.splitlines():
            _evidence(f"    [dim]{line}[/dim]")
    console.print("")


@click.command("push-config")
@click.argument("peer", required=False)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    default=False,
    help="READ-ONLY: classify each peer's config, exit non-zero on drift.",
)
@click.option(
    "--all",
    "all_peers",
    is_flag=True,
    default=False,
    help="Every peer in config.yaml (skips glob patterns and the centre itself).",
)
@click.option(
    "--diff",
    "show_diff",
    is_flag=True,
    default=False,
    help="Print the unified diff (remote vs rendered) for any non-current peer.",
)
@click.option(
    "--adopt",
    is_flag=True,
    default=False,
    help=(
        "Replace ONE hand-edited peer config, backing it up on the peer first "
        "(config.yaml.pre-adopt-<UTC>). Only valid for a HAND-EDITED verdict."
    ),
)
@click.option("--timeout", type=int, default=30, help="Per-ssh wall-clock cap (s).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON (for cron).")
@click.pass_context
def host_push_config(
    ctx: click.Context,
    peer: str | None,
    check_only: bool,
    all_peers: bool,
    show_diff: bool,
    adopt: bool,
    timeout: int,
    as_json: bool,
) -> None:
    """Push the GENERATED client config to a peer. One-way, master → peer.

    The master's ``~/.scitex/agent-container/config.yaml`` is the only
    hand-edited topology file in the fleet (ADR-0021). This verb renders
    each peer's minimal client config from it (canonical name,
    ``comms_nodes.sync_on_start: false``, the route back to the master)
    and reconciles the peer's copy — or refuses, loudly.

    \b
    Detect (read-only, cron-friendly — exits non-zero on drift):
      $ sac host push-config --check spartan
      $ sac host push-config --check --all --json
    \b
    Reconcile:
      $ sac host push-config spartan
      $ sac host push-config --all
    \b
    Verdicts, each printed with its evidence:
      current       remote content matches the render (timestamp aside)
      STALE         has our header but differs -> push overwrites it
      ABSENT        no config.yaml there -> push creates it
      HAND-EDITED   exists WITHOUT our header -> REFUSED + diff printed;
                    only `--adopt` (single peer) replaces it, after
                    backing it up ON THE PEER as config.yaml.pre-adopt-*
      UNKNOWN       ssh failed / unreadable -> exit non-zero, NEVER
                    written to. An unknown peer is not a clean peer.

    Every push is verified by reading the peer back and comparing bytes;
    a push that cannot substantiate itself reports FAILED. The peer-side
    path is expanded remotely ($HOME on the peer) — never locally.
    """
    if bool(peer) == all_peers:
        raise click.UsageError(
            "give exactly one of PEER or --all  (e.g. `sac host push-config "
            "--check spartan` or `sac host push-config --check --all`)"
        )
    if adopt and check_only:
        raise click.UsageError(
            "--adopt mutates; --check is read-only. Run one or the other."
        )
    if adopt and all_peers:
        raise click.UsageError(
            "--adopt is surgical: name exactly ONE peer whose hand-edited "
            "config you have reviewed (never --all)."
        )
    if peer and any(c in peer for c in "*?["):
        raise click.UsageError(
            f"'{peer}' is a glob template, not a host — push-config targets "
            "concrete peers only"
        )

    cfg = _load_cfg()
    if peer and peer == cfg.canonical_host():
        raise click.UsageError(
            f"'{peer}' is this host (the master). The master's config.yaml is "
            "the hand-edited SSOT — it is never generated."
        )
    targets = syncable_peers(cfg) if all_peers else [peer or ""]
    if not targets:
        raise click.UsageError(
            f"no syncable peers in {cfg.source_path} — add one with `sac host add`"
        )
    missing = [name for name in targets if name not in cfg.peers]
    if missing:
        raise click.UsageError(
            f"peer '{missing[0]}' is not defined in {cfg.source_path} — add it "
            f"on the MASTER with `sac host add {missing[0]} --ssh <user@host>`"
        )

    results: list[PushConfigResult] = []
    for name in targets:
        if check_only:
            results.append(check_config_peer(name, cfg, timeout=timeout))
        else:
            results.append(push_config_peer(name, cfg, adopt=adopt, timeout=timeout))
    code = max((r.exit_code for r in results), default=0)

    if _json_flag(ctx, as_json):
        payload = {
            "mode": "check" if check_only else "push",
            "exit_code": code,
            "peers": [r.to_dict() for r in results],
        }
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)

    mode = "check (read-only)" if check_only else "push"
    console.print(
        f"[bold]sac host push-config {mode}[/bold]  master -> {len(results)} peer(s)\n"
    )
    for result in results:
        _print_result(result, show_diff=show_diff)

    # Never silent: say what the verdict MEANS, not just what it was.
    drifted = [r.peer for r in results if r.exit_code != 0]
    if check_only and drifted:
        console.print(
            f"[yellow]config drift on {len(drifted)} peer(s):[/yellow] "
            f"{', '.join(drifted)}\n"
            "  These peers are NOT running the master's generated client "
            "config. Reconcile with:\n"
            f"    sac host push-config {drifted[0]}"
        )
    elif code == 0:
        console.print(
            "[green]all peers carry the master's generated client config[/green] "
            "[dim](verified by read-back bytes, not by a write's exit code)[/dim]"
        )
    raise SystemExit(code)


def register(host_group) -> None:
    """Attach ``push-config`` to the parent ``host`` Click group."""
    host_group.add_command(host_push_config)


__all__ = ["host_push_config", "register"]
