"""``sac host push-config`` — generated client config + bearer tokens.

Sibling of :mod:`._host_sync` (one-way code sync) and registered onto
``host_group`` the same way. Where ``host sync`` moves CODE, this verb
moves the peer's minimal GENERATED client config and — PR-B — the bearer
tokens that ride the same guarded channel (ADR-0021: the master's
config.yaml is the fleet's only hand-edited topology file).

Rendering rule, inherited verbatim from the sibling: **there is no quiet
success path.** Every peer prints its verdict and its evidence, a
refusal prints the offending diff and names the next command, and an
UNKNOWN peer exits non-zero rather than passing as clean.

Token surface, and why it is shaped this way
--------------------------------------------
* **Reading is always on; writing is opt-in.** ``--check`` reports token
  state because a silent bearer desync is the exact failure this PR
  exists to catch, and an alarm nobody armed is not an alarm.
  ``--no-tokens`` opts out. Nothing in check mode can mutate a peer.
* **Writing needs a flag.** A plain push touches no tokens without
  ``--with-tokens`` (which pushes only the master's OWN bearer — the leg
  that cannot break a working peer).
* **Rotation is its own mode**, single-peer, and never fleet-wide.

Token VALUES never reach this module. Verdicts carry 12-char sha256
prefixes only (:func:`.._hostsync.sha12`), and the peer-side read
digests on the peer — mirroring
:func:`.._listen.peer_tokens.list_peer_hosts`, which has never printed a
value either.
"""

from __future__ import annotations

import json

import click

from .._hostsync import (
    ConfigVerdict,
    PushConfigResult,
    TokenStateResult,
    TokenVerdict,
    check_config_peer,
    check_tokens_peer,
    push_config_peer,
    push_master_bearer,
    rotate_peer_tokens,
    syncable_peers,
)
from .._hostsync._token_state import DEFAULT_LISTEN_PORT
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

_TOKEN_STYLE = {
    TokenVerdict.TOKENS_CURRENT: ("green", "tokens-current"),
    TokenVerdict.TOKENS_DRIFTED: ("red", "TOKENS-DRIFTED"),
    TokenVerdict.TOKENS_ABSENT: ("yellow", "TOKENS-ABSENT"),
    TokenVerdict.UNDETERMINED: ("magenta", "TOKENS-UNKNOWN"),
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


def _print_token_result(result: TokenStateResult) -> None:
    """One peer's token state — digests only, never a value.

    The digest columns are the evidence: an operator comparing two
    sha256 prefixes by eye can see WHICH leg drifted, which is the whole
    diagnosis. Printing the tokens themselves would put live fleet
    bearers in every cron log for no diagnostic gain at all.
    """
    colour, label = _TOKEN_STYLE[result.verdict]
    _evidence(
        f"[{colour}]{label:<14}[/{colour}] {result.peer}  "
        f"[dim]action: {result.action}[/dim]"
    )
    if result.peer_hostname:
        _evidence(f"    peer hostname   {result.peer_hostname}")
    _evidence(
        f"    outbound        peer holds "
        f"sha256:{result.peer_holds_master_sha12 or '<absent>'}  vs  "
        f"master bearer sha256:{result.master_bearer_sha12 or '<absent>'}"
    )
    _evidence(
        f"    inbound         master holds "
        f"sha256:{result.master_holds_peer_sha12 or '<absent>'}  vs  "
        f"peer bearer sha256:{result.peer_bearer_sha12 or '<absent>'}"
    )
    for name, digest in sorted(result.listen_files.items()):
        _evidence(f"    listen file     {name}  sha256:{digest or '<undigested>'}")
    for line in result.detail.splitlines():
        _evidence(f"    {line}" if line.strip() else "")
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
@click.option(
    "--rotate-tokens",
    "rotate_peer",
    metavar="PEER",
    default="",
    help=(
        "ROTATE ONE peer's listen bearer: mint -> seed both sides -> restart "
        "its listen -> verify with an authenticated probe -> only then discard "
        "the old one. Single peer only; never with --all or --check."
    ),
)
@click.option(
    "--with-tokens",
    is_flag=True,
    default=False,
    help=(
        "During a push, also write the MASTER's own bearer to the peer's "
        "peer-tokens/<master>.token (the leg the peer calls home with). "
        "Off by default: a push touches no secrets unless asked."
    ),
)
@click.option(
    "--no-tokens",
    is_flag=True,
    default=False,
    help="Skip the token-state check (config drift only).",
)
@click.option(
    "--listen-port",
    type=int,
    default=DEFAULT_LISTEN_PORT,
    show_default=True,
    help="The peer's `sac listen` port, for --rotate-tokens' verify probe.",
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
    rotate_peer: str,
    with_tokens: bool,
    no_tokens: bool,
    listen_port: int,
    timeout: int,
    as_json: bool,
) -> None:
    """Push the GENERATED client config + bearer tokens, master → peer.

    The master's ``~/.scitex/agent-container/config.yaml`` is the only
    hand-edited topology file in the fleet (ADR-0021). This verb renders
    each peer's minimal client config from it (canonical name,
    ``comms_nodes.sync_on_start: false``, the route back to the master)
    and reconciles the peer's copy — or refuses, loudly. The fleet's
    bearer tokens ride the same guarded channel.

    \b
    Detect (read-only, cron-friendly — exits non-zero on drift):
      $ sac host push-config --check spartan
      $ sac host push-config --check --all --json
    \b
    Reconcile:
      $ sac host push-config spartan
      $ sac host push-config --all
      $ sac host push-config spartan --with-tokens   # + the master's bearer
    \b
    Rotate ONE peer's listen bearer (mints, restarts its listen, verifies):
      $ sac host push-config --rotate-tokens spartan
    \b
    Config verdicts, each printed with its evidence:
      current       remote content matches the render (timestamp aside)
      STALE         has our header but differs -> push overwrites it
      ABSENT        no config.yaml there -> push creates it
      HAND-EDITED   exists WITHOUT our header -> REFUSED + diff printed;
                    only `--adopt` (single peer) replaces it, after
                    backing it up ON THE PEER as config.yaml.pre-adopt-*
      UNKNOWN       ssh failed / unreadable -> exit non-zero, NEVER
                    written to. An unknown peer is not a clean peer.
    \b
    Token verdicts (--check; `--no-tokens` skips them):
      tokens-current  both a2a legs match
      TOKENS-DRIFTED  a leg mismatches -> that direction's a2a is dead
      TOKENS-ABSENT   a leg's token file does not exist
      TOKENS-UNKNOWN  unreadable, or the peer holds several DIFFERENT
                      listen tokens (which one its listen serves is not
                      knowable from here) -> rotate to collapse them

    Only sha256 digests (12 chars) are ever printed — never a token.

    Every push is verified by reading the peer back and comparing bytes;
    a push that cannot substantiate itself reports FAILED. The peer-side
    path is expanded remotely ($HOME on the peer) — never locally.
    """
    if rotate_peer:
        if check_only:
            raise click.UsageError(
                "--rotate-tokens MUTATES (it mints a bearer and restarts the "
                "peer's listen); --check is read-only. Run one or the other."
            )
        if all_peers:
            raise click.UsageError(
                "--rotate-tokens is surgical: it restarts the named peer's "
                "listen, so a fleet-wide rotation would take every peer's "
                "control plane down at once. Name exactly ONE peer."
            )
        if peer:
            raise click.UsageError(
                "--rotate-tokens already names the peer — drop the positional "
                f"argument (`sac host push-config --rotate-tokens {rotate_peer}`)."
            )
        if adopt:
            raise click.UsageError(
                "--adopt is for a hand-edited CONFIG; --rotate-tokens is for "
                "TOKENS. Run them separately."
            )
    elif bool(peer) == all_peers:
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
    if with_tokens and check_only:
        raise click.UsageError(
            "--with-tokens WRITES the master's bearer to the peer; --check is "
            "read-only. Drop one. (--check already REPORTS token state.)"
        )
    target = rotate_peer or peer or ""
    if target and any(c in target for c in "*?["):
        raise click.UsageError(
            f"'{target}' is a glob template, not a host — push-config targets "
            "concrete peers only"
        )

    cfg = _load_cfg()
    if target and target == cfg.canonical_host():
        raise click.UsageError(
            f"'{target}' is this host (the master). The master's config.yaml is "
            "the hand-edited SSOT — it is never generated."
        )
    if rotate_peer:
        if rotate_peer not in cfg.peers:
            raise click.UsageError(
                f"peer '{rotate_peer}' is not defined in {cfg.source_path} — add "
                f"it on the MASTER with `sac host add {rotate_peer} --ssh <user@host>`"
            )
        _run_rotate(
            ctx,
            rotate_peer,
            cfg,
            listen_port=listen_port,
            timeout=timeout,
            as_json=as_json,
        )
        return
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
    tokens: list[TokenStateResult] = []
    for name in targets:
        if check_only:
            results.append(check_config_peer(name, cfg, timeout=timeout))
            if not no_tokens:
                tokens.append(check_tokens_peer(name, cfg, timeout=timeout))
        else:
            results.append(push_config_peer(name, cfg, adopt=adopt, timeout=timeout))
            if with_tokens:
                tokens.append(push_master_bearer(name, cfg, timeout=timeout))
    code = max(
        (r.exit_code for r in [*results, *tokens]),
        default=0,
    )

    if _json_flag(ctx, as_json):
        payload = {
            "mode": "check" if check_only else "push",
            "exit_code": code,
            "peers": [r.to_dict() for r in results],
            "tokens": [t.to_dict() for t in tokens],
        }
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)

    mode = "check (read-only)" if check_only else "push"
    console.print(
        f"[bold]sac host push-config {mode}[/bold]  master -> {len(results)} peer(s)\n"
    )
    for result in results:
        _print_result(result, show_diff=show_diff)
    for token in tokens:
        _print_token_result(token)

    # Never silent: say what the verdict MEANS, not just what it was.
    drifted = [r.peer for r in results if r.exit_code != 0]
    tok_drifted = [t.peer for t in tokens if t.exit_code != 0]
    if check_only and drifted:
        console.print(
            f"[yellow]config drift on {len(drifted)} peer(s):[/yellow] "
            f"{', '.join(drifted)}\n"
            "  These peers are NOT running the master's generated client "
            "config. Reconcile with:\n"
            f"    sac host push-config {drifted[0]}"
        )
    if check_only and tok_drifted:
        console.print(
            f"[red]token drift on {len(tok_drifted)} peer(s):[/red] "
            f"{', '.join(tok_drifted)}\n"
            "  a2a is broken (or will break at the next listen restart) in at "
            "least one direction. Reconcile with:\n"
            f"    sac host push-config {tok_drifted[0]} --with-tokens   "
            "# the master's OWN bearer\n"
            f"    sac host push-config --rotate-tokens {tok_drifted[0]}   "
            "# the peer's bearer (restarts its listen)"
        )
    if code == 0:
        console.print(
            "[green]all peers carry the master's generated client config[/green] "
            "[dim](verified by read-back bytes, not by a write's exit code)[/dim]"
        )
    raise SystemExit(code)


def _run_rotate(
    ctx: click.Context,
    peer: str,
    cfg,
    *,
    listen_port: int,
    timeout: int,
    as_json: bool,
) -> None:
    """``--rotate-tokens`` — the one mode that can break live a2a.

    Kept in its own function because its output has nothing in common
    with the per-peer verdict table: a rotation is ONE event with a
    sequence, and what an operator needs on a failure is which side holds
    what, not a column.
    """
    result = rotate_peer_tokens(peer, cfg, timeout=timeout, port=listen_port)
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "mode": "rotate-tokens",
                    "exit_code": result.exit_code,
                    "rotation": result.to_dict(),
                },
                indent=2,
            )
        )
        raise SystemExit(result.exit_code)

    console.print(f"[bold]sac host push-config --rotate-tokens[/bold]  {peer}\n")
    colour = "green" if result.ok else "red"
    label = result.action.upper() if not result.ok else "rotated"
    _evidence(f"[{colour}]{label:<14}[/{colour}] {peer}")
    if result.new_sha12:
        _evidence(f"    new bearer      sha256:{result.new_sha12}")
    if result.old_sha12:
        _evidence(f"    old bearer      sha256:{result.old_sha12}")
    _evidence(f"    listen restart  {'yes' if result.restarted else 'no'}")
    _evidence(f"    verified        {'yes' if result.verified else 'NO'}")
    if result.backup:
        _evidence(f"    RETAINED        {result.backup}")
    for line in result.detail.splitlines():
        _evidence(f"    {line}" if line.strip() else "")
    console.print("")
    raise SystemExit(result.exit_code)


def register(host_group) -> None:
    """Attach ``push-config`` to the parent ``host`` Click group."""
    host_group.add_command(host_push_config)


__all__ = ["host_push_config", "register"]
